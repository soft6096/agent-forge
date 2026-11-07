"""模型接入：Provider 抽象 + 两种实现。

- openai：真实调用，零第三方依赖（urllib）
- replay：离线回放，按输入哈希找录制响应；没有录制就返回一条明确的"无法处理"

为什么要有 replay：评测入口必须在任何环境都能跑（0.0 第 8 项）。
没有凭据时降级到回放而不是报错，这就是降级路径的第一个落点。
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .settings import Settings


class ModelError(Exception):
    """模型层失败。retryable 决定骨架是否重试。"""

    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class LLMResponse:
    """一次模型调用的结果。tokens 必须来自 API 返回，不能本地估算。"""

    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = "stop"
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    """Provider 契约：只做一次调用，不管重试、预算、日志。"""

    name: str

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        timeout_ms: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """发一次请求。

        :param messages: 完整消息列表，含 system/user/assistant 与 tool 角色消息
        :param timeout_ms: 单次调用超时（毫秒），由骨架传入，Provider 内部不重试
        :param tools: function calling 工具清单；None 表示本轮不给模型任何工具
        :return: 一次调用的结果，tool_calls 非空表示模型要求执行工具
        """
        ...


class ReplayProvider:
    """离线回放：按用户输入找录制文件，逐轮返回录制的响应。

    为什么必须支持多轮：真实任务是多步的（模型请求工具 → 回填 → 再调模型）。
    只回放单条响应的话，第二次调用会重复上一句，直接触发骨架的死循环识别；
    评测也就无法覆盖"工具调用 → 回填 → 结论"这条主干链路。
    """

    name = "replay"

    def __init__(self, replay_dir: Path):
        """扫描录制目录，建立 input 原文、输入别名与输入哈希三套索引。

        :param replay_dir: 录制目录，一个文件对应用例的一组轮次
        """
        self._dir = replay_dir
        self._by_input: dict[str, Path] = {}
        self._by_digest: dict[str, Path] = {}
        self._cursor: dict[str, int] = {}
        self._turns: dict[str, list[dict[str, Any]]] = {}
        self._scan()

    def _scan(self) -> None:
        """扫描并缓存录制文件。坏文件跳过，不让一条录制污染整轮评测。"""
        if not self._dir.is_dir():
            return

        # 1. 逐个读取录制：数量在几十条量级，启动时一次读完比每次调用扫目录划算
        for path in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue

            # 2. 建索引：原文优先（可读、改名不影响），哈希兜底（老命名兼容）
            self._by_digest[path.stem] = path
            input_text = data.get("input")
            if isinstance(input_text, str) and input_text:
                self._by_input[input_text] = path

            # 3. 别名索引：入向过滤命中注入特征时会给 user 消息前置一行安全提示，
            #    回放又按"最后一条 user 消息"定位录制，不带别名会永远命中不到对抗样本
            aliases = data.get("input_aliases")
            if isinstance(aliases, list):
                for alias in aliases:
                    if isinstance(alias, str) and alias:
                        self._by_input[alias] = path
            turns = data.get("turns")
            self._turns[path.stem] = turns if isinstance(turns, list) else [data]

    def recording_turns(self, user_input: str) -> int:
        """查询某个输入在录制里的可用轮次，0 表示没有录制。

        :param user_input: 用户输入原文
        :return: 录制轮次数量；评测的 --check-only 用它提前发现缺数据
        """
        # 1. 原文匹配优先，哈希兜底
        path = self._by_input.get(user_input)
        if path is None:
            digest = hashlib.sha1(user_input.encode("utf-8")).hexdigest()[:16]
            path = self._by_digest.get(digest)

        # 2. 未找到返回 0，调用方据此判定缺录制
        if path is None:
            return 0
        return len(self._turns.get(path.stem, []))

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        timeout_ms: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """回放当前输入的下一条录制响应。

        :param messages: 消息列表，只取最后一条 user 消息定位录制
        :param timeout_ms: 忽略，回放不产生真实等待
        :param tools: 忽略，工具调用已固化在录制里
        :return: 录制的响应；录制缺失或轮次用尽时返回提示性文本而不是报错
        """
        # 1. 定位录制：原文匹配优先，哈希兜底
        last_user = next(
            (str(m.get("content") or "") for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        digest = hashlib.sha1(last_user.encode("utf-8")).hexdigest()[:16]
        path = self._by_input.get(last_user) or self._by_digest.get(digest)
        if path is None:
            return LLMResponse(
                text="离线回放模式：未找到该输入的录制响应，无法给出判定。",
                finish_reason="stop",
            )

        # 2. 取本轮：游标按录制文件独立推进，同一进程内多次任务互不干扰
        turns = self._turns.get(path.stem, [])
        index = self._cursor.get(path.stem, 0)
        if index >= len(turns):
            return LLMResponse(
                text="离线回放模式：录制轮次已用尽（共 %d 轮），无法继续。" % len(turns),
                finish_reason="stop",
            )
        turn = turns[index]

        # 3. 失败注入：录制里声明 raise 时按声明抛 ModelError，用于覆盖失败分支。
        #    WHY: 不推进游标——骨架重试应当重放同一轮，否则重试会跳内容
        if turn.get("raise"):
            spec = turn["raise"] or {}
            raise ModelError(
                str(spec.get("message") or "录制注入的模型错误"),
                retryable=bool(spec.get("retryable", False)),
            )

        # 4. 推进游标并组装响应
        self._cursor[path.stem] = index + 1
        return LLMResponse(
            text=str(turn.get("text") or ""),
            prompt_tokens=int(turn.get("prompt_tokens") or 0),
            completion_tokens=int(turn.get("completion_tokens") or 0),
            finish_reason=str(turn.get("finish_reason") or "stop"),
            tool_calls=list(turn.get("tool_calls") or []),
            raw=turn,
        )


class OpenAIProvider:
    """真实调用。失败按可重试性分类，不自带重试循环。"""

    name = "openai"

    def __init__(self, settings: Settings):
        self._s = settings

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        timeout_ms: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """发一次 chat/completions 请求，失败按可重试性抛 ModelError。

        :param messages: 完整消息列表，含 tool 角色消息与 assistant 的 tool_calls
        :param timeout_ms: 单次调用超时（毫秒），直接作为 socket 超时
        :param tools: function calling 工具清单；给了就带 tool_choice=auto
        :return: 模型回复，含文本、用量与 tool_calls
        """
        # 1. 组装请求体：基础参数来自配置，工具清单按需附加
        payload: dict[str, Any] = {
            "model": self._s.model.name,
            "messages": messages,
            "temperature": self._s.model.temperature,
            "max_tokens": self._s.model.max_output_tokens,
        }
        if tools:
            # WHY: 不给 tools 时模型永远发不出 tool_calls，工具层就是摆设；
            # 给了 tools 必须同时给 tool_choice，否则部分兼容实现会忽略工具
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        req = urllib.request.Request(
            self._s.model.api_base.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % self._s.model.api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_ms / 1000.0) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 5xx 与 429 可重试；4xx（除 429）是请求本身有问题，重试无意义
            retryable = exc.code >= 500 or exc.code == 429
            raise ModelError("HTTP %d：%s" % (exc.code, exc.reason), retryable=retryable) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ModelError("网络或超时：%s" % exc, retryable=True) from exc

        choice = (body.get("choices") or [{}])[0]
        usage = body.get("usage") or {}
        message = choice.get("message") or {}
        return LLMResponse(
            text=message.get("content") or "",
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            finish_reason=choice.get("finish_reason", "stop"),
            tool_calls=message.get("tool_calls") or [],
            raw=body,
        )


def build_provider(settings: Settings) -> LLMProvider:
    """按配置选 Provider。auto 时无凭据自动降级为回放，不抛错。"""
    kind = settings.model.effective_provider
    if kind == "openai":
        return OpenAIProvider(settings)
    return ReplayProvider(settings.project_root / "evals" / "replay")
