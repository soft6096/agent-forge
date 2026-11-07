"""结构化日志与 trace。

固定七个字段：session_id、trace_id、step_no、tool_name、tokens、elapsed_ms、result_code。
缺 session_id / trace_id 就无法把一次问题复盘串起来；缺 tokens 就无法核账。
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Span:
    """span 的最小实现：一次模型调用或工具调用一个 span。"""

    name: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    parent_span_id: str | None = None
    start_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    elapsed_ms: int = 0
    status: str = "ok"

    def finish(self, status: str = "ok") -> None:
        """结束 span 并记录状态；异常路径也必须走到这里，否则时延统计缺一段。"""
        self.elapsed_ms = int(time.time() * 1000) - self.start_ms
        self.status = status


class StructuredLogger:
    """按固定字段输出 JSON 行日志，便于采集与检索。"""

    def __init__(self, session_id: str, trace_id: str | None = None, stream=None):
        self.session_id = session_id
        self.trace_id = trace_id or uuid.uuid4().hex[:12]
        self._stream = stream or sys.stderr
        self._spans: list[Span] = []

    def child_span(self, name: str, parent: Span | None = None) -> Span:
        """开一个子 span，父 span 缺省时挂在 trace 根上。"""
        span = Span(name=name, parent_span_id=parent.span_id if parent else None)
        self._spans.append(span)
        return span

    def log(
        self,
        *,
        step_no: int,
        tool_name: str = "llm",
        tokens: dict[str, int] | None = None,
        elapsed_ms: int = 0,
        result_code: str = "ok",
        event: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        """打一条结构化日志。七个必填字段不允许缺省或写默认值糊弄。"""
        record: dict[str, Any] = {
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "step_no": step_no,
            "tool_name": tool_name,
            "tokens": tokens or {"prompt": 0, "completion": 0},
            "elapsed_ms": elapsed_ms,
            "result_code": result_code,
        }
        if event:
            record["event"] = event
        if extra:
            record.update(extra)
        self._stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._stream.flush()
