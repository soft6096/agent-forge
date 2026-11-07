"""会话状态：可完整序列化，用于中断恢复。

硬规则（agent-core-standards 01 §3 / 03 §1、§3）：
1. 状态是事实（做过什么、花了多少），不是"这一轮给模型看什么"，因此不参与上下文裁剪。
2. 必须能完整落 JSON 且带 schema 版本——字段会演进，读旧快照不允许直接 cls(**data)。
3. 落盘必须原子：写到一半被 kill 留下的半截 JSON，会把"恢复"变成"启动即崩"。
4. 快照要能过期：不留过期时间，runtime 目录只会无限增长且永远分不清哪份还能用。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any

# 快照格式版本：字段增删必须递增，读取时按版本做兼容或明确拒绝
# v2：新增 detection（骨架检测状态），恢复后死循环识别、熔断、无进展判定不能从零开始
SCHEMA_VERSION = 2
# 快照过期时间（秒）：超过即视为不可恢复，只能新开会话
DEFAULT_SNAPSHOT_TTL_S = 24 * 3600
# 归档目录保留时长（秒）：过期快照先归档再清，留出人工捞回的窗口
ARCHIVE_TTL_S = 7 * 24 * 3600


class SessionStatus(str, Enum):
    """会话阶段状态。显式枚举，禁止用布尔组合表达（01 §3）。"""

    RUNNING = "running"                  # 进行中
    COMPLETED = "completed"              # 模型给出最终答案
    CANCELLED = "cancelled"              # 用户中断，可恢复
    STEP_EXHAUSTED = "step_exhausted"    # 步数或任务时长耗尽
    BUDGET_EXCEEDED = "budget_exceeded"  # token / 金额超限
    BLOCKED = "blocked"                  # 安全拦截或不收敛


# 允许续跑的状态：只有"跑到一半被打断"的才该接上。
# WHY 不把 completed / blocked 也算可恢复：续跑它们只会重放已完成的工作，
# 写类工具虽然有幂等保护，但对用户是"又跑了一遍"，语义上就是错的。
RESUMABLE_STATUSES = (SessionStatus.RUNNING.value, SessionStatus.CANCELLED.value)


class SnapshotError(Exception):
    """快照不可用（缺失、损坏、版本更高）。

    恢复入口必须显性报错，禁止静默新建会话——否则用户以为在续跑，实际从头开始，
    还可能把已经做过的写操作再做一遍。
    """


@dataclass
class SessionState:
    """一次任务的全部事实，能落 JSON 也能原样恢复。"""

    session_id: str
    user_input: str
    step_no: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    used_tools: list[str] = field(default_factory=list)
    pending_confirmation: dict[str, Any] | None = None
    status: str = SessionStatus.RUNNING.value
    # 一次任务一个 trace_id：把模型调用与工具调用串成一条链路，缺了只能按时间猜
    trace_id: str = ""
    # 累计用量：恢复后必须接着计费与判预算，否则续跑等于把预算清零重来
    usage: dict[str, Any] = field(default_factory=dict)
    # 已调用工具清单：工具名 + 参数指纹 + 结果摘要（03 §1），既用于恢复对账也用于死循环识别
    tool_records: list[dict[str, Any]] = field(default_factory=list)
    # 骨架检测状态：重复调用签名、工具失败计数、已见工具名/结果摘要、无进展步数。
    # WHY 必须一起落盘：这些不是"这一轮的临时变量"，而是会话事实；恢复后若归零，
    # 模型接着打转也不会被识别，前几步等于白送。
    detection: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def new(cls, user_input: str) -> "SessionState":
        """开新会话。session_id 用于日志关联与幂等键前缀。"""
        return cls(
            session_id=uuid.uuid4().hex[:12],
            user_input=user_input,
            trace_id=uuid.uuid4().hex,
        )

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
        """记录已用工具名。保留这一份扁平清单，便于评测与日志直接读取。"""
        self.used_tools.append(name)

    def record_tool_call(
        self,
        name: str,
        args_fingerprint: str,
        step: int,
        raw_arguments: Any = None,
    ) -> dict[str, Any]:
        """记一条工具调用记录，返回可后续补写结果摘要的字典。

        WHY 先记后补：工具执行中途被 kill 时，"调用过但没有结果"这个事实必须在快照里，
        否则恢复后无法判断上次到底写没写成。

        :param name: 工具名
        :param args_fingerprint: 参数指纹（已归一化），用于死循环识别
        :param step: 本次调用发生在第几步
        :param raw_arguments: 原始参数，恢复对账时用它重算幂等键
        :return: 记录字典本体，调用方执行完补 result_digest
        """
        record: dict[str, Any] = {
            "name": name,
            "args_fingerprint": args_fingerprint,
            "result_digest": "",
            "step": step,
            "arguments": raw_arguments,
        }
        self.tool_records.append(record)
        return record

    def unfinished_tool_calls(self) -> list[dict[str, Any]]:
        """返回"开始执行但没有结果摘要"的记录，恢复对账用。"""
        return [item for item in self.tool_records if not item.get("result_digest")]

    def is_resumable(self) -> bool:
        """是否处于可续跑状态。"""
        return self.status in RESUMABLE_STATUSES

    def to_dict(self) -> dict[str, Any]:
        """转字典。字段集合即快照 schema，新增字段必须同时递增 SCHEMA_VERSION。"""
        return asdict(self)

    def save(self, path: Path) -> None:
        """原子落快照：先写临时文件再 rename，中途被杀也不会留下半截 JSON。

        :param path: 快照文件路径
        """
        # 1. 目录不存在先建：恢复入口可能指向一个刚被清理过的目录
        path.parent.mkdir(parents=True, exist_ok=True)

        # 2. 写临时文件：default=str 兜住 provider 回传的原始对象，
        #    否则一个不可序列化字段就会让整份快照丢失
        tmp = path.with_name(path.name + ".tmp")
        payload = json.dumps(
            self.to_dict(), ensure_ascii=False, indent=2, default=str
        )
        tmp.write_text(payload, encoding="utf-8")

        # 3. rename 是原子操作：要么是旧快照，要么是新快照，不存在写了一半的中间态
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> "SessionState":
        """从快照文件还原会话状态，对未知字段与未知状态做容错。

        :param path: 快照文件路径
        :return: 还原后的会话状态
        :raises SnapshotError: 文件缺失、内容损坏、版本高于当前程序时抛出
        """
        # 1. 读取与解析：坏文件必须显性失败，不能返回一个"空会话"让人接着跑
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SnapshotError("快照读取失败：%s（%s）" % (path, exc)) from None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotError("快照内容损坏：%s（%s）" % (path, exc)) from None
        if not isinstance(raw, dict):
            raise SnapshotError("快照不是 JSON 对象：%s" % path)

        # 2. 版本校验：版本更高说明程序比快照旧，硬读会把新字段当脏数据丢掉
        try:
            version = int(raw.get("schema_version") or 0)
        except (TypeError, ValueError):
            raise SnapshotError("快照 schema_version 非法：%s" % path) from None
        if version > SCHEMA_VERSION:
            raise SnapshotError(
                "快照版本 %d 高于当前支持的 %d，请升级程序后再恢复：%s"
                % (version, SCHEMA_VERSION, path)
            )

        # 3. 字段过滤：只取认识的键，多出来的忽略、缺的走默认值，避免演进一次旧快照全废
        known = {item.name for item in fields(cls)}
        data = {key: value for key, value in raw.items() if key in known}
        try:
            state = cls(**data)
        except TypeError as exc:
            raise SnapshotError("快照字段与当前版本不匹配：%s（%s）" % (path, exc)) from None

        # 4. 归一：版本号对齐到当前，status 认不出就按 running 处理并保留其余事实
        state.schema_version = SCHEMA_VERSION
        if state.status not in {item.value for item in SessionStatus}:
            state.status = SessionStatus.RUNNING.value
        if not state.trace_id:
            # 老快照没有 trace_id：补一个，保证后续日志能串起来，而不是整段为空
            state.trace_id = uuid.uuid4().hex
        return state

    def idempotency_key(self, tool_name: str, biz_key: str) -> str:
        """写类工具的幂等键：绑定会话、工具与业务键。

        WHY: 键里故意不含步序号——同一业务在跨步重试时步序会变，含步序等于每次都是新键，
        幂等直接失效。也不允许随机 UUID，那样每次重试都会产生新键。

        :param tool_name: 工具名，用于区分不同写操作
        :param biz_key: 业务键（如 vin|零件|单据类型），同一业务必须稳定
        :return: 幂等键字符串
        """
        return "%s:%s:%s" % (self.session_id, tool_name, biz_key)


def prune_snapshots(
    directory: Path, ttl_s: int = DEFAULT_SNAPSHOT_TTL_S
) -> list[str]:
    """把过期快照归档，并清理超期归档。

    WHY 归档而不是直接删：快照是排障证据，24 小时刚过就物理删除，出事时无从复盘。

    :param directory: 快照目录（runtime/sessions）
    :param ttl_s: 快照保留时长（秒），超过即移入 archive
    :return: 本次被归档的 session_id 列表
    """
    archived: list[str] = []

    # 1. 目录不存在：没跑过任务，直接返回，不建空目录
    if not directory.is_dir():
        return archived

    deadline = time.time() - ttl_s
    archive_dir = directory / "archive"

    # 2. 逐个判过期：只有 .json 才是快照，tmp 残留由写盘方自己覆盖
    for path in sorted(directory.glob("*.json")):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= deadline:
            continue
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
            os.replace(path, archive_dir / path.name)
        except OSError:
            # 归档失败不影响主流程：快照本身已经过了可恢复期，删不掉只是占点空间
            continue
        archived.append(path.stem)

    # 3. 归档目录再按更长周期清一次，避免它变成第二个无限增长目录
    if archive_dir.is_dir():
        archive_deadline = time.time() - ARCHIVE_TTL_S
        for path in archive_dir.glob("*.json"):
            try:
                if path.stat().st_mtime < archive_deadline:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    return archived
