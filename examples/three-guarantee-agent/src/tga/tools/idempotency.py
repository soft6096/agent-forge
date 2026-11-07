"""写类工具的幂等存储：抢占执行权、复用首次结果、按窗口清理。

为什么不能用"写之前先查一次"：并发下两个调用都能查到"没有记录"，然后各写一条。
这里用文件系统的独占创建（O_CREAT|O_EXCL）当唯一索引，对应数据库里的唯一约束；
真实实现换成幂等键表 + 唯一索引，语义完全一致。

状态机只有三个结果：命中首次结果（HIT）、抢到执行权（ACQUIRED）、别人正在执行（IN_FLIGHT）。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class ClaimStatus(str, Enum):
    """抢占结果。三态缺一，幂等就会出现"重复写"或"永久锁死"。"""

    HIT = "hit"              # 窗口内已有结果，直接复用，不再执行
    ACQUIRED = "acquired"    # 抢到执行权，可以真正执行
    IN_FLIGHT = "in_flight"  # 别人正在执行，调用方应退避重试


@dataclass(frozen=True)
class Claim:
    """一次抢占的结果。token 用于后续提交或释放，防止迟到者覆盖别人的结论。"""

    status: ClaimStatus
    token: str = ""                          # 仅 ACQUIRED 时有值
    result: dict[str, Any] | None = None     # 仅 HIT 时有值，是首次执行的业务结果


class FileIdempotencyStore:
    """文件系统幂等存储：一个键一个文件，占位与结果共用一个文件的不同状态。"""

    # 占位超过该秒数视为上次执行已崩（进程被杀、线程僵死），允许后来者接管
    STALE_SECONDS = 60
    # 清理扫描的最小间隔，避免每次调用都全目录扫描
    PURGE_INTERVAL_S = 60

    def __init__(self, root: Path, window_s: int):
        """建立存储。

        :param root: 存储目录，不存在会自动创建
        :param window_s: 去重窗口（秒），超过窗口的旧记录允许被重新执行
        """
        self._root = root
        self._window_s = window_s
        self._last_purge = 0
        self._root.mkdir(parents=True, exist_ok=True)

    def claim(self, key: str) -> Claim:
        """抢占某个幂等键的执行权。

        :param key: 幂等键，由注册表按会话 + 工具 + 业务键生成
        :return: 三态之一；HIT 时附带首次结果，ACQUIRED 时附带 token
        """
        # 1. 机会式清理：窗口外的旧记录先删掉，避免目录无限增长
        self._purge_if_due()

        path = self._path(key)
        now = int(time.time())
        record = self._read(path)

        # 2. 已有记录：按状态与时间判定命中、冲突还是接管
        if record is not None:
            if record.get("state") == "done":
                if now - int(record.get("created_at") or 0) <= self._window_s:
                    return Claim(status=ClaimStatus.HIT, result=record.get("result") or {})
                # 窗口外：允许重放，删掉旧结果后重新抢占
                path.unlink(missing_ok=True)
            else:
                if now - int(record.get("started_at") or 0) <= self.STALE_SECONDS:
                    return Claim(status=ClaimStatus.IN_FLIGHT)
                # 占位超时：上次执行已僵死，接管它，让重试还能继续
                path.unlink(missing_ok=True)

        # 3. 原子占位：O_CREAT|O_EXCL 是文件系统层面的唯一索引，并发只有一个能成功
        token = uuid.uuid4().hex[:12]
        payload = {"key": key, "state": "in_flight", "started_at": now, "owner": token}
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # 竞争失败：说明另一个调用刚刚抢到，本次按"正在执行"处理
            return Claim(status=ClaimStatus.IN_FLIGHT)

        # 4. 写入占位内容，标明这是哪一次执行占的位
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        return Claim(status=ClaimStatus.ACQUIRED, token=token)

    def commit(self, key: str, token: str, result: dict[str, Any]) -> None:
        """写入首次执行结果，供窗口内重复调用复用。

        :param key: 幂等键
        :param token: 抢占时拿到的令牌，令牌不符说明占位已被接管
        :param result: 业务结果，重复调用原样返回
        """
        path = self._path(key)

        # 1. 校验归属：占位被接管后自己是迟到者，结果必须丢弃，不能覆盖别人的
        record = self._read(path)
        if record is None or record.get("owner") != token:
            return

        # 2. 落结果：state 从 in_flight 转为 done，此后同键调用直接命中
        payload = {
            "key": key,
            "state": "done",
            "created_at": int(time.time()),
            "result": result,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def release(self, key: str, token: str) -> None:
        """释放占位，让失败后的重试能够再次抢占。

        :param key: 幂等键
        :param token: 抢占时拿到的令牌
        """
        path = self._path(key)

        # 1. 只有自己占的位才能释放，避免误删别人正在执行的占位
        record = self._read(path)
        if record is not None and record.get("owner") == token:
            path.unlink(missing_ok=True)

    def purge(self) -> int:
        """删除窗口外的已完成记录与僵死占位。

        :return: 被删除的文件数
        """
        removed = 0
        now = int(time.time())

        # 1. 逐文件判定：done 超窗口、in_flight 超僵死时间都可删
        for path in self._root.glob("*.json"):
            record = self._read(path)
            if record is None:
                # 坏文件（写到一半被中断）也一并清掉，否则永远占着这个键
                path.unlink(missing_ok=True)
                removed += 1
                continue
            if record.get("state") == "done":
                expired = now - int(record.get("created_at") or 0) > self._window_s
            else:
                expired = now - int(record.get("started_at") or 0) > self.STALE_SECONDS
            if expired:
                path.unlink(missing_ok=True)
                removed += 1

        # 2. 记录本次清理时间，供下一次判定
        self._last_purge = now
        return removed

    def _purge_if_due(self) -> None:
        """按最小间隔触发清理，避免每次调用都扫目录。"""
        if int(time.time()) - self._last_purge < self.PURGE_INTERVAL_S:
            return
        self.purge()

    def _path(self, key: str) -> Path:
        """把幂等键映射到文件路径。

        WHY: 键里有冒号、竖线与中文，直接当文件名在部分系统上非法；
        用哈希做文件名、键原文写进文件内容，既能落盘也不影响排查。

        :param key: 幂等键
        :return: 该键对应的文件路径
        """
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]
        return self._root / ("%s.json" % digest)

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        """读取记录，文件不存在或内容损坏时返回 None。

        :param path: 记录文件路径
        :return: 解析后的记录，坏文件返回 None 由调用方按"无记录"处理
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None
