"""上下文窗口投影：按预算表把全量会话事实裁成一次请求可发送的上下文。

三条不可让步的规则：
1. 系统提示词是不可变段，永不被裁剪、也永不被摘要替换；
2. 裁剪只发生在投影副本上，会话状态保持完整，中断恢复才不丢东西；
3. 降级按固定阶梯（折叠外部内容 → 压历史 → 明确告知不足），禁止静默截断。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..settings import ContextSettings
from .budget import estimate_message, estimate_tokens

# 降级级别：0 未降级，1 折叠外部内容，2 压缩历史，3 仍超限（必须终止并告知用户）
DEGRADE_NONE = 0
DEGRADE_FOLD_EXTERNAL = 1
DEGRADE_COMPRESS_HISTORY = 2
DEGRADE_EXCEEDED = 3

# 折叠占位文本的前缀，便于日志与排查区分"原文"与"折叠后"
_FOLD_SUFFIX = "…[已折叠 %d 字符：单条结果超出上限，完整内容见会话快照]"


@dataclass(frozen=True)
class ContextReport:
    """一次投影的统计结果，直接作为裁剪日志的字段来源。"""

    step_no: int           # 第几步发生的投影
    before_tokens: int     # 投影前估算占用
    after_tokens: int      # 投影后估算占用
    dropped_messages: int  # 被摘要替换的消息条数
    folded_results: int    # 被折叠的工具结果条数
    strategy: str          # window / summary / retrieval_trim / degrade
    degraded: int          # 降级级别，含义见 DEGRADE_* 常量

    @property
    def trimmed(self) -> bool:
        """本次是否真的发生了裁剪或折叠。没发生就不必刷日志。"""
        return self.dropped_messages > 0 or self.folded_results > 0 or self.degraded > 0

    def to_log(self) -> dict[str, Any]:
        """转成结构化日志字段。

        :return: 含步号、前后占用、丢弃条数与降级级别的字典
        """
        return {
            "step": self.step_no,
            "strategy": self.strategy,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "dropped_items": self.dropped_messages,
            "folded_items": self.folded_results,
            "degraded": self.degraded,
        }


class ContextWindow:
    """按配置预算把消息列表投影成可发送的上下文。"""

    def __init__(self, budget: ContextSettings):
        """保存预算表。

        :param budget: 五段配额与压缩阈值，全部来自配置
        """
        self._b = budget

    def project(
        self,
        messages: list[dict[str, Any]],
        step_no: int,
        tool_tokens: int = 0,
    ) -> tuple[list[dict[str, Any]], ContextReport]:
        """把消息投影成可发送列表，并给出裁剪统计。

        :param messages: 会话全量消息（状态事实，本方法不修改它）
        :param step_no: 当前步序号，用于日志
        :param tool_tokens: 本轮工具描述的估算占用
        :return: （可发送消息列表, 统计报告）；报告 degraded 为 3 时调用方必须终止
        """
        before = sum(estimate_message(item) for item in messages) + tool_tokens

        # 1. 拆分不可变段与可变部分：system 永不被裁剪
        system = [item for item in messages if item.get("role") == "system"]
        rest = [item for item in messages if item.get("role") != "system"]
        system_tokens = sum(estimate_message(item) for item in system)
        if system_tokens > self._b.system:
            raise ValueError(
                "系统提示词 %d token 超出配额 %d，不可变段不能裁剪，请改配置或精简提示词"
                % (system_tokens, self._b.system)
            )

        # 2. 第一级降级：折叠超长工具结果，避免一条结果吃掉整段外部内容预算
        folded: list[dict[str, Any]] = []
        folded_count = 0
        single_cap = max(200, self._b.retrieval // 2)
        for item in rest:
            if item.get("role") == "tool" and estimate_message(item) > single_cap:
                folded.append(_fold(item, single_cap))
                folded_count += 1
            else:
                folded.append(item)

        # 3. 第二级降级：历史滑窗，从最新往前取，且至少保留配置里要求的近期条数
        kept, dropped = self._take_recent(folded)

        # 4. 组装固定顺序：不可变段 → 会话摘要 → 近期历史
        summary = _summarize(dropped) if dropped else ""
        projected = list(system)
        if summary:
            projected.append({"role": "system", "content": summary})
        projected.extend(kept)

        # 5. 收敛校验：剩给输出的空间不足就不允许再发请求，明确走到第三级
        used = sum(estimate_message(item) for item in projected) + tool_tokens
        headroom = self._b.usable_tokens - self._b.output_reserve
        degraded = DEGRADE_NONE
        if folded_count:
            degraded = DEGRADE_FOLD_EXTERNAL
        if dropped:
            degraded = DEGRADE_COMPRESS_HISTORY
        if used > headroom:
            degraded = DEGRADE_EXCEEDED

        # 6. 归纳策略名，供日志一眼看出这次做了哪一级
        if not folded_count and not dropped:
            strategy = "window"
        elif dropped:
            strategy = "summary"
        else:
            strategy = "retrieval_trim"
        if degraded == DEGRADE_EXCEEDED:
            strategy = "degrade"

        report = ContextReport(
            step_no=step_no,
            before_tokens=before,
            after_tokens=used,
            dropped_messages=len(dropped),
            folded_results=folded_count,
            strategy=strategy,
            degraded=degraded,
        )
        return projected, report

    def _take_recent(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """从最新往前取消息，直到历史预算用尽。

        :param messages: 可裁剪的消息（不含 system）
        :return: （保留的消息, 被丢弃的消息），两者都按时间正序
        """
        budget = self._b.history
        kept_reversed: list[dict[str, Any]] = []
        used = 0

        # 1. 逆序累加：近期消息优先保住
        for item in reversed(messages):
            cost = estimate_message(item)
            over_budget = used + cost > budget
            # 2. 已满足最少保留条数且超预算时才停，保证最近 N 条始终在
            if over_budget and len(kept_reversed) >= self._b.keep_recent_messages:
                break
            kept_reversed.append(item)
            used += cost

        # 3. 还原正序
        kept = list(reversed(kept_reversed))
        dropped = messages[: len(messages) - len(kept)]
        return kept, dropped


def _fold(message: dict[str, Any], cap: int) -> dict[str, Any]:
    """把超长工具结果折叠成占位摘要。

    :param message: 原始 tool 消息
    :param cap: 保留的字符数上限
    :return: 折叠后的新消息字典（不修改入参）
    """
    # 1. 保留头部：错误码、关键字段通常在前部，先保住可判定的信息
    content = str(message.get("content") or "")
    head = content[:cap]

    # 2. 复制并改写正文，附带原文长度便于排查
    folded = dict(message)
    folded["content"] = head + (_FOLD_SUFFIX % (len(content) - cap))
    return folded


def _summarize(dropped: list[dict[str, Any]]) -> str:
    """把被裁掉的消息压成结构化摘要。

    WHY: 只保留工具名与用户诉求片段，不改写任何数值；宁可少写，也不能编造。

    :param dropped: 被裁掉的消息，按时间正序
    :return: 摘要文本；空列表返回空串
    """
    tools: list[str] = []
    asks: list[str] = []

    # 1. 分类提取：工具名去重保序，用户诉求保留片段
    for item in dropped:
        role = item.get("role")
        if role == "tool":
            name = str(item.get("name") or "unknown")
            if name not in tools:
                tools.append(name)
        elif role == "user":
            text = str(item.get("content") or "").replace("\n", " ")
            asks.append(text[:60] + ("…" if len(text) > 60 else ""))

    # 2. 固定模板输出，便于机器读与人工核对
    parts = ["[会话摘要] 覆盖前 %d 条消息" % len(dropped)]
    if tools:
        parts.append("已调用工具：%s" % "、".join(tools))
    if asks:
        parts.append("用户诉求：%s" % "；".join(asks))
    return "；".join(parts)


__all__ = [
    "ContextReport",
    "ContextWindow",
    "DEGRADE_COMPRESS_HISTORY",
    "DEGRADE_EXCEEDED",
    "DEGRADE_FOLD_EXTERNAL",
    "DEGRADE_NONE",
    "estimate_tokens",
]
