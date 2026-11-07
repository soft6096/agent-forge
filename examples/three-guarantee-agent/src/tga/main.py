"""入口：加载配置 → 选 Provider → 跑一次任务 → 输出结构化结果。

用法：
    PYTHONPATH=src python3 -m tga.main --input "VIN 为 LSVAA1234，变速箱异响是否在保"
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys

from .llm import build_provider
from .loop import AgentLoop, TermReason
from .ops.logging import StructuredLogger
from .settings import load_settings
from .state import SnapshotError
from .tools.builtin import build_registry


def main(argv: list[str] | None = None) -> int:
    """解析参数并跑一次任务。退出码：0 完成 / 1 未完成 / 2 参数或环境错误。"""
    parser = argparse.ArgumentParser(description="售后三包判定助手")
    parser.add_argument("--input", default=None, help="用户输入原文（新会话必填）")
    parser.add_argument(
        "--resume",
        default=None,
        metavar="SESSION_ID",
        help="恢复指定会话：从最后完成步的下一步继续，不重跑已完成工具",
    )
    parser.add_argument("--config", default=None, help="配置文件路径，默认 config/agent.toml")
    parser.add_argument("--no-log", action="store_true", help="不输出结构化日志（测试用）")
    args = parser.parse_args(argv)

    # 0. 新会话与恢复二选一：同时给或都不给都是用法错误，不能靠猜意图
    if bool(args.input) == bool(args.resume):
        print("参数错误：--input 与 --resume 必须且只能给一个", file=sys.stderr)
        return 2

    try:
        settings = load_settings(args.config)
        # 提示词版本校验放在启动期：等到主循环里才发现不一致，任务已经开跑了
        if not settings.prompt_text:
            raise ValueError("提示词正文为空：config/prompts/system.md")
    except (OSError, ValueError, KeyError) as exc:
        print("配置加载失败：%s" % exc, file=sys.stderr)
        return 2

    sink = open(os.devnull, "w", encoding="utf-8") if args.no_log else None
    try:
        logger = StructuredLogger(session_id="pending", stream=sink)
        provider = build_provider(settings)
        # 1. 工具在启动期装配：契约不合格在这里就报错，而不是等模型真的调起工具才炸
        registry = build_registry(settings, logger=logger)
        # 2. 主循环拿注册表，工具超时、校验与回填都由它统一处理
        loop = AgentLoop(settings=settings, provider=provider, logger=logger, tools=registry)

        # 3. 中断信号：第一次交给主循环在检查点处理，第二次直接退出。
        #    WHY: 卡在模型调用里时检查点要等 LLM 超时才到，连按两次是用户明确的"立刻停"
        interrupt_count = 0

        def _on_sigint(*_: object) -> None:
            nonlocal interrupt_count
            interrupt_count += 1
            if interrupt_count >= 2:
                print(
                    "已强制退出（第二次中断），快照保留到上一个步边界",
                    file=sys.stderr,
                )
                os._exit(130)
            loop.request_cancel()

        signal.signal(signal.SIGINT, _on_sigint)

        # 4. 跑一次任务：新会话或从快照续跑，两条路共用同一套主循环
        result = loop.run(args.input) if args.input else loop.run_resume(args.resume)
    except SnapshotError as exc:
        # 恢复失败必须显性报错：静默新建会话会让用户以为在续跑，实际从头重跑
        print("会话恢复失败：%s" % exc, file=sys.stderr)
        return 2
    except ValueError as exc:
        print("启动失败（工具契约或配置不合法）：%s" % exc, file=sys.stderr)
        return 2
    finally:
        # /dev/null 句柄也要关：不关会在进程退出前一直占着一个 fd
        if sink is not None:
            sink.close()

    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.reason is TermReason.COMPLETED else 1


if __name__ == "__main__":
    sys.exit(main())
