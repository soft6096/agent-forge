"""主循环：五类终止、三处超时、失败分类、步数与预算上限。

骨架纪律（agent-core-standards）：
1. 每步只做一件事：调模型 → 解析 → 执行工具 → 回填
2. 五类终止各有独立返回结构，禁止统一 error
3. 失败按层分类，只有可重试的才重试，且重试次数由骨架控制
4. 模型输出一律视为不可信，解析失败不得冒泡成系统错误
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .llm import LLMProvider, ModelError
from .ops.cost import Budget
from .ops.logging import StructuredLogger
from .settings import Settings
from .state import SessionState
from .tools.contract import ToolContext, ToolOutcome
from .tools.registry import ToolRegistry


class TermReason(str, Enum):
    """五类终止原因，缺一类即骨架不完整。"""

    COMPLETED = "completed"              # 模型给出最终答案
    STEP_EXHAUSTED = "step_exhausted"    # 达到最大步数
    BUDGET_EXCEEDED = "budget_exceeded"  # token 或金额超限
    CANCELLED = "cancelled"              # 用户中断
    BLOCKED = "blocked"                  # 安全拦截或死循环识别


@dataclass
class TaskResult:
    """一次任务的最终结果。halted_at 用来说明"卡在哪一步、在等什么"。"""

    reason: TermReason
    answer: str
    session: SessionState
    steps: int
    usage: dict[str, Any]
    halted_at: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason.value,
            "answer": self.answer,
            "steps": self.steps,
            "usage": self.usage,
            "halted_at": self.halted_at,
            "detail": self.detail,
            "session_id": self.session.session_id,
            "status": self.session.status,
        }


class AgentLoop:
    """主循环：调模型 → 执行工具 → 回填结果，每一步都在检查点判终止。"""

    def __init__(
        self,
        settings: Settings,
        provider: LLMProvider,
        logger: StructuredLogger,
        tools: ToolRegistry | None = None,
    ):
        """装配一次任务的运行环境。

        :param settings: 只读配置，提供预算、超时、提示词与项目根
        :param provider: 模型接入层，只负责单次调用
        :param logger: 结构化日志器
        :param tools: 工具注册表；None 表示本次不给模型任何工具
        :raises ValueError: 工具数超过当轮注入上限时抛出，属配置错误
        """
        self._s = settings
        self._provider = provider
        self._log = logger
        self._tools = tools
        # 工具清单在装配期算一次：每步重算既浪费也容易与登记状态不一致
        self._tool_schemas = tools.schemas() if tools is not None else None
        self._budget = Budget(settings.budget)
        self._deadline_ms = int(time.time() * 1000) + settings.timeout.task_ms
        self._cancel_requested = False
        self._max_retries = 1
        # 工具级重试次数：只对可重试错误生效，写类工具靠幂等键保证重复执行收敛
        self._max_tool_retries = 1
        self._recent_tool_signatures: list[str] = []
        # 工具结果单条截断上限：不截断的话一次大结果就能吃掉大半上下文预算
        self._max_tool_chars = 4000

    def request_cancel(self) -> None:
        """由入口的信号处理调用，下一步检查点生效。"""
        self._cancel_requested = True

    # ---------- 主流程 ----------

    def run(self, user_input: str) -> TaskResult:
        """跑一次任务。每步开始检查终止条件，不允许在步中间 return。"""
        state = SessionState.new(user_input)
        # 日志必须在第一条记录之前拿到真实 session_id，否则整轮日志无法与会话对齐
        self._log.session_id = state.session_id
        # system 必须排在 user 之前，顺序错了模型会当作用户中途插入的指令
        state.add_message("system", self._s.prompt_text)
        state.add_message("user", user_input)

        for step in range(1, self._s.budget.max_steps + 1):
            state.step_no = step

            # 终止条件检查点：中断 → 预算 → 任务超时（三类都在调模型之前判）
            if self._cancel_requested:
                return self._terminate(TermReason.CANCELLED, state, "收到中断信号，已保留会话快照")
            over = self._budget.exceeded()
            if over:
                return self._terminate(
                    TermReason.BUDGET_EXCEEDED, state, "预算耗尽（%s）" % over
                )
            if self._task_timeout():
                return self._terminate(TermReason.STEP_EXHAUSTED, state, "任务总时长超限")

            try:
                resp = self._call_llm(state)
            except ModelError as exc:
                if exc.retryable:
                    return self._terminate(
                        TermReason.STEP_EXHAUSTED if not self._cancel_requested else TermReason.CANCELLED,
                        state,
                        "模型层可重试错误重试耗尽：%s" % exc,
                    )
                return self._terminate(TermReason.BLOCKED, state, "模型层不可重试错误：%s" % exc)

            self._budget.record(resp.prompt_tokens, resp.completion_tokens)
            # WHY: assistant 消息必须原样带上 tool_calls，否则后面的 tool 结果没有归属，
            # 真实 API 会直接报协议错误，模型也看不出自己请求过什么
            state.add_message(
                "assistant",
                resp.text,
                tool_calls=list(resp.tool_calls) if resp.tool_calls else None,
            )

            # 调用后必须再判一次预算：只在步首判的话，单步就能把上限撑爆还报"完成"
            over = self._budget.exceeded()
            if over:
                return self._terminate(
                    TermReason.BUDGET_EXCEEDED,
                    state,
                    "预算耗尽（%s）：本次调用后累计 %d tokens / %.4f 元，不再发起下一步"
                    % (over, self._budget.usage.total_tokens, self._budget.cost_cny),
                )

            # 工具调用分支：执行并回填。返回非空表示本步就要终止
            if resp.tool_calls:
                halted = self._handle_tool_calls(resp.tool_calls, state, step)
                if halted is not None:
                    return halted
                continue

            # 无工具调用即视为给出最终答案
            state.status = "completed"
            return self._terminate(TermReason.COMPLETED, state, resp.text)

        state.status = "step_exhausted"
        return self._terminate(
            TermReason.STEP_EXHAUSTED,
            state,
            "达到最大步数 %d，最后一步停在解析模型输出" % self._s.budget.max_steps,
        )

    # ---------- 内部步骤 ----------

    def _call_llm(self, state: SessionState):
        """调一次模型，带单次超时与有限重试。

        :param state: 当前会话状态，提供消息历史与步序号
        :return: 模型回复，含文本、用量与 tool_calls
        :raises ModelError: 重试耗尽或不可重试时上抛，由主循环映射成终止原因
        """
        attempt = 0
        # 1. 循环尝试：次数上限由 _max_retries 控制，模型无权决定重试几次
        while True:
            attempt += 1
            # 每次尝试单独开 span：复用会让重试次数叠加进同一条时延，看不出单次耗时
            span = self._log.child_span("llm_call")
            # 2. 发起调用：超时与工具清单都由骨架传入，Provider 只管这一次请求
            try:
                resp = self._provider.complete(
                    state.messages,
                    timeout_ms=self._s.timeout.llm_ms,
                    tools=self._tool_schemas,
                )
            except ModelError as exc:
                span.finish("error")
                self._log.log(
                    step_no=state.step_no,
                    tool_name="llm",
                    elapsed_ms=span.elapsed_ms,
                    result_code="model_error",
                    event="llm_call",
                    extra={"retryable": exc.retryable, "attempt": attempt},
                )
                if not exc.retryable or attempt > self._max_retries:
                    raise
                # 可重试：退避后继续，次数上限由 _max_retries 控制
                time.sleep(0.2 * attempt)
                continue

            span.finish("ok")
            self._log.log(
                step_no=state.step_no,
                tool_name="llm",
                tokens={"prompt": resp.prompt_tokens, "completion": resp.completion_tokens},
                elapsed_ms=span.elapsed_ms,
                result_code="ok",
                event="llm_call",
                extra={"model": self._s.model.name, "attempt": attempt},
            )
            return resp

    def _handle_tool_calls(
        self, tool_calls: list[dict[str, Any]], state: SessionState, step: int
    ) -> TaskResult | None:
        """执行本步的全部工具调用，并把结果回填进会话。

        :param tool_calls: 模型请求的工具调用列表，遵循 OpenAI function calling 结构
        :param state: 当前会话状态，工具结果追加进 messages
        :param step: 当前步序号，用于日志与幂等键
        :return: 需要终止时返回 TaskResult；继续下一步返回 None
        """
        # 1. 不收敛识别：只取工具名与参数做签名，同一批调用连续 3 步出现即判定原地打转
        signature = json.dumps(
            [{"name": _call_name(c), "arguments": _call_arguments(c)} for c in tool_calls],
            sort_keys=True,
            ensure_ascii=False,
        )
        self._recent_tool_signatures.append(signature)
        if self._recent_tool_signatures[-3:] == [signature] * 3:
            state.status = "blocked"
            return self._terminate(
                TermReason.BLOCKED,
                state,
                "连续 3 步相同工具调用，判定为不收敛，最后一步停在工具回填",
            )

        # 2. 装配兜底：没注入注册表却出现工具调用，属接线错误，必须显性失败而不是忽略
        if self._tools is None:
            state.status = "blocked"
            return self._terminate(
                TermReason.BLOCKED,
                state,
                "模型请求工具调用，但本次运行未装配工具注册表",
            )

        # 3. 逐个执行：参数错误、越权、超时都回填给模型，让它自己改，不中断整轮任务
        context = self._tool_context(state)
        for call in tool_calls:
            name = _call_name(call)
            outcome = self._invoke_with_retry(name, _call_arguments(call), context, step)

            # 4. 回填：tool 消息必须带 tool_call_id 与 name，模型才知道结果属于哪个调用
            state.add_message(
                "tool",
                self._truncate(outcome.to_message_content()),
                tool_call_id=str(call.get("id") or ""),
                name=name,
            )
            state.mark_tool(name)

            # 5. 记日志：工具名、耗时、结果码进结构化日志，慢调用与失败工具可回溯
            self._log.log(
                step_no=step,
                tool_name=name or "unknown",
                elapsed_ms=outcome.elapsed_ms,
                result_code=outcome.error.code.value if outcome.error else "ok",
                event="tool_call",
                extra={"retryable": outcome.retryable, "duplicated": outcome.duplicated},
            )

        # 6. 全部回填完成，交给下一步让模型基于结果继续推理
        return None

    def _tool_context(self, state: SessionState) -> ToolContext:
        """构造工具调用上下文：会话与租户信息由骨架提供，不接受模型参数。

        :param state: 当前会话状态
        :return: 工具执行所需的上下文，含确认开关
        """
        return ToolContext(
            state=state,
            # WHY: 示例工程是单租户；真实接入时这里取会话鉴权结果，绝不读模型给的租户 id
            tenant_id="default",
            project_root=self._s.project_root,
            require_approval=self._s.safety.require_approval,
            retrieval=self._s.retrieval,
        )

    def _invoke_with_retry(
        self,
        name: str,
        arguments: str | dict[str, Any] | None,
        context: ToolContext,
        step: int,
    ) -> ToolOutcome:
        """执行一次工具调用，可重试错误由骨架决定是否重试。

        :param name: 工具名，可能是模型编造的
        :param arguments: 模型给出的原始参数
        :param context: 工具调用上下文，含租户与幂等开关
        :param step: 当前步序号，用于日志定位
        :return: 最终结果；重试耗尽时返回最后一次失败
        """
        # 1. 首次执行：校验、幂等与超时都由注册表处理
        outcome = self._tools.invoke(name, arguments, context=context)

        # 2. 只有可重试错误才重试：参数错误、越权、业务冲突重试多少次结果都一样
        attempt = 1
        while outcome.retryable and attempt <= self._max_tool_retries:
            # WHY: 写类工具重试靠幂等键收敛，重复执行不会产生第二条记录
            time.sleep(0.2 * attempt)
            attempt += 1
            self._log.log(
                step_no=step,
                tool_name=name or "unknown",
                result_code="retry",
                event="tool_retry",
                extra={"attempt": attempt},
            )
            outcome = self._tools.invoke(name, arguments, context=context)

        # 3. 返回最终结果，重试次数由骨架控制，模型无权决定
        return outcome

    def _truncate(self, content: str) -> str:
        """截断过长的工具结果，避免单条结果吃掉大半上下文预算。

        :param content: 序列化后的工具结果
        :return: 原内容，或截断后附带说明的内容
        """
        if len(content) <= self._max_tool_chars:
            return content
        return content[: self._max_tool_chars] + "…[结果已截断，原文 %d 字符]" % len(content)

    def _task_timeout(self) -> bool:
        """任务级超时：与单次调用超时是两回事，必须分开判。"""
        return int(time.time() * 1000) > self._deadline_ms

    def _terminate(self, reason: TermReason, state: SessionState, detail: str) -> TaskResult:
        """统一出口：落状态快照、写收尾日志、返回带用量的结果。"""
        span = self._log.child_span("finalize")
        # 先落快照再写收尾日志：日志里的 elapsed_ms 就是落盘真实耗时，不是占位 0
        state.save(self._s.project_root / "runtime" / "sessions" / ("%s.json" % state.session_id))
        span.finish("ok")
        self._log.log(
            step_no=state.step_no,
            tool_name="finalize",
            elapsed_ms=span.elapsed_ms,
            result_code=reason.value,
            event="task_end",
            extra={"usage": self._budget.snapshot(), "detail": detail[:200]},
        )
        return TaskResult(
            reason=reason,
            answer=detail if reason is TermReason.COMPLETED else "",
            session=state,
            steps=state.step_no,
            usage=self._budget.snapshot(),
            halted_at=("step %d" % state.step_no) if reason is not TermReason.COMPLETED else None,
            detail=detail,
        )


def _call_name(call: dict[str, Any]) -> str:
    """从一条工具调用里取工具名。

    :param call: 单条工具调用；OpenAI 是 {"function": {"name": ...}}，
                 部分兼容实现是扁平结构，两种都要认
    :return: 工具名；取不到时返回空串，注册表会按"工具不存在"给可修正提示
    """
    # 1. 优先取嵌套结构
    function = call.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "")

    # 2. 退回扁平结构
    return str(call.get("name") or "")


def _call_arguments(call: dict[str, Any]) -> str | dict[str, Any] | None:
    """从一条工具调用里取原始参数。

    :param call: 单条工具调用
    :return: 原始 arguments（通常是 JSON 字符串）；解析与校验一律交给注册表
    """
    # 1. 优先取嵌套结构
    function = call.get("function")
    if isinstance(function, dict):
        return function.get("arguments")

    # 2. 退回扁平结构
    return call.get("arguments")
