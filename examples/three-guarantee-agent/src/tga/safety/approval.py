"""高危操作的人工确认：待确认项落盘、确认后凭令牌执行、全程留痕。

为什么必须有这一层：注册表能拦住"模型自己执行"，但拦完之后若没有开闸通道，
高危工具就永远只能回"请人工处理"，功能等于缺失；反过来直接放行，
模型一句"用户已确认"就能绕过——确认动作必须由人触发，且可回溯到会话与步序号。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# 待确认状态取值：只有 pending 可以执行，其余一律拒绝，避免重复执行已关闭的确认项
STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"


@dataclass(frozen=True)
class PendingAction:
    """一条待人工确认的高危操作。参数指纹保证确认期间参数不可被替换。"""

    action_id: str                  # 确认令牌，人凭它执行，模型不得自行生成
    tool_name: str                  # 待执行工具名
    arguments: dict[str, Any]       # 已通过契约校验的参数，确认后原样执行
    arguments_digest: str           # 参数指纹，校验时比对，防止确认的是 A 执行的是 B
    summary: str                    # 给售后顾问看的一句话说明
    session_id: str                 # 沿用原会话，保证幂等键与首次尝试一致
    step_no: int                    # 产生该确认项的步序号，用于审计回溯
    created_at: float               # 创建时间（秒），用于过期判定
    status: str = STATUS_PENDING    # 状态：pending / confirmed / rejected / expired
    result: dict[str, Any] = field(default_factory=dict)  # 执行结果，成功后写入


def digest_arguments(arguments: dict[str, Any]) -> str:
    """计算参数指纹。

    :param arguments: 已校验的业务参数
    :return: 稳定排序后的 sha1 前 16 位；参数顺序不同不影响指纹
    """
    payload = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


class ApprovalStore:
    """待确认项的文件存储。一个确认项一个文件，确认动作本身也写回同一文件。"""

    def __init__(self, root: Path, ttl_s: int):
        """建立存储。

        :param root: 存储目录，不存在会自动创建
        :param ttl_s: 确认项有效期（秒），超时的确认项不可执行，必须重新发起
        """
        self._root = root
        self._ttl_s = ttl_s
        self._root.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        summary: str,
        session_id: str,
        step_no: int,
    ) -> PendingAction:
        """登记一条待确认操作。

        :param tool_name: 工具名
        :param arguments: 已校验参数
        :param summary: 给人看的一句话说明
        :param session_id: 原会话 id，确认后沿用
        :param step_no: 产生该确认项的步序号
        :return: 新建的确认项，含确认令牌
        """
        # 1. 生成确认项：令牌用短 uuid，人只需要复制粘贴
        action = PendingAction(
            action_id=uuid.uuid4().hex[:12],
            tool_name=tool_name,
            arguments=arguments,
            arguments_digest=digest_arguments(arguments),
            summary=summary,
            session_id=session_id,
            step_no=step_no,
            created_at=time.time(),
        )

        # 2. 落盘：确认项必须先于执行存在，崩溃后也能凭文件恢复
        self._write(action)
        return action

    def load(self, action_id: str) -> PendingAction | None:
        """读取确认项。

        :param action_id: 确认令牌
        :return: 确认项；不存在或文件损坏返回 None
        """
        path = self._path(action_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return PendingAction(**data)

    def list_pending(self) -> list[PendingAction]:
        """列出仍可确认的操作，按创建时间升序。

        :return: 状态为 pending 且未过期的确认项
        """
        actions: list[PendingAction] = []

        # 1. 逐文件读取，坏文件跳过
        for path in sorted(self._root.glob("*.json")):
            action = self.load(path.stem)
            if action is None or action.status != STATUS_PENDING:
                continue

            # 2. 过期的不返回，也不在这里改状态，避免只读操作产生写副作用
            if self.is_expired(action):
                continue
            actions.append(action)

        # 3. 按创建时间升序，先来的先处理
        actions.sort(key=lambda item: item.created_at)
        return actions

    def verify(
        self, action_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> tuple[PendingAction | None, str]:
        """校验一个确认令牌是否可用于本次执行。

        :param action_id: 确认令牌
        :param tool_name: 本次要执行的工具名
        :param arguments: 本次要执行的参数
        :return: （确认项, 失败原因）；确认项非空表示通过，失败原因为空串
        """
        # 1. 存在性
        action = self.load(action_id)
        if action is None:
            return None, "确认令牌不存在：%s" % action_id

        # 2. 状态：只有 pending 可执行，已确认过的不能重放
        if action.status != STATUS_PENDING:
            return None, "确认项状态为 %s，不可执行" % action.status

        # 3. 有效期
        if self.is_expired(action):
            return None, "确认项已过期（有效期 %d 秒），请重新发起" % self._ttl_s

        # 4. 工具与参数必须与确认时完全一致，防止"确认 A 执行 B"
        if action.tool_name != tool_name:
            return None, "确认项对应工具 %s，与本次 %s 不符" % (action.tool_name, tool_name)
        if action.arguments_digest != digest_arguments(arguments):
            return None, "参数与确认时不一致，已拒绝执行"

        return action, ""

    def close(
        self, action: PendingAction, status: str, result: dict[str, Any] | None = None
    ) -> None:
        """关闭确认项：执行完成写 confirmed，人工拒绝写 rejected。

        :param action: 确认项
        :param status: 目标状态
        :param result: 执行结果，仅 confirmed 时有意义
        """
        updated = PendingAction(
            action_id=action.action_id,
            tool_name=action.tool_name,
            arguments=action.arguments,
            arguments_digest=action.arguments_digest,
            summary=action.summary,
            session_id=action.session_id,
            step_no=action.step_no,
            created_at=action.created_at,
            status=status,
            result=result or {},
        )
        self._write(updated)

    def is_expired(self, action: PendingAction) -> bool:
        """判断确认项是否已过期。

        :param action: 确认项
        :return: 超过有效期返回 True
        """
        return time.time() - action.created_at > self._ttl_s

    def _write(self, action: PendingAction) -> None:
        """把确认项写盘。

        :param action: 确认项
        """
        path = self._path(action.action_id)
        path.write_text(
            json.dumps(asdict(action), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _path(self, action_id: str) -> Path:
        """把确认令牌映射到文件路径。

        :param action_id: 确认令牌
        :return: 文件路径
        """
        safe = "".join(ch for ch in action_id if ch.isalnum() or ch in "-_")
        return self._root / ("%s.json" % safe)
