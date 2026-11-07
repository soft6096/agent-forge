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
from dataclasses import dataclass, field

from ..llm import ReplayProvider
from ..loop import AgentLoop, TaskResult
from ..ops.logging import StructuredLogger
from ..settings import Settings, load_settings
from ..tools.builtin import build_registry
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


def run_case(case: Case, settings: Settings) -> CaseResult:
    """跑一条用例并判定。

    :param case: 用例，含输入、判定条件与是否预置中断
    :param settings: 运行配置，保证评测与线上用同一套预算与超时
    :return: 判定结果，失败项已归因，含真实输入与输出
    """
    # 1. 每条用例独立 Provider：录制游标必须隔离，否则用例之间互相偷轮次
    provider = ReplayProvider(settings.project_root / "evals" / "replay")

    # 2. 日志丢弃到内存：评测不需要逐条日志，但也不该污染终端
    logger = StructuredLogger(session_id="eval-" + case.case_id, stream=io.StringIO())

    # 3. 装配主循环：与线上同一套代码，只把 Provider 换成回放
    registry = build_registry(settings, logger=logger)
    loop = AgentLoop(settings=settings, provider=provider, logger=logger, tools=registry)

    # 4. 中断分支：先置标志，让第一步检查点就命中 cancelled
    if case.cancel_before_run:
        loop.request_cancel()

    # 5. 跑任务并判定
    result = loop.run(case.input_text)
    failures = _judge(case, result)

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
    )


def _judge(case: Case, result: TaskResult) -> list[str]:
    """按用例判定条件核对结果。

    :param case: 用例，提供期望值
    :param result: 主循环返回的实际结果
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

    # 3. 工具使用：期望工具必须都被调用过
    used = set(result.session.used_tools)
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

    # 5. 工具错误码：按集合相等判定，多报少报都算问题
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

        # 2. 解析失败直接跳过：回填内容本身是骨架写的，坏了属于实现问题不是判定问题
        try:
            payload = json.loads(str(message.get("content") or "{}"))
        except json.JSONDecodeError:
            continue

        # 3. 只有 ok=false 且带 error 码的消息才算错误
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
            print("  调用工具: %s" % ("、".join(item.tools_used) or "无"))
            print("  工具错误码: %s" % ("、".join(item.tool_errors) or "无"))
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
                    "tools_used": item.tools_used,
                    "tool_errors": item.tool_errors,
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
