"""人工确认入口：列出待确认的高危操作，确认执行或拒绝。

这是唯一能放行高危操作的通道，模型与主循环都无法调用它——确认动作必须由人执行。
每次确认都写回确认项文件并留结构化日志，可回溯到原会话与步序号。

用法：
    PYTHONPATH=src python3 -m tga.confirm --list
    PYTHONPATH=src python3 -m tga.confirm --id <确认令牌>
    PYTHONPATH=src python3 -m tga.confirm --id <确认令牌> --reject
"""

from __future__ import annotations

import argparse
import json
import sys

from .ops.logging import StructuredLogger
from .safety.approval import STATUS_REJECTED, ApprovalStore
from .settings import Settings, load_settings
from .state import SessionState
from .tools.builtin import build_registry
from .tools.contract import ToolContext


def main(argv: list[str] | None = None) -> int:
    """解析参数并处理一次确认动作。

    :param argv: 命令行参数，None 表示取 sys.argv
    :return: 退出码：0 成功 / 1 执行失败 / 2 参数或确认项错误
    """
    parser = argparse.ArgumentParser(description="高危操作人工确认（模型不得调用）")
    parser.add_argument("--list", action="store_true", help="列出待确认操作")
    parser.add_argument("--id", default=None, help="确认令牌")
    parser.add_argument("--reject", action="store_true", help="拒绝该操作，不执行")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except (OSError, ValueError, KeyError) as exc:
        print("配置加载失败：%s" % exc, file=sys.stderr)
        return 2

    store = ApprovalStore(
        settings.project_root / "runtime" / "approvals", settings.safety.approval_ttl_s
    )

    # 1. 列出待确认项
    if args.list:
        return _print_pending(store)

    # 2. 参数校验：必须给出令牌
    if not args.id:
        parser.error("需要 --list 或 --id <确认令牌>")

    action = store.load(args.id)
    if action is None:
        print("确认项不存在：%s" % args.id, file=sys.stderr)
        return 2

    # 3. 拒绝：只改状态，不执行
    if args.reject:
        store.close(action, STATUS_REJECTED)
        print("已拒绝 %s（%s），未执行。" % (action.action_id, action.tool_name))
        return 0

    # 4. 确认执行：沿用原会话 id，保证幂等键与首次尝试一致，不会重复建单
    return _execute(action, settings)


def _print_pending(store: ApprovalStore) -> int:
    """打印仍可确认的操作。

    :param store: 确认项存储
    :return: 退出码，恒为 0（没有待确认项也是正常结果）
    """
    pending = store.list_pending()

    # 1. 空列表明确提示，避免让人以为命令没生效
    if not pending:
        print("当前没有待确认的高危操作。")
        return 0

    # 2. 逐条输出：令牌、工具、摘要、来源会话与步序
    for action in pending:
        print(
            "%-14s %-22s %s（会话 %s 第 %d 步）"
            % (action.action_id, action.tool_name, action.summary, action.session_id, action.step_no)
        )
    return 0


def _execute(action, settings: Settings) -> int:
    """执行一条已确认的高危操作。

    :param action: 确认项，含工具名、参数与原会话 id
    :param settings: 运行配置
    :return: 退出码，0 成功 / 1 执行失败
    """
    # 1. 恢复会话上下文：session_id 必须沿用，否则幂等键会变、可能重复落库
    state = SessionState(session_id=action.session_id, user_input="[人工确认] %s" % action.summary)
    state.step_no = action.step_no

    # 2. 组装工具上下文，确认令牌从这里进入注册表；模型侧永远拿不到它
    context = ToolContext(
        state=state,
        tenant_id="default",
        project_root=settings.project_root,
        require_approval=settings.safety.require_approval,
        retrieval=settings.retrieval,
        approval_id=action.action_id,
    )

    # 3. 执行：与线上同一套注册表，参数校验、幂等、超时一律照走
    logger = StructuredLogger(session_id=action.session_id)
    registry = build_registry(settings, logger=logger)
    outcome = registry.invoke(action.tool_name, action.arguments, context=context)

    # 4. 审计留痕：确认动作本身必须可回溯，成功与否都记录
    logger.log(
        step_no=action.step_no,
        tool_name=action.tool_name,
        result_code="ok" if outcome.ok else (outcome.error.code.value if outcome.error else "error"),
        event="approval_executed",
        extra={"action_id": action.action_id, "duplicated": outcome.duplicated},
    )

    # 5. 输出结果
    if outcome.ok:
        print(json.dumps({"ok": True, "data": outcome.data, "duplicated": outcome.duplicated},
                         ensure_ascii=False, indent=2))
        return 0

    print(
        json.dumps(
            {"ok": False, "error": outcome.error.to_dict() if outcome.error else {}},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
