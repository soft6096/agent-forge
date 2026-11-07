"""工具契约层：九字段契约卡、副作用分级、参数校验、三段式错误语义。

为什么单独一层：工具是模型唯一的动作通道。契约写错时，模型会选错工具、传错参数、
重复写数据，或者把不可逆操作当查询执行——这些都不是"模型不聪明"，而是契约没说清。
本模块只放契约与校验；登记与执行在 registry，具体业务在 builtin。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from ..settings import RetrievalSettings
from ..state import SessionState

# schema 里允许出现的 JSON 类型；出现其他值说明契约本身写错了
_ALLOWED_TYPES = ("string", "integer", "number", "boolean", "array", "object")


class SideEffect(str, Enum):
    """副作用分级。判据：能否在 10 分钟内靠反向操作完全恢复，不能恢复的一律按高危算。"""

    READ = "read"            # 只读：不改变任何外部状态，可自由重试
    WRITE = "write"          # 写：改变状态但可补偿，必须有幂等键
    DANGEROUS = "dangerous"  # 高危：不可逆或对外可见，幂等键 + 人工确认开关


class ToolErrorCode(str, Enum):
    """工具错误码，有限枚举。骨架只认这一组值来判定是否重试，不接受自由文本错误。"""

    INVALID_ARGUMENT = "invalid_argument"    # 参数问题，模型可自我修正
    PERMISSION_DENIED = "permission_denied"  # 越权，模型不可重试，应转人工
    APPROVAL_REQUIRED = "approval_required"  # 高危工具待人工确认，不可重试
    NOT_FOUND = "not_found"                  # 目标不存在，换参数重查有意义
    CONFLICT = "conflict"                    # 业务规则冲突，重试同样结果
    RATE_LIMITED = "rate_limited"            # 限流，退避可重试
    TIMEOUT = "timeout"                      # 单次调用超时，可重试
    UPSTREAM_ERROR = "upstream_error"        # 依赖服务 5xx，可重试
    INTERNAL_ERROR = "internal_error"        # 实现缺陷，不可重试，需人查


# 可重试错误码：只给暂时性故障留口子。参数与权限类错误重试多少次都不会变对
_RETRYABLE_CODES = frozenset(
    {ToolErrorCode.RATE_LIMITED, ToolErrorCode.TIMEOUT, ToolErrorCode.UPSTREAM_ERROR}
)


@dataclass(frozen=True)
class ToolError:
    """工具失败的三段式返回：错误码 + 可读消息 + 下一步建议，缺一模型就无法自我修正。"""

    code: ToolErrorCode
    message: str  # 一句话说清哪个值不对，必须带具体值
    hint: str = ""   # 下一步建议，空表示没有
    field: str = ""  # 出问题的参数名，非参数类错误留空

    @property
    def retryable(self) -> bool:
        """是否属于暂时性故障。骨架据此决定是否重试，工具自身绝不重试。"""
        return self.code in _RETRYABLE_CODES

    def to_dict(self) -> dict[str, Any]:
        """转成给模型看的字典。不含堆栈，避免泄露实现细节且对模型无用。"""
        data: dict[str, Any] = {"error": self.code.value, "message": self.message}
        if self.field:
            data["field"] = self.field
        if self.hint:
            data["hint"] = self.hint
        return data


class ToolFailure(Exception):
    """业务工具主动抛出的已分类失败。抛出后由注册表转成结构化结果，不冒泡到主循环。"""

    def __init__(self, error: ToolError):
        """用带错误码的 ToolError 包装，保证失败一定可归因。"""
        super().__init__(error.message)
        self.error = error


@dataclass(frozen=True)
class ToolOutcome:
    """一次工具调用的结果。成功与失败共用同一结构，骨架只判 ok，不猜业务字段。"""

    ok: bool
    data: dict[str, Any] | None = None      # 成功时的业务数据，失败为 None
    error: ToolError | None = None          # 失败时的三段式错误，成功为 None
    elapsed_ms: int = 0                     # 本次调用实际耗时，含校验与执行
    duplicated: bool = False                # 幂等命中：返回的是首次结果，不是新写入

    @property
    def retryable(self) -> bool:
        """失败结果是否可重试。成功恒为 False，避免骨架对成功结果重试。"""
        return bool(self.error and self.error.retryable)

    def to_message_content(self) -> str:
        """序列化成 tool 消息正文。失败也返回结构化内容，让模型有机会换参数重来。"""
        if self.ok:
            payload: dict[str, Any] = {"ok": True, "data": self.data or {}}
            if self.duplicated:
                # 幂等命中要显式告知，否则模型会把"没重复写"当成"没写成功"
                payload["duplicated"] = True
        else:
            payload = {"ok": False, "error": self.error.to_dict() if self.error else {}}
        return json.dumps(payload, ensure_ascii=False)


@dataclass(frozen=True)
class ParamSpec:
    """一个工具参数的定义：类型、必填性、约束与示例。少任一项模型就可能传错。"""

    name: str            # 参数名，蛇形，与 schema properties 的键一致
    type: str            # JSON 类型，取值见 _ALLOWED_TYPES
    description: str     # 业务含义 + 约束（如"VIN 17 位，不含 I/O/Q"）
    required: bool = True          # 是否必填，缺必填直接拒绝，不猜默认值
    enum: tuple[str, ...] = ()     # 枚举型参数的完整取值，禁止让模型自定格式
    example: Any = None            # 示例值，写进 schema 的 examples 供模型参考
    pattern: str = ""              # 字符串正则校验，空表示不限格式


@dataclass(frozen=True)
class ToolContext:
    """一次工具调用的上下文。会话与租户信息从这里取，绝不接受模型参数传入的 id。"""

    state: SessionState      # 当前会话状态，用于取会话 id 与步序号
    tenant_id: str           # 租户标识，来自会话鉴权结果，不来自模型参数
    project_root: Path       # 项目根，示例工具用它定位本地 fixture 与运行目录
    require_approval: bool   # 高危工具人工确认总开关，来自配置，默认开
    retrieval: RetrievalSettings  # 检索上限与阈值，工具不得自己写死条数
    # 注册表为写类工具生成的幂等键，只读工具为空；工具不得自行生成，否则每次都是新键
    idempotency_key: str = ""
    # 人工确认令牌：只由确认入口在人工核对后传入；模型文本里出现的"已确认"一律不算数
    approval_id: str = ""

    def make_idempotency_key(self, tool_name: str, biz_key: str) -> str:
        """生成写类工具的幂等键。键里必须含会话与业务键，随机 UUID 等于没有幂等。

        :param tool_name: 工具名，区分不同写操作
        :param biz_key: 业务键，同一业务必须稳定（如 vin|零件|单据类型）
        :return: 幂等键字符串
        """
        return self.state.idempotency_key(tool_name, biz_key)


# 工具执行体：入参是已校验的业务参数与调用上下文，返回可直接序列化的业务数据。
# 保留键 _duplicated=True 表示幂等命中，注册表会把它提到 ToolOutcome.duplicated 并从数据中移除
Handler = Callable[[dict[str, Any], ToolContext], dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    """一张工具契约卡。九字段齐全才算定义完成，缺一项即视为契约未完成。

    九字段：name / description / params(parameters) / side_effect / idempotency /
    permission / errors / timeout_ms / source。
    """

    name: str                     # 动词+名词，全局唯一，小写蛇形
    description: str              # 何时用 / 何时不用 / 返回什么，模型选工具的唯一依据
    params: tuple[ParamSpec, ...]  # 参数定义，按位置对应 schema properties
    side_effect: SideEffect       # 副作用等级，写与高危必须显式标注
    permission: str               # 所需角色 + 租户隔离方式，不写"已鉴权"这类空话
    errors: tuple[ToolErrorCode, ...]  # 可能返回的错误码，必须是有限枚举子集
    timeout_ms: int               # 单次调用超时（毫秒），重试归骨架不归工具
    source: str                   # 实现来源：本地实现 / MCP / 内部服务
    handler: Handler              # 执行体，只在注册表里被调用
    idempotency: str = ""         # 写类必填：键名 + 去重窗口，只读留空
    # 写类必填：参与业务键的参数名，注册表按顺序拼成业务键，工具不得自行生成幂等键
    idempotency_key_fields: tuple[str, ...] = ()
    group: str = "default"        # 业务域分组，工具数超软上限时按组注入
    requires_approval: bool = False  # 高危标记，人工确认由骨架读开关执行

    @property
    def is_write(self) -> bool:
        """是否改变外部状态。只读工具可以自由重试，写类必须先过幂等。"""
        return self.side_effect is not SideEffect.READ


def _type_matches(value: Any, expected: str) -> bool:
    """判断值是否符合 JSON 类型。bool 必须单独判，因为 Python 里 bool 是 int 的子类。"""
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def validate_arguments(
    spec: ToolSpec, arguments: dict[str, Any]
) -> tuple[dict[str, Any], ToolError | None]:
    """按契约校验参数，返回（清洗后的参数, 错误）。错误为 None 表示可以执行。

    :param spec: 目标工具的契约卡，提供参数定义与取值范围
    :param arguments: 模型给出的原始参数，可能是任意脏数据
    :return: 二元组；第二个元素非空时必须中止执行，且错误信息要能让模型自己改对
    """
    # 1. 结构校验：参数必须是对象，模型偶尔会传字符串或数组
    if not isinstance(arguments, dict):
        return {}, ToolError(
            code=ToolErrorCode.INVALID_ARGUMENT,
            message="arguments 必须是 JSON 对象，收到 %s" % type(arguments).__name__,
            hint='按 schema 传键值对，例如 {"vin": "LSVAA1234R1234567"}',
        )

    known = {p.name for p in spec.params}

    # 2. 未知参数直接拒绝：静默丢弃会让模型以为该参数生效了
    unknown = sorted(set(arguments) - known)
    if unknown:
        return {}, ToolError(
            code=ToolErrorCode.INVALID_ARGUMENT,
            message="不认识参数 %s" % "、".join(unknown),
            field=unknown[0],
            hint="可用参数：%s" % ("、".join(sorted(known)) or "无"),
        )

    cleaned: dict[str, Any] = {}

    # 3. 逐参数校验必填、类型、枚举、正则，任一不过立即返回可修正的错误
    for param in spec.params:
        value = arguments.get(param.name)
        if value is None:
            if param.required:
                return {}, ToolError(
                    code=ToolErrorCode.INVALID_ARGUMENT,
                    message="缺少必填参数 %s" % param.name,
                    field=param.name,
                    hint=param.description,
                )
            continue
        if not _type_matches(value, param.type):
            return {}, ToolError(
                code=ToolErrorCode.INVALID_ARGUMENT,
                message="参数 %s 期望 %s，收到 %s（%r）"
                % (param.name, param.type, type(value).__name__, value),
                field=param.name,
                hint=param.description,
            )
        if param.enum and value not in param.enum:
            return {}, ToolError(
                code=ToolErrorCode.INVALID_ARGUMENT,
                message="参数 %s 只接受 %s，收到 %r" % (param.name, " / ".join(param.enum), value),
                field=param.name,
                hint="取值大小写敏感，请从上面列出的值中选一个",
            )
        if param.pattern and isinstance(value, str) and not re.fullmatch(param.pattern, value):
            return {}, ToolError(
                code=ToolErrorCode.INVALID_ARGUMENT,
                message="参数 %s 格式不符合要求（%r）" % (param.name, value),
                field=param.name,
                hint=param.description,
            )
        cleaned[param.name] = value

    # 4. 可选参数不在这里补默认值：默认值属于业务语义，由工具自己决定
    return cleaned, None


def build_function_schema(spec: ToolSpec) -> dict[str, Any]:
    """生成 OpenAI function calling 的 tools 条目。枚举带完整取值，模型才不会自创格式。

    :param spec: 工具契约卡
    :return: 可直接放进请求体 tools 数组的字典
    """
    properties: dict[str, Any] = {}
    required: list[str] = []

    # 1. 逐参数展开 schema：描述、枚举、示例、正则全部透传
    for param in spec.params:
        prop: dict[str, Any] = {"type": param.type, "description": param.description}
        if param.enum:
            prop["enum"] = list(param.enum)
        if param.example is not None:
            prop["examples"] = [param.example]
        if param.pattern:
            prop["pattern"] = param.pattern
        properties[param.name] = prop
        if param.required:
            required.append(param.name)

    # 2. 组装函数条目：additionalProperties 必须关，多传字段等于契约失效
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def check_spec(spec: ToolSpec) -> None:
    """登记前核对契约九字段。不合格直接抛 ValueError，不允许带病上线。

    :param spec: 待登记的工具契约卡
    :raises ValueError: 任一必填字段缺失、非法或自相矛盾时抛出，消息说明该改哪个字段
    """
    # 1. 命名校验：动词+名词的小写蛇形，禁止 manage/do/process 这类聚合命名
    if not re.fullmatch(r"[a-z][a-z0-9]*(_[a-z0-9]+)+", spec.name):
        raise ValueError(
            "工具名 %r 不规范：必须是动词+名词的小写蛇形，如 query_warranty_period" % spec.name
        )
    if any(bad in spec.name.split("_") for bad in ("manage", "do", "handle", "process")):
        raise ValueError("工具名 %r 用了聚合动词，应拆成具体动作" % spec.name)

    # 2. 描述校验：必须写清何时用/何时不用/返回什么，只写功能名等于把选择权交给运气
    if len(spec.description.strip()) < 20:
        raise ValueError(
            "工具 %s 的描述过短：至少写清何时用、何时不用、返回什么" % spec.name
        )
    if "何时用" not in spec.description and "何时不用" not in spec.description:
        raise ValueError("工具 %s 的描述缺少「何时用 / 何时不用」说明" % spec.name)

    # 3. 参数校验：至少一个参数，类型合法，名字不重复
    if not spec.params:
        raise ValueError("工具 %s 未定义任何参数；无参工具也要显式声明理由" % spec.name)
    seen: set[str] = set()
    for param in spec.params:
        if param.type not in _ALLOWED_TYPES:
            raise ValueError(
                "工具 %s 参数 %s 类型 %r 非法，只允许 %s"
                % (spec.name, param.name, param.type, " / ".join(_ALLOWED_TYPES))
            )
        if param.name in seen:
            raise ValueError("工具 %s 参数 %s 重复定义" % (spec.name, param.name))
        seen.add(param.name)
        if not param.description.strip():
            raise ValueError("工具 %s 参数 %s 缺少描述" % (spec.name, param.name))

    # 4. 副作用校验：写类与高危必须有幂等键说明与业务键参数，高危必须挂人工确认
    if spec.is_write and not spec.idempotency.strip():
        raise ValueError(
            "工具 %s 是 %s 级，必须写清幂等键与去重窗口" % (spec.name, spec.side_effect.value)
        )
    if spec.is_write:
        if not spec.idempotency_key_fields:
            raise ValueError(
                "工具 %s 是 %s 级，必须声明幂等键字段（参与业务键的参数名）"
                % (spec.name, spec.side_effect.value)
            )
        for field_name in spec.idempotency_key_fields:
            if field_name not in seen:
                raise ValueError(
                    "工具 %s 的幂等键字段 %s 不是已声明的参数" % (spec.name, field_name)
                )
    if spec.side_effect is SideEffect.DANGEROUS and not spec.requires_approval:
        raise ValueError("工具 %s 是高危级，必须挂人工确认（requires_approval=True）" % spec.name)

    # 5. 错误、超时、权限、来源：不许留空或写空话
    if not spec.errors:
        raise ValueError("工具 %s 未声明可能的错误码" % spec.name)
    if spec.timeout_ms <= 0:
        raise ValueError("工具 %s 的超时必须为正数毫秒" % spec.name)
    if not spec.permission.strip():
        raise ValueError("工具 %s 未声明所需角色与租户隔离方式" % spec.name)
    if not spec.source.strip():
        raise ValueError("工具 %s 未声明实现来源" % spec.name)
