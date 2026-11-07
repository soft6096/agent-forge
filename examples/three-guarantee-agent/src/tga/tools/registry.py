"""工具注册表：契约登记、schema 导出、统一执行入口。

为什么集中执行：参数校验、确认开关、超时、错误归一化这几件事，一旦散落到各个工具里，
必然出现"这个工具做了超时、那个没做"的不一致。注册表是唯一入口，工具只写业务。
重试不在这里做——可重试性由错误码表达，重试次数与退避归主循环。
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from dataclasses import replace
from typing import Any, Iterable

from ..ops.logging import StructuredLogger
from ..settings import Settings
from .contract import (
    SideEffect,
    ToolContext,
    ToolError,
    ToolErrorCode,
    ToolFailure,
    ToolOutcome,
    ToolSpec,
    build_function_schema,
    check_spec,
    validate_arguments,
)
from .idempotency import Claim, ClaimStatus, FileIdempotencyStore

# 同一轮上下文里工具数的硬上限：超过 20 个，模型选工具的准确率会明显下降
MAX_TOOLS_IN_CONTEXT = 20
# 超过这个数量必须给选择策略（这里用业务域分组），直接全量注入等于放弃选择
SOFT_TOOL_LIMIT = 15


class _ToolTimeout(Exception):
    """内部信号：工具执行超过契约声明的单次超时。不对外暴露，统一转成 timeout 错误码。"""


def _parse_arguments(
    arguments: str | dict[str, Any] | None,
) -> tuple[dict[str, Any], ToolError | None]:
    """把模型给的参数解析成字典。坏 JSON 归为参数错误，绝不让解析异常冒泡。

    :param arguments: 模型输出的 arguments，可能是 JSON 字符串、对象或 None
    :return: 二元组（解析后的字典, 错误）；错误非空时不得继续执行
    """
    # 1. 空参数按空对象处理：无参工具与模型漏传都会走到这里
    if arguments is None or arguments == "":
        return {}, None

    # 2. 已是对象直接放行，交给契约校验
    if isinstance(arguments, dict):
        return arguments, None

    # 3. 字符串必须是合法 JSON 对象，解析失败给可修正的提示
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError as exc:
            return {}, ToolError(
                code=ToolErrorCode.INVALID_ARGUMENT,
                message="arguments 不是合法 JSON：%s（位置 %d）" % (exc.msg, exc.pos),
                hint='传 {"参数名": 值} 形式的 JSON 对象',
            )
        if not isinstance(parsed, dict):
            return {}, ToolError(
                code=ToolErrorCode.INVALID_ARGUMENT,
                message="arguments 必须是 JSON 对象，解析结果是 %s" % type(parsed).__name__,
                hint='传 {"参数名": 值} 形式的 JSON 对象',
            )
        return parsed, None

    # 4. 其他类型是协议层错误，直接拒绝
    return {}, ToolError(
        code=ToolErrorCode.INVALID_ARGUMENT,
        message="arguments 类型不支持：%s" % type(arguments).__name__,
        hint="按工具 schema 传 JSON 对象",
    )


class ToolRegistry:
    """工具注册表：登记契约、导出 schema、统一执行。

    执行入口只做四件事：定位工具、校验参数、判人工确认、带超时调用。
    重试、循环控制、结果入上下文一律不在这里。
    """

    def __init__(self, settings: Settings, logger: StructuredLogger | None = None):
        """建立空注册表。

        :param settings: 运行配置，提供工具超时天花板、项目根与幂等存储配置
        :param logger: 结构化日志器，用于记录未分类异常；为空则不记录
        """
        self._s = settings
        self._log = logger
        self._specs: dict[str, ToolSpec] = {}
        # 幂等存储在装配期建好：写类工具的重复保护必须是默认行为，不能靠调用方记得传
        self._idem = (
            FileIdempotencyStore(
                settings.project_root / settings.idempotency.dir,
                settings.idempotency.window_s,
            )
            if settings.idempotency.enabled
            else None
        )

    # ---------- 登记与导出 ----------

    def register(self, spec: ToolSpec) -> None:
        """登记一个工具契约。

        :param spec: 契约卡，登记前会跑 check_spec 全量核对
        :raises ValueError: 契约不合格或工具名重复
        """
        # 1. 契约核对：九字段缺一即拒绝，不允许带病上线
        check_spec(spec)

        # 2. 唯一性：同名工具只允许一个实现，否则模型可能调用到不确定的那个
        if spec.name in self._specs:
            raise ValueError("工具 %s 已登记，禁止重复注册" % spec.name)

        # 3. 登记：此后 schema 导出与执行都以这张卡为准
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        """按名取契约卡，供调试与自检使用，不执行。

        :param name: 工具名
        :return: 契约卡，未登记返回 None
        """
        return self._specs.get(name)

    def names(self) -> list[str]:
        """列出已登记的工具名，用于错误提示里给模型可用清单。

        :return: 工具名列表，按登记顺序
        """
        return list(self._specs)

    def schemas(self, groups: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """导出给模型的工具清单（OpenAI tools 格式）。

        :param groups: 只导出这些业务域的工具；None 表示全量
        :return: tools 数组，可直接放进请求体
        :raises ValueError: 当轮注入数超硬上限，或全量导出时工具数超软上限
        """
        # 1. 按业务域筛选，决定这一轮模型能看见哪些工具
        specs = [s for s in self._specs.values() if groups is None or s.group in groups]

        # 2. 数量策略：硬上限一律拒绝；软上限要求显式分组，禁止一次性全塞
        if len(specs) > MAX_TOOLS_IN_CONTEXT:
            raise ValueError(
                "本轮注入工具数 %d 超过硬上限 %d，必须按分组加载"
                % (len(specs), MAX_TOOLS_IN_CONTEXT)
            )
        if groups is None and len(self._specs) > SOFT_TOOL_LIMIT:
            raise ValueError(
                "工具数 %d 超过 %d，必须按业务域分组注入（schemas(groups=[...])）"
                % (len(self._specs), SOFT_TOOL_LIMIT)
            )

        # 3. 生成 schema：描述与枚举全部来自契约卡，不在这里二次加工
        return [build_function_schema(s) for s in specs]

    # ---------- 执行 ----------

    def invoke(
        self,
        name: str,
        arguments: str | dict[str, Any] | None,
        *,
        context: ToolContext,
    ) -> ToolOutcome:
        """执行一次工具调用。任何失败都转成结构化结果返回，不向主循环抛异常。

        :param name: 模型请求的工具名，可能是编造的、不存在的
        :param arguments: 模型给出的参数，可能是坏 JSON、缺字段或类型不符
        :param context: 调用上下文，提供会话、租户与人工确认开关
        :return: 成功或失败的结构化结果，含本次实际耗时
        """
        started = int(time.time() * 1000)

        # 1. 工具定位：模型编造工具名是常态，返回可用清单比抛异常有用
        spec = self._specs.get(name)
        if spec is None:
            return self._fail(
                started,
                ToolError(
                    code=ToolErrorCode.NOT_FOUND,
                    message="工具 %s 不存在" % name,
                    hint="可用工具：%s" % ("、".join(self.names()) or "无"),
                    field="name",
                ),
            )

        # 2. 参数解析：模型输出一律不可信，坏 JSON 不得冒泡成系统错误
        parsed, parse_error = _parse_arguments(arguments)
        if parse_error is not None:
            return self._fail(started, parse_error)

        # 3. 契约校验：类型、必填、枚举、正则，失败信息要能让模型自己改对
        cleaned, invalid = validate_arguments(spec, parsed)
        if invalid is not None:
            return self._fail(started, invalid)

        # 4. 人工确认：高危工具在确认开关打开时不下发执行，确认流程不在工具侧
        if spec.side_effect is SideEffect.DANGEROUS and context.require_approval:
            return self._fail(
                started,
                ToolError(
                    code=ToolErrorCode.APPROVAL_REQUIRED,
                    message="工具 %s 属高危操作，需人工确认后执行" % spec.name,
                    hint="把待确认内容整理给售后顾问，确认后再提交；不要自行重试",
                ),
            )

        # 5. 幂等准入：写类工具先抢幂等键，命中就复用首次结果，不再执行业务
        claim = Claim(status=ClaimStatus.ACQUIRED)
        call_context = context
        if spec.is_write and self._idem is not None:
            key = self._make_key(spec, cleaned, context)
            claim = self._idem.claim(key)
            if claim.status is ClaimStatus.HIT:
                # 幂等命中：返回首次结果，明确告知"这次没有真的写"
                return ToolOutcome(
                    ok=True,
                    data=claim.result or {},
                    elapsed_ms=int(time.time() * 1000) - started,
                    duplicated=True,
                )
            if claim.status is ClaimStatus.IN_FLIGHT:
                return self._fail(
                    started,
                    ToolError(
                        code=ToolErrorCode.CONFLICT,
                        message="同一请求正在处理中（工具 %s）" % spec.name,
                        hint="稍后退避重试；不要换参数重复提交，否则会产生多条记录",
                    ),
                )
            # 抢到执行权：把键交给工具，工具不得自己再造键
            call_context = replace(context, idempotency_key=key)

        # 6. 带超时执行：单次超时归工具层，重试归骨架
        try:
            data = self._run_with_timeout(spec, cleaned, call_context)
        except ToolFailure as exc:
            # 业务工具主动抛出的已分类失败：释放占位，让后续重试还能进来
            self._release(claim, call_context)
            return self._fail(started, exc.error)
        except _ToolTimeout:
            # 超时也释放：线程可能仍在后台跑，但它提交时会因令牌不符被丢弃
            self._release(claim, call_context)
            return self._fail(
                started,
                ToolError(
                    code=ToolErrorCode.TIMEOUT,
                    message="工具 %s 超过 %d ms 未返回" % (spec.name, self._timeout_ms(spec)),
                    hint="可稍后重试；若持续超时请转人工核查该接口",
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 这里必须兜住全部异常，否则会冒泡
            # 7. 未分类异常：记成实现缺陷，堆栈只进日志不进上下文
            self._release(claim, call_context)
            self._log_exception(spec.name, exc, context)
            return self._fail(
                started,
                ToolError(
                    code=ToolErrorCode.INTERNAL_ERROR,
                    message="工具 %s 执行失败（%s）" % (spec.name, type(exc).__name__),
                    hint="这是实现缺陷，不要重试，请转人工处理",
                ),
            )

        # 8. 成功：先提交幂等结果，再把保留键提到 duplicated
        duplicated = bool(data.pop("_duplicated", False))
        if call_context.idempotency_key and self._idem is not None:
            self._idem.commit(call_context.idempotency_key, claim.token, data)
        return ToolOutcome(
            ok=True,
            data=data,
            elapsed_ms=int(time.time() * 1000) - started,
            duplicated=duplicated,
        )

    def _make_key(self, spec: ToolSpec, arguments: dict[str, Any], context: ToolContext) -> str:
        """按契约声明的业务键字段拼出幂等键。

        :param spec: 工具契约卡，提供幂等键字段顺序
        :param arguments: 已通过契约校验的参数
        :param context: 调用上下文，提供会话 id
        :return: 幂等键字符串
        """
        parts = [str(arguments.get(name, "")) for name in spec.idempotency_key_fields]
        return context.make_idempotency_key(spec.name, "|".join(parts))

    def _release(self, claim: Claim, context: ToolContext) -> None:
        """失败路径释放幂等占位，避免一次失败把同一业务永久锁死。

        :param claim: 抢占结果，未抢到执行权时不做任何事
        :param context: 调用上下文，提供幂等键
        """
        if claim.status is not ClaimStatus.ACQUIRED or self._idem is None:
            return
        if context.idempotency_key:
            self._idem.release(context.idempotency_key, claim.token)

    def _run_with_timeout(
        self, spec: ToolSpec, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        """在守护线程里执行工具并限时等待结果。

        WHY: 用守护线程而不是线程池——线程池关闭时会等待任务跑完，等于把刚设的超时抵消掉。
        超时只能"放弃等待"，Python 无法强杀线程，工具可能仍在后台执行；
        这正是写类工具必须有幂等键的原因，重复执行必须收敛到同一结果。

        :param spec: 目标工具契约卡，提供执行体与单次超时
        :param arguments: 已通过契约校验的参数
        :param context: 调用上下文
        :return: 工具返回的业务数据
        :raises _ToolTimeout: 超过单次超时仍未返回
        :raises ToolFailure: 工具主动抛出的已分类失败
        :raises Exception: 工具内部的未分类异常，由调用方归一化
        """
        box: dict[str, Any] = {}
        done = threading.Event()

        def target() -> None:
            """执行工具并把结果或异常放进 box。线程内必须全捕获，否则异常丢失只能等超时。"""
            try:
                box["data"] = spec.handler(arguments, context)
            except BaseException as exc:  # noqa: BLE001 - 线程内不兜住就没有第二次机会
                box["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(target=target, name="tool-%s" % spec.name, daemon=True)
        worker.start()
        if not done.wait(self._timeout_ms(spec) / 1000.0):
            raise _ToolTimeout()
        if "error" in box:
            raise box["error"]
        return box["data"] or {}

    def _timeout_ms(self, spec: ToolSpec) -> int:
        """算出本次调用的实际超时：契约声明与配置天花板取较小者。

        WHY: 配置是全局兜底，契约只能更严不能更松；确实需要更长的工具，
        必须同时上调配置并写明理由，避免单个工具悄悄拖死整轮任务。
        """
        ceiling = self._s.timeout.tool_ms
        if ceiling <= 0:
            return spec.timeout_ms
        return min(spec.timeout_ms, ceiling)

    def _fail(self, started: int, error: ToolError) -> ToolOutcome:
        """构造失败结果，统一补齐耗时字段。

        :param started: 调用开始时间戳（毫秒）
        :param error: 三段式错误
        :return: ok=False 的结果
        """
        return ToolOutcome(ok=False, error=error, elapsed_ms=int(time.time() * 1000) - started)

    def _log_exception(self, tool_name: str, exc: Exception, context: ToolContext) -> None:
        """把未分类异常写进结构化日志。堆栈只在这里出现，不进模型上下文。

        :param tool_name: 出错工具名，用于定位
        :param exc: 原始异常
        :param context: 调用上下文，提供步序号
        """
        if self._log is None:
            return
        self._log.log(
            step_no=context.state.step_no,
            tool_name=tool_name,
            result_code=ToolErrorCode.INTERNAL_ERROR.value,
            event="tool_exception",
            extra={
                "exception": type(exc).__name__,
                # 截断堆栈：日志有上限，定位够用即可
                "traceback": traceback.format_exc()[-800:],
            },
        )
