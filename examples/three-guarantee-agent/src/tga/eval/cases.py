"""评测用例：加载与校验。

为什么用例放 JSON 而不是写测试类：判定项固定、可读，改一条期望结论不用动代码，
售后专家也能直接维护；用例写错时在加载期就报错，不会静默跑成"零用例全绿"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_type_hints

from ..loop import TermReason
from ..settings import ContextSettings

# 判定条件里必须显式给出的字段；其余按默认值（不限制）处理
_REQUIRED_EXPECT_FIELDS = ("reason", "max_steps")


@dataclass(frozen=True)
class CaseExpect:
    """一条用例的判定条件。全部命中才算通过，任一不命中都要能说清差在哪。"""

    reason: str                                # 期望的终止原因，取值必须是五类之一
    max_steps: int                             # 允许的最大步数，超出算失控
    tools_used: tuple[str, ...] = ()           # 期望被调用过的工具，按"包含"判定
    answer_contains: tuple[str, ...] = ()      # 答复里必须出现的片段
    answer_not_contains: tuple[str, ...] = ()  # 答复里不允许出现的片段（如编造的条款号）
    detail_contains: tuple[str, ...] = ()      # 终止说明里必须出现的片段，用于非 completed 分支
    tool_error_codes: tuple[str, ...] = ()     # 期望出现的工具错误码，按集合相等判定


@dataclass(frozen=True)
class Case:
    """一条评测用例：输入 + 判定条件 + 说明。"""

    case_id: str            # 用例标识，文件名通常与之一致
    input_text: str         # 喂给助手的用户输入原文
    expect: CaseExpect      # 判定条件
    cancel_before_run: bool  # 是否在开跑前置中断标志，用于覆盖 cancelled 分支
    note: str               # 这条用例覆盖什么，失败时直接进报告
    path: Path              # 用例文件路径，便于定位
    # 用例级配置覆盖：只用于构造极端场景（如把窗口压到很小来验证降级），不改变默认配置
    context_override: dict[str, float] | None = None
    requires_recording: bool = True   # 是否需要 replay 录制；纯终止分支用例可以不录
    confirm_pending: bool = False     # 跑完后是否模拟人工确认，验证高危操作的开闸通道
    # 快照期望：跑完后核对落盘快照的 status / resumable / min_step，验证中断恢复的落盘事实
    snapshot_expect: dict[str, Any] | None = None
    # 恢复探针：不调模型，直接构造坏快照验证 run_resume 的拒绝语义，
    # 取值 missing / corrupt / future_version / completed，None 表示不是探针用例
    resume_probe: str | None = None


def load_cases(suite_dir: Path, only: str | None = None) -> list[Case]:
    """加载并校验一个用例集。

    :param suite_dir: 用例目录，一个 JSON 文件一条用例
    :param only: 只加载指定 id 的用例；None 表示全部
    :return: 用例列表，按文件名排序，保证多次运行输出顺序一致
    :raises ValueError: 目录不存在、目录为空、字段缺失、id 重复或 reason 不合法
    """
    # 1. 目录校验：空目录跑出"零失败"是最危险的假绿
    if not suite_dir.is_dir():
        raise ValueError("用例目录不存在：%s" % suite_dir)
    files = sorted(suite_dir.glob("*.json"))
    if not files:
        raise ValueError("用例目录为空：%s" % suite_dir)

    # 2. 逐文件解析并校验 id 唯一
    cases: list[Case] = []
    seen: set[str] = set()
    for path in files:
        case = _parse_case(path)
        if only is not None and case.case_id != only:
            continue
        if case.case_id in seen:
            raise ValueError("用例 id 重复：%s" % case.case_id)
        seen.add(case.case_id)
        cases.append(case)

    # 3. 过滤后为空说明 id 写错，不能静默返回空集当作通过
    if not cases:
        raise ValueError("没有匹配的用例：%s" % (only or suite_dir))
    return cases


def _parse_case(path: Path) -> Case:
    """解析单个用例文件，字段缺失或取值非法立即抛错。

    :param path: 用例文件路径
    :return: 用例对象
    :raises ValueError: 字段缺失或取值非法，消息指明是哪个用例哪个字段
    :raises json.JSONDecodeError: 文件不是合法 JSON
    """
    # 1. 读原始 JSON，坏文件直接报错
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("用例必须是 JSON 对象：%s" % path.name)

    # 2. 基础字段
    case_id = str(data.get("id") or "").strip()
    if not case_id:
        raise ValueError("用例缺少 id：%s" % path.name)
    input_text = str(data.get("input") or "").strip()
    if not input_text:
        raise ValueError("用例 %s 缺少 input" % case_id)

    # 3. 判定条件：reason 必须落在五类终止里，避免写出永远不可能通过的用例
    raw = data.get("expect")
    if not isinstance(raw, dict):
        raise ValueError("用例 %s 缺少 expect 段" % case_id)
    for field in _REQUIRED_EXPECT_FIELDS:
        if field not in raw:
            raise ValueError("用例 %s 的 expect 缺少 %s" % (case_id, field))

    reason = str(raw["reason"])
    if reason not in {item.value for item in TermReason}:
        raise ValueError(
            "用例 %s 的 reason %r 不在五类终止内：%s"
            % (case_id, reason, " / ".join(item.value for item in TermReason))
        )

    # 4. 组装：列表型字段统一转元组，避免调用方误改
    expect = CaseExpect(
        reason=reason,
        max_steps=int(raw["max_steps"]),
        tools_used=tuple(str(x) for x in (raw.get("tools_used") or ())),
        answer_contains=tuple(str(x) for x in (raw.get("answer_contains") or ())),
        answer_not_contains=tuple(str(x) for x in (raw.get("answer_not_contains") or ())),
        detail_contains=tuple(str(x) for x in (raw.get("detail_contains") or ())),
        tool_error_codes=tuple(str(x) for x in (raw.get("tool_error_codes") or ())),
    )

    # 5. 配置覆盖与两个开关：配置覆盖的键名、取值都在这里校验
    return Case(
        case_id=case_id,
        input_text=input_text,
        expect=expect,
        cancel_before_run=bool(data.get("cancel_before_run", False)),
        note=str(data.get("note") or ""),
        path=path,
        context_override=_parse_context_override(case_id, data.get("context_override")),
        requires_recording=bool(data.get("requires_recording", True)),
        confirm_pending=bool(data.get("confirm_pending", False)),
        snapshot_expect=_parse_snapshot_expect(case_id, data.get("snapshot_expect")),
        resume_probe=_parse_resume_probe(case_id, data.get("resume_probe")),
    )


def _parse_snapshot_expect(case_id: str, raw: Any) -> dict[str, Any] | None:
    """解析快照期望段，键名在加载期就校验。

    WHY 加载期校验：键名写错若静默忽略，用例会跑在"没有期望"的状态下恒绿，
    等于给自己发一张假的通过证明。

    :param case_id: 用例 id，用于报错定位
    :param raw: JSON 里的 snapshot_expect 段，可为 None
    :return: 期望字典；缺省返回 None
    :raises ValueError: 不是对象或含未知键时抛出
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("用例 %s 的 snapshot_expect 必须是对象" % case_id)

    allowed = {"status", "resumable", "min_step"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(
            "用例 %s 的 snapshot_expect 含未知字段 %s（可用：%s）"
            % (case_id, "、".join(unknown), " / ".join(sorted(allowed)))
        )
    if "status" not in raw:
        raise ValueError("用例 %s 的 snapshot_expect 缺少 status" % case_id)
    return dict(raw)


def _parse_resume_probe(case_id: str, raw: Any) -> str | None:
    """解析恢复探针类型，取值必须是已知的几种坏快照构造方式。

    :param case_id: 用例 id，用于报错定位
    :param raw: JSON 里的 resume_probe 字段，可为 None
    :return: 探针类型；缺省返回 None
    :raises ValueError: 取值不在允许集合内
    """
    if raw is None:
        return None
    value = str(raw)
    allowed = {"missing", "corrupt", "future_version", "completed"}
    if value not in allowed:
        raise ValueError(
            "用例 %s 的 resume_probe %r 非法（可用：%s）"
            % (case_id, value, " / ".join(sorted(allowed)))
        )
    return value


def _parse_context_override(case_id: str, raw: Any) -> dict[str, float] | None:
    """解析用例级配置覆盖，键名与取值在加载期就校验。

    WHY: 覆盖只用于构造极端场景；键名写错若静默忽略，用例会跑在默认配置下，
    判定却按极端场景写，最后得到一条永远说不清原因的假失败。

    :param case_id: 用例 id，用于报错定位
    :param raw: JSON 里的 context_override 段，可为 None
    :return: 覆盖字典；缺省或空对象返回 None
    :raises ValueError: 不是对象、键名不在上下文预算表、取值不是数值
    """
    # 1. 缺省：不覆盖，按全局配置跑
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("用例 %s 的 context_override 必须是对象" % case_id)

    # 2. 逐项校验：键必须在上下文预算表里，取值按目标字段的类型转换
    hints = get_type_hints(ContextSettings)
    parsed: dict[str, float] = {}
    for key, value in raw.items():
        name = str(key)
        if name not in hints:
            raise ValueError(
                "用例 %s 的 context_override 含未知字段 %s（可覆盖：%s）"
                % (case_id, name, " / ".join(sorted(hints)))
            )
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(
                "用例 %s 的 context_override.%s 必须是数值" % (case_id, name)
            ) from None
        # 整型字段保持整型：预算表里窗口、配额都是整数，留成浮点会让下游格式化出错
        parsed[name] = int(number) if hints[name] is int else number

    return parsed or None
