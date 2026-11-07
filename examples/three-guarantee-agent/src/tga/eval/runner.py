"""评测入口：跑评测集，输出判定结果与失败归因。

默认不执行：本模块只在显式执行时才会跑评测，main 与任何主流程都不会自动触发；
需要跑时先问开发人员，得到明确同意再执行，避免无谓的时间与 token 消耗。

评测固定走 replay，不读 OPENAI_API_KEY：评测必须可复现，也不能因为跑评测产生费用。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from dataclasses import dataclass, field, replace
from typing import Any

from ..llm import ReplayProvider
from ..loop import AgentLoop, TaskResult
from ..ops.logging import StructuredLogger
from ..safety.approval import ApprovalStore
from ..safety.filters import strip_external
from ..settings import Settings, load_settings
from ..state import SessionState
from ..tools.builtin import build_registry
from ..tools.contract import ToolContext
from .cases import Case, load_cases


@dataclass
class CaseResult:
    """单条用例的执行结果与判定明细。失败项逐条列出，便于直接归因。"""

    case_id: str
    passed: bool
    failures: list[str]  # 未命中的判定项，空表示通过
    reason: str          # 实际终止原因
    steps: int           # 实际消耗步数
    answer: str          # 实际答复，失败样本要能直接看到
    input_text: str = ""                                   # 真实输入，失败归因必备
    tools_used: list[str] = field(default_factory=list)   # 实际调用过的工具
    tool_errors: list[str] = field(default_factory=list)  # 工具返回的错误码
    note: str = ""       # 用例覆盖说明
    detail: str = ""     # 终止说明，非 completed 分支的失败只能从这里看
    approvals: list[dict[str, Any]] = field(default_factory=list)  # 模拟人工确认的执行结果


def run_case(case: Case, settings: Settings) -> CaseResult:
    """跑一条用例并判定。

    :param case: 用例，含输入、判定条件与是否预置中断
    :param settings: 运行配置，保证评测与线上用同一套预算与超时
    :return: 判定结果，失败项已归因，含真实输入与输出
    """
    # 1. 用例级配置覆盖：极端场景只对这条用例成立，不改全局配置
    settings = _with_override(settings, case)

    # 2. 每条用例独立 Provider：录制游标必须隔离，否则用例之间互相偷轮次
    provider = ReplayProvider(settings.project_root / "evals" / "replay")

    # 3. 日志丢弃到内存：评测不需要逐条日志，但也不该污染终端
    logger = StructuredLogger(session_id="eval-" + case.case_id, stream=io.StringIO())

    # 4. 装配主循环：与线上同一套代码，只把 Provider 换成回放
    registry = build_registry(settings, logger=logger)
    loop = AgentLoop(settings=settings, provider=provider, logger=logger, tools=registry)

    # 5. 中断分支：先置标志，让第一步检查点就命中 cancelled
    if case.cancel_before_run:
        loop.request_cancel()

    # 6. 跑任务，再按用例要求走一遍人工确认通道
    result = loop.run(case.input_text)
    approvals = _confirm_pending(case, result, settings, logger)
    failures = _judge(case, result, approvals)

    return CaseResult(
        case_id=case.case_id,
        passed=not failures,
        failures=failures,
        reason=result.reason.value,
        steps=result.steps,
        answer=result.answer,
        input_text=case.input_text,
        tools_used=list(result.session.used_tools),
        tool_errors=_tool_error_codes(result),
        note=case.note,
        detail=result.detail,
        approvals=approvals,
    )


def _with_override(settings: Settings, case: Case) -> Settings:
    """把用例级配置覆盖应用到本次运行。

    WHY: 极端场景（如把窗口压到极小验证降级）只应影响这一条用例，
    改全局配置会让同批次其他用例一起失真，失败归因也就无从下手。

    :param settings: 全局配置，只读
    :param case: 用例，可能带 context_override
    :return: 覆盖后的新配置；没有覆盖时原样返回
    """
    # 1. 无覆盖直接返回，避免无谓的对象拷贝
    if not case.context_override:
        return settings

    # 2. 只覆盖上下文预算表：为什么不用 validate() 复检——极端场景本身就是故意越界，
    #    复检会把"要验证的降级路径"变成装配失败，用例永远跑不起来
    return replace(settings, context=replace(settings.context, **case.context_override))


def _confirm_pending(
    case: Case,
    result: TaskResult,
    settings: Settings,
    logger: StructuredLogger,
) -> list[dict[str, Any]]:
    """模拟人工确认：把本会话产生的待确认项按真人流程执行一遍。

    这是评测里唯一允许"开闸"的地方——主循环与模型永远拿不到确认令牌，
    只有用例显式声明 confirm_pending 才代表"此处有人确认过"，用于验证开闸后真能落地。

    :param case: 用例，confirm_pending 为 False 时直接返回空
    :param result: 主循环结果，用会话 id 收敛待确认项
    :param settings: 运行配置，提供确认项有效期与项目根
    :param logger: 结构化日志器，确认动作必须留痕
    :return: 每条确认项的执行结果；空列表表示本次没有产生待确认项
    """
    # 1. 未声明的用例一律不动确认项，避免把"该转人工"的用例悄悄放行
    if not case.confirm_pending:
        return []

    # 2. 只处理本会话产生的确认项：runtime 目录是共用的，别的会话的绝不能碰
    store = ApprovalStore(
        settings.project_root / "runtime" / "approvals", settings.safety.approval_ttl_s
    )
    pending = [item for item in store.list_pending() if item.session_id == result.session.session_id]

    # 3. 逐条执行：沿用原会话 id 与步序号，幂等键不变，重复确认不会落两条记录
    registry = build_registry(settings, logger=logger)
    executed: list[dict[str, Any]] = []
    for action in pending:
        state = SessionState(
            session_id=action.session_id, user_input="[评测模拟人工确认] %s" % action.summary
        )
        state.step_no = action.step_no
        context = ToolContext(
            state=state,
            tenant_id="default",
            project_root=settings.project_root,
            require_approval=settings.safety.require_approval,
            retrieval=settings.retrieval,
            approval_id=action.action_id,
        )
        outcome = registry.invoke(action.tool_name, action.arguments, context=context)
        executed.append(
            {
                "action_id": action.action_id,
                "tool": action.tool_name,
                "ok": outcome.ok,
                "error": outcome.error.code.value if outcome.error else "",
            }
        )

        # 4. 审计留痕：确认动作本身要可回溯到用例、会话与步序
        logger.log(
            step_no=action.step_no,
            tool_name=action.tool_name,
            result_code=(
                "ok" if outcome.ok else (outcome.error.code.value if outcome.error else "error")
            ),
            event="approval_executed",
            extra={"action_id": action.action_id, "case_id": case.case_id},
        )

    return executed


def _judge(case: Case, result: TaskResult, approvals: list[dict[str, Any]]) -> list[str]:
    """按用例判定条件核对结果。

    :param case: 用例，提供期望值
    :param result: 主循环返回的实际结果
    :param approvals: 模拟人工确认的执行结果，仅 confirm_pending 用例非空
    :return: 失败项列表；空列表表示全部命中
    """
    failures: list[str] = []
    expect = case.expect

    # 1. 终止原因：五类之一必须精确匹配，不能用"跑完就算过"糊弄
    if result.reason.value != expect.reason:
        failures.append("终止原因 期望 %s 实际 %s" % (expect.reason, result.reason.value))

    # 2. 步数：超过上限说明失控，即便结论对也不算通过
    if result.steps > expect.max_steps:
        failures.append("步数 期望 <= %d 实际 %d" % (expect.max_steps, result.steps))

    # 3. 工具使用：期望工具必须都被调用过；确认通道里执行过的工具同样算调过
    #    WHY 不看执行成败：与主循环口径一致（会话里的 used_tools 也记录失败调用），
    #    否则一次失败会同时刷出"没调过"和"确认执行失败"两条互相打脸的结论
    used = set(result.session.used_tools) | {item["tool"] for item in approvals}
    missing = [name for name in expect.tools_used if name not in used]
    if missing:
        failures.append(
            "未调用期望工具 %s（实际 %s）"
            % ("、".join(missing), "、".join(sorted(used)) or "无")
        )

    # 4. 答复片段：必须出现的要有，禁止出现的不能有（如编造的条款编号）
    for token in expect.answer_contains:
        if token not in result.answer:
            failures.append("答复缺少 %r" % token)
    for token in expect.answer_not_contains:
        if token in result.answer:
            failures.append("答复出现禁止片段 %r" % token)

    # 5. 终止说明：非 completed 分支的结论全在 detail 里，必须能对上关键片段
    for token in expect.detail_contains:
        if token not in result.detail:
            failures.append("终止说明缺少 %r（实际 %r）" % (token, result.detail))

    # 6. 人工确认通道：声明需确认的用例必须真产生待确认项，且开闸后执行成功
    if case.confirm_pending:
        if not approvals:
            failures.append("期望产生待确认的高危操作，实际没有（确认开关或工具高危标记可能没生效）")
        for item in approvals:
            if not item["ok"]:
                failures.append(
                    "人工确认后执行失败 %s（%s）" % (item["tool"], item["error"] or "error")
                )

    # 7. 工具错误码：按集合相等判定，多报少报都算问题
    actual_errors = _tool_error_codes(result)
    if set(expect.tool_error_codes) != set(actual_errors):
        failures.append(
            "工具错误码 期望 %s 实际 %s"
            % (sorted(expect.tool_error_codes), sorted(actual_errors))
        )

    return failures


def _tool_error_codes(result: TaskResult) -> list[str]:
    """从会话的 tool 消息里收集错误码，用于判定错误分支是否真被覆盖。

    :param result: 主循环结果
    :return: 出现过的工具错误码列表，按出现顺序
    """
    codes: list[str] = []

    # 1. 逐条扫 tool 消息
    for message in result.session.messages:
        if message.get("role") != "tool":
            continue

        # 2. 先剥掉外部内容分隔符：开启了内容隔离时，tool 消息外面会包一层数据标记
        raw = strip_external(str(message.get("content") or ""))

        # 3. 解析失败直接跳过：回填内容本身是骨架写的，坏了属于实现问题不是判定问题
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue

        # 4. 只有 ok=false 且带 error 码的消息才算错误
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict) and error.get("error"):
            codes.append(str(error["error"]))

    return codes


def _check_only(cases: list[Case], settings: Settings) -> int:
    """只校验用例与录制是否配齐，不跑模型。

    :param cases: 待校验的用例
    :param settings: 运行配置，用于定位录制目录
    :return: 退出码，0 表示全部用例都有录制
    """
    provider = ReplayProvider(settings.project_root / "evals" / "replay")
    missing = 0

    # 1. 逐条查录制轮次
    for case in cases:
        # 免录制用例：纯终止分支根本不调模型，按缺录制算失败会把正常用例判成红的
        if not case.requires_recording:
            print("%-28s %-8s %-10s %s" % (case.case_id, "免录制", "轮次=-", case.note))
            continue
        turns = provider.recording_turns(case.input_text)
        state = "ok" if turns > 0 else "缺录制"
        if turns == 0:
            missing += 1
        print("%-28s %-8s 轮次=%d  %s" % (case.case_id, state, turns, case.note))

    # 2. 汇总：缺录制必须挡住，否则跑起来只会得到"未找到录制"的假结论
    print("用例 %d 条，缺录制 %d 条" % (len(cases), missing))
    return 0 if missing == 0 else 1


def _print_report(results: list[CaseResult], settings: Settings) -> int:
    """打印人读报告并给出退出码。

    :param results: 全部用例结果
    :param settings: 运行配置，提供门禁阈值
    :return: 退出码，0 全绿 / 1 有失败
    """
    passed = sum(1 for item in results if item.passed)
    rate = passed / len(results) if results else 0.0

    # 1. 逐条结果一行，先看整体
    print("%-30s %-6s %-15s %-6s %s" % ("用例", "结果", "终止原因", "步数", "说明"))
    for item in results:
        print(
            "%-30s %-6s %-15s %-6d %s"
            % (item.case_id, "PASS" if item.passed else "FAIL", item.reason, item.steps, item.note)
        )

    # 2. 汇总与门禁：冒烟集要求全绿，其余按配置阈值
    all_green = passed == len(results)
    print(
        "\n通过 %d/%d（%.1f%%），门禁阈值 %.0f%%，冒烟集要求全绿"
        % (passed, len(results), rate * 100, settings.eval.gate_min_pass_rate * 100)
    )

    # 3. 失败样本：带真实输入输出与归因，缺这些的报告无法复盘
    failed = [item for item in results if not item.passed]
    if failed:
        print("\n失败样本：")
        for item in failed:
            print("- %s（%s）" % (item.case_id, item.note))
            print("  输入: %s" % item.input_text)
            print("  实际终止原因: %s，步数: %d" % (item.reason, item.steps))
            print("  实际答复: %s" % (item.answer or "（空）"))
            # 非 completed 分支答复是空的，结论全在终止说明里，不打印就无法归因
            if item.detail and not item.answer:
                print("  终止说明: %s" % item.detail)
            print("  调用工具: %s" % ("、".join(item.tools_used) or "无"))
            print("  工具错误码: %s" % ("、".join(item.tool_errors) or "无"))
            if item.approvals:
                print(
                    "  人工确认: %s"
                    % "；".join(
                        "%s %s%s"
                        % (
                            one["tool"],
                            "成功" if one["ok"] else "失败",
                            "" if one["ok"] else "（%s）" % (one["error"] or "error"),
                        )
                        for one in item.approvals
                    )
                )
            for failure in item.failures:
                print("  未命中: %s" % failure)

    return 0 if all_green else 1


def main(argv: list[str] | None = None) -> int:
    """评测入口。默认不执行，只有显式调用才会跑。

    :param argv: 命令行参数，None 表示取 sys.argv
    :return: 退出码，0 全绿 / 1 有失败 / 2 用例或环境错误
    """
    parser = argparse.ArgumentParser(
        description="三包判定助手评测（默认不自动执行，跑之前先确认）"
    )
    parser.add_argument("--suite", default="smoke", help="用例集名称，默认 smoke")
    parser.add_argument("--case", default=None, help="只跑指定用例 id")
    parser.add_argument("--json", action="store_true", help="输出机读 JSON")
    parser.add_argument(
        "--check-only", action="store_true", help="只校验用例与录制是否配齐，不跑模型"
    )
    args = parser.parse_args(argv)

    # 1. 装配：用例或配置有问题时以退出码 2 结束，不执行任何模型调用
    try:
        settings = load_settings()
        if args.suite == "smoke":
            suite_dir = settings.project_root / settings.eval.smoke_dir
        else:
            suite_dir = settings.project_root / "evals" / args.suite
        cases = load_cases(suite_dir, only=args.case)
    except (OSError, ValueError, KeyError) as exc:
        print("评测装配失败：%s" % exc, file=sys.stderr)
        return 2

    # 2. 只校验模式：秒级完成，用于交付前确认录制没缺
    if args.check_only:
        return _check_only(cases, settings)

    # 3. 执行
    results = [run_case(case, settings) for case in cases]

    # 4. 输出：机读给人看分两条路，判定口径完全一致
    if args.json:
        payload = {
            "suite": args.suite,
            "total": len(results),
            "passed": sum(1 for item in results if item.passed),
            "results": [
                {
                    "case_id": item.case_id,
                    "passed": item.passed,
                    "reason": item.reason,
                    "steps": item.steps,
                    "input": item.input_text,
                    "answer": item.answer,
                    "detail": item.detail,
                    "tools_used": item.tools_used,
                    "tool_errors": item.tool_errors,
                    "approvals": item.approvals,
                    "failures": item.failures,
                    "note": item.note,
                }
                for item in results
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if all(item.passed for item in results) else 1

    return _print_report(results, settings)


if __name__ == "__main__":
    sys.exit(main())
