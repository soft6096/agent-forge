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
from .tools.builtin import build_registry


def main(argv: list[str] | None = None) -> int:
    """解析参数并跑一次任务。退出码：0 完成 / 1 未完成 / 2 参数或环境错误。"""
    parser = argparse.ArgumentParser(description="售后三包判定助手")
    parser.add_argument("--input", required=True, help="用户输入原文")
    parser.add_argument("--config", default=None, help="配置文件路径，默认 config/agent.toml")
    parser.add_argument("--no-log", action="store_true", help="不输出结构化日志（测试用）")
    args = parser.parse_args(argv)

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

        # 3. 中断信号：交给主循环在检查点处理，不在信号处理里直接退出
        signal.signal(signal.SIGINT, lambda *_: loop.request_cancel())

        # 4. 跑一次任务
        result = loop.run(args.input)
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
