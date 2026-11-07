"""会话状态：可完整序列化，用于中断恢复。

硬规则：状态是事实（做过什么、花了多少），不是"这一轮给模型看什么"。
上下文由 context 层投影，状态本身不参与裁剪。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SessionState:
    """一次任务的全部事实，能落 JSON 也能原样恢复。"""

    session_id: str
    user_input: str
    step_no: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    used_tools: list[str] = field(default_factory=list)
    pending_confirmation: dict[str, Any] | None = None
    status: str = "running"

    @classmethod
    def new(cls, user_input: str) -> "SessionState":
        """开新会话。session_id 用于日志关联与幂等键前缀。"""
        return cls(session_id=uuid.uuid4().hex[:12], user_input=user_input)

    def add_message(self, role: str, content: str, **extra: Any) -> None:
        """追加一条消息。

        :param role: 消息角色，取值 system / user / assistant / tool
        :param content: 消息正文；工具调用回填时是序列化后的结果 JSON
        :param extra: 协议附加字段，如 tool_calls（assistant 发起调用）、
                      tool_call_id 与 name（tool 回填时归属哪个调用）；
                      值为 None 的字段会被丢弃，避免写进 null 干扰模型
        """
        # 1. 组装基础结构：role 与 content 固定，不允许被 extra 覆盖
        message: dict[str, Any] = {"role": role, "content": content}

        # 2. 合并协议字段：丢掉 None，其余原样保留，保证 tool 结果能归属到具体调用
        message.update({key: value for key, value in extra.items() if value is not None})

        # 3. 追加：历史裁剪由 context 层做，这里只负责记录事实
        self.messages.append(message)

    def mark_tool(self, name: str) -> None:
        """记录已用工具，既用于恢复也用于死循环识别。"""
        self.used_tools.append(name)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: Path) -> None:
        """落快照：中断后靠它恢复，不允许只存在内存里。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "SessionState":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)

    def idempotency_key(self, tool_name: str, biz_key: str) -> str:
        """写类工具的幂等键：绑定会话、工具与业务键。

        WHY: 键里故意不含步序号——同一业务在跨步重试时步序会变，含步序等于每次都是新键，
        幂等直接失效。也不允许随机 UUID，那样每次重试都会产生新键。

        :param tool_name: 工具名，用于区分不同写操作
        :param biz_key: 业务键（如 vin|零件|单据类型），同一业务必须稳定
        :return: 幂等键字符串
        """
        return "%s:%s:%s" % (self.session_id, tool_name, biz_key)
