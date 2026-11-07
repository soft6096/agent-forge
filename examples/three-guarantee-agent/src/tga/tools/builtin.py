"""内置工具集：三包判定场景的 3 个只读工具 + 1 个高危写工具。

为什么只有四个：示例要演示的是契约、副作用分级与幂等边界，不是业务深度。
只读工具直接读本地 fixture；真实接入时换成主数据与知识库服务，契约一字不用改。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..ops.logging import StructuredLogger
from ..settings import Settings
from .contract import (
    ParamSpec,
    SideEffect,
    ToolContext,
    ToolError,
    ToolErrorCode,
    ToolFailure,
    ToolSpec,
)
from .registry import ToolRegistry

# 可判定的零部件枚举。必须给全，模型才不会自创名称（如把变速箱写成变速器）
PART_NAMES = ("发动机", "变速箱", "动力电池", "转向系统", "制动系统", "车身", "空调系统")

# 申诉单类型枚举，值对应售后系统里的单据类别
APPEAL_TYPES = ("三包维修", "更换总成", "退换车")

# VIN 是 17 位，不含容易与数字混淆的 I / O / Q
VIN_PATTERN = r"[A-HJ-NPR-Z0-9]{17}"


def _load_fixture(project_root: Path, name: str) -> Any:
    """读取本地 fixture，代替真实主数据接口。

    :param project_root: 项目根目录
    :param name: fixture 文件名，位于 data/ 目录下
    :return: 解析后的 JSON 内容（对象或列表）
    :raises ToolFailure: 数据文件缺失时抛 internal_error，不伪装成"查无此车"
    """
    # 1. 定位文件
    path = project_root / "data" / name

    # 2. 缺失属部署问题，绝不能当成业务上的"未找到"，否则模型会给出错误结论
    if not path.is_file():
        raise ToolFailure(
            ToolError(
                code=ToolErrorCode.INTERNAL_ERROR,
                message="缺少数据文件 data/%s" % name,
                hint="检查部署是否带上 data 目录，这不是业务查询失败",
            )
        )

    # 3. 解析并返回
    return json.loads(path.read_text(encoding="utf-8"))


def _add_months(value: date, months: int) -> date:
    """在日期上加若干个月。

    :param value: 起始日期
    :param months: 增加的月数，必须为正
    :return: 到期日期；目标月没有对应日时取该月最后一天（如 1 月 31 日 + 1 月 = 2 月 28 日）
    """
    # 1. 先算年月，月份进位到年份
    total = value.month - 1 + months
    year = value.year + total // 12
    month = total % 12 + 1

    # 2. 逐月回退到该月最后一天，避免 31 日落到只有 30 天的月份报错
    day = value.day
    while True:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1


def _get_vehicle_profile(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """按 VIN 取车辆档案摘要。

    :param arguments: 已通过契约校验的参数，含 vin
    :param context: 调用上下文，提供项目根
    :return: 车型、购车日期、里程、使用性质与基准日期
    :raises ToolFailure: 档案中无此 VIN 时抛 not_found
    """
    # 1. 取参数
    vin = arguments["vin"]

    # 2. 查档案
    payload = _load_fixture(context.project_root, "vehicles.json")
    profile = (payload.get("vehicles") or {}).get(vin)

    # 3. 查无此车：给可修正的提示，让模型回去确认 VIN 而不是换结论
    if profile is None:
        raise ToolFailure(
            ToolError(
                code=ToolErrorCode.NOT_FOUND,
                message="档案中无 VIN %s" % vin,
                field="vin",
                hint="确认 VIN 是否录错，或该车未在本系统建档",
            )
        )

    # 4. 只回判定所需字段：车主联系方式等 PII 不进上下文
    return {
        "vin": vin,
        "model": profile["model"],
        "purchase_date": profile["purchase_date"],
        "mileage_km": int(profile["mileage_km"]),
        "usage_type": profile["usage_type"],
        "reference_date": payload["reference_date"],
    }


def _query_warranty_period(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """查某个零部件是否还在三包有效期内。

    :param arguments: 已校验参数，含 vin 与 part_name
    :param context: 调用上下文，提供项目根
    :return: 起止日期、剩余天数、里程上限、在保结论与条款出处
    :raises ToolFailure: 车辆未建档或零部件未配置规则
    """
    # 1. 取参数
    vin = arguments["vin"]
    part = arguments["part_name"]

    # 2. 取车辆档案：没有购车日期就算不出有效期
    vehicles = _load_fixture(context.project_root, "vehicles.json")
    profile = (vehicles.get("vehicles") or {}).get(vin)
    if profile is None:
        raise ToolFailure(
            ToolError(
                code=ToolErrorCode.NOT_FOUND,
                message="档案中无 VIN %s，无法计算保修期" % vin,
                field="vin",
                hint="先确认 VIN，或改由人工核查购车凭证",
            )
        )

    # 3. 取该零部件的三包规则
    rules = _load_fixture(context.project_root, "warranty_rules.json")
    rule = rules.get(part)
    if not isinstance(rule, dict):
        raise ToolFailure(
            ToolError(
                code=ToolErrorCode.NOT_FOUND,
                message="未配置 %s 的三包规则" % part,
                field="part_name",
                hint="可判定的零部件：%s" % "、".join(PART_NAMES),
            )
        )

    # 4. 算到期日与剩余天数
    purchase = date.fromisoformat(profile["purchase_date"])
    expire = _add_months(purchase, int(rule["months"]))
    # WHY: 基准日取 fixture 固定值而不是今天，否则同一输入隔天跑会得出不同结论，评测无法复现
    reference = date.fromisoformat(vehicles["reference_date"])
    days_left = (expire - reference).days

    # 5. 判在保：时间与里程必须同时满足，任一超限都不算在保
    within_time = days_left >= 0
    within_km = int(profile["mileage_km"]) <= int(rule["km"])
    if within_time and within_km:
        reason = "时间与里程均在保修范围内"
    elif not within_time and not within_km:
        reason = "已超保修期 %d 天且超里程 %d 公里" % (
            -days_left,
            int(profile["mileage_km"]) - int(rule["km"]),
        )
    elif not within_time:
        reason = "已超保修期 %d 天" % -days_left
    else:
        reason = "已超保修里程 %d 公里" % (int(profile["mileage_km"]) - int(rule["km"]))

    # 6. 返回结论与依据，出处由模型带进答复
    return {
        "vin": vin,
        "part_name": part,
        "warranty_start": purchase.isoformat(),
        "warranty_end": expire.isoformat(),
        "reference_date": reference.isoformat(),
        "days_left": days_left,
        "limit_km": int(rule["km"]),
        "mileage_km": int(profile["mileage_km"]),
        "in_warranty": bool(within_time and within_km),
        "reason": reason,
        "clause": rule["clause"],
    }


def _search_clause(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """按关键词检索三包条款，返回带出处的片段。

    :param arguments: 已校验参数，含 query
    :param context: 调用上下文，提供项目根
    :return: 命中片段列表与说明；无命中时列表为空，绝不编造条款
    """
    # 1. 取检索词并归一化，去掉两端空白
    query = str(arguments["query"]).strip()

    # 2. 读条款库
    payload = _load_fixture(context.project_root, "clauses.json")
    clauses = payload.get("clauses") or []

    # 3. 打分：标题命中最高，关键词命中次之，正文命中最低
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in clauses:
        score = 0
        if query and query in item["title"]:
            score += 4
        if any(keyword in query for keyword in item["keywords"]):
            score += 3
        if query and query in item["text"]:
            score += 1
        if score > 0:
            scored.append((score, item))

    # 4. 按阈值过滤再取 top_k 条：低于阈值的"勉强相关"不能当依据用
    threshold = context.retrieval.similarity_threshold
    top_k = context.retrieval.top_k
    keeper = [pair for pair in scored if pair[0] >= threshold]
    # 同分保持库内顺序，保证同一输入每次跑出同样结果
    keeper.sort(key=lambda pair: pair[0], reverse=True)
    hits = [
        {
            "chunk_id": item["chunk_id"],
            "source": item["source"],
            "title": item["title"],
            "text": item["text"],
        }
        for _, item in keeper[:top_k]
    ]

    # 5. 无命中要明确告知，让模型说"未查到相关规定"而不是编条款
    return {
        "query": query,
        "hits": hits,
        "note": "命中 %d 条" % len(hits) if hits else "未检索到相关条款，不要引用未出现的条款编号",
    }


def _submit_appeal_ticket(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """提交三包申诉单（高危写）。同一幂等键只落一次，重复调用返回首次结果。

    :param arguments: 已校验参数，含 vin、part_name、appeal_type、description
    :param context: 调用上下文，提供会话、幂等键与项目根
    :return: 工单号与状态；文件已存在时 data 里带 _duplicated 标记
    :raises ToolFailure: 注册表未下发幂等键时抛 internal_error，写类不允许无保护执行
    """
    # 1. 取参数
    vin = arguments["vin"]
    part = arguments["part_name"]
    appeal_type = arguments["appeal_type"]
    description = arguments["description"]

    # 2. 取注册表下发的幂等键：工具自己生成等于每次重试都是新键，幂等直接失效
    key = context.idempotency_key
    if not key:
        raise ToolFailure(
            ToolError(
                code=ToolErrorCode.INTERNAL_ERROR,
                message="写类工具缺少幂等键：注册表未下发",
                hint="检查 idempotency.enabled 是否为 true，不要绕过注册表直接调用",
            )
        )
    # WHY: 幂等键含冒号、竖线与中文，直接当文件名在部分系统上非法，替换后再用
    safe_key = key.replace(":", "-").replace("|", "-")
    target = context.project_root / "runtime" / "tickets" / ("%s.json" % safe_key)

    # 3. 幂等命中：同键已有结果直接返回首次结果，不重复下单
    if target.is_file():
        first = json.loads(target.read_text(encoding="utf-8"))
        return {
            "ticket_id": first["ticket_id"],
            "status": first["status"],
            "_duplicated": True,
        }

    # 4. 落库：示例写本地文件，真实实现调工单系统（唯一索引兜底）
    ticket = {
        "ticket_id": "AP%s" % key.split(":")[0][-8:].upper(),
        "vin": vin,
        "part_name": part,
        "appeal_type": appeal_type,
        "description": description,
        "session_id": context.state.session_id,
        "step_no": context.state.step_no,
        "status": "submitted",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(ticket, ensure_ascii=False, indent=2), encoding="utf-8")

    # 5. 返回工单号，供答复里引用
    return {"ticket_id": ticket["ticket_id"], "status": ticket["status"]}


# ---------- 契约卡 ----------

GET_VEHICLE_PROFILE = ToolSpec(
    name="get_vehicle_profile",
    description=(
        "按 VIN 查车辆档案：车型、购车日期、当前里程、使用性质。"
        "何时用：需要判断保修期、核对车辆基础信息时。"
        "何时不用：用户没给 VIN，或只问条款内容不涉及具体车辆时，不要调用。"
        "返回：车型、购车日期、里程（公里）、使用性质（personal 家用 / operating 营运）与基准日期。"
    ),
    params=(
        ParamSpec(
            name="vin",
            type="string",
            description="车架号，17 位，不含 I / O / Q",
            pattern=VIN_PATTERN,
            example="LSVAA1234R1234567",
        ),
    ),
    side_effect=SideEffect.READ,
    permission="售后顾问角色；租户为单租户示例，真实接入按会话所属经销商做行级隔离",
    errors=(ToolErrorCode.NOT_FOUND, ToolErrorCode.INVALID_ARGUMENT, ToolErrorCode.INTERNAL_ERROR),
    timeout_ms=3000,
    source="本地实现（data/vehicles.json fixture）",
    handler=_get_vehicle_profile,
    group="warranty",
)

QUERY_WARRANTY_PERIOD = ToolSpec(
    name="query_warranty_period",
    description=(
        "查某零部件是否还在三包有效期内，返回起止日期、剩余天数、里程上限与条款出处。"
        "何时用：已拿到 VIN 与零部件名称、需要判断「在不在保」时。"
        "何时不用：还没确认 VIN 或零部件名称时，先追问再调用；只问条款原文时改用 search_clause。"
        "返回：in_warranty 布尔结论、剩余天数（负数表示已超期）、超限原因与条款出处。"
    ),
    params=(
        ParamSpec(
            name="vin",
            type="string",
            description="车架号，17 位，不含 I / O / Q",
            pattern=VIN_PATTERN,
            example="LSVAA1234R1234567",
        ),
        ParamSpec(
            name="part_name",
            type="string",
            description="零部件名称，必须取自固定枚举",
            enum=PART_NAMES,
            example="变速箱",
        ),
    ),
    side_effect=SideEffect.READ,
    permission="售后顾问角色；租户隔离同车辆档案",
    errors=(
        ToolErrorCode.NOT_FOUND,
        ToolErrorCode.INVALID_ARGUMENT,
        ToolErrorCode.TIMEOUT,
        ToolErrorCode.INTERNAL_ERROR,
    ),
    timeout_ms=3000,
    source="本地实现（data/vehicles.json + data/warranty_rules.json）",
    handler=_query_warranty_period,
    group="warranty",
)

SEARCH_CLAUSE = ToolSpec(
    name="search_clause",
    description=(
        "按关键词检索三包条款库，返回带出处的条款片段（最多 3 条）。"
        "何时用：需要在答复里给出条款依据、或确认某类故障是否属于三包范围时。"
        "何时不用：只需要车辆基础信息或保修期结论时不要调用。"
        "返回：hits 列表（含 chunk_id、source、title、text）；hits 为空表示未查到，不得编造条款。"
    ),
    params=(
        ParamSpec(
            name="query",
            type="string",
            description="检索词，用业务名词组合，如「变速箱 累计修理 更换」",
            example="变速箱 累计修理",
        ),
    ),
    side_effect=SideEffect.READ,
    permission="售后顾问角色；条款库为全租户共享只读资源",
    errors=(ToolErrorCode.INVALID_ARGUMENT, ToolErrorCode.INTERNAL_ERROR),
    timeout_ms=3000,
    source="本地实现（data/clauses.json，真实实现走 RAG 检索）",
    handler=_search_clause,
    group="clause",
)

SUBMIT_APPEAL_TICKET = ToolSpec(
    name="submit_appeal_ticket",
    description=(
        "提交三包申诉单，生成工单号。属高危写操作，必须先经人工确认。"
        "何时用：判定结论为在保可赔、且售后顾问明确要求建单时。"
        "何时不用：结论未定、需人工复核、或用户只是咨询时，一律不要调用。"
        "返回：ticket_id 与 status；重复提交同一业务会返回首次工单号，不会新建。"
    ),
    params=(
        ParamSpec(
            name="vin",
            type="string",
            description="车架号，17 位，不含 I / O / Q",
            pattern=VIN_PATTERN,
            example="LSVAA1234R1234567",
        ),
        ParamSpec(
            name="part_name",
            type="string",
            description="零部件名称，必须取自固定枚举",
            enum=PART_NAMES,
            example="变速箱",
        ),
        ParamSpec(
            name="appeal_type",
            type="string",
            description="申诉类型，必须取自固定枚举",
            enum=APPEAL_TYPES,
            example="三包维修",
        ),
        ParamSpec(
            name="description",
            type="string",
            description="故障现象与已做处理的一句话说明，不超过 200 字",
            example="低速换挡异响，已进店检查未拆解",
        ),
    ),
    side_effect=SideEffect.DANGEROUS,
    permission="售后顾问角色；租户取自会话，按经销商隔离",
    errors=(
        ToolErrorCode.APPROVAL_REQUIRED,
        ToolErrorCode.PERMISSION_DENIED,
        ToolErrorCode.INVALID_ARGUMENT,
        ToolErrorCode.INTERNAL_ERROR,
    ),
    timeout_ms=5000,
    source="本地实现（写 runtime/tickets/，真实实现调工单系统）",
    handler=_submit_appeal_ticket,
    idempotency="session_id:tool:biz_key（biz_key = vin|part|appeal_type，不含步序），去重窗口见 idempotency.window_s",
    # 业务键字段由契约声明，注册表按顺序拼接后交给幂等存储；工具不参与生成
    idempotency_key_fields=("vin", "part_name", "appeal_type"),
    requires_approval=True,
    group="appeal",
)


def build_registry(settings: Settings, logger: StructuredLogger | None = None) -> ToolRegistry:
    """装配示例工具集并返回注册表。

    :param settings: 运行配置，工具超时的全局天花板取自其中
    :param logger: 结构化日志器，用于记录工具内部的未分类异常
    :return: 已登记 3 个只读工具与 1 个高危写工具的注册表
    """
    # 1. 建注册表
    registry = ToolRegistry(settings, logger=logger)

    # 2. 逐个登记：契约不合格会在这里直接抛错，暴露在启动期而不是运行期
    for spec in (GET_VEHICLE_PROFILE, QUERY_WARRANTY_PERIOD, SEARCH_CLAUSE, SUBMIT_APPEAL_TICKET):
        registry.register(spec)

    # 3. 返回可用注册表
    return registry
