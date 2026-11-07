"""配置加载：TOML 配置 + 环境变量覆盖。

为什么单独一层：模型名、预算、超时这些值散落在代码里，改一次要全局搜索，
也无法做"同一份代码跑不同环境"。配置只在这里解析一次，其余模块只读不可变对象。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

# src/tga/settings.py -> parents[2] 就是项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ModelSettings:
    """模型层配置。provider=auto 时按凭据是否存在决定真实调用还是回放。"""

    provider: str
    name: str
    temperature: float
    max_output_tokens: int
    api_base: str
    api_key: str

    @property
    def effective_provider(self) -> str:
        """auto 模式下的实际 provider：有 key 走 openai，否则降级 replay。

        这就是 0.0 第 9 项「降级路径」的落点之一：模型不可用时不是报错，
        而是切到离线回放，保证评测入口在任何环境都能跑。
        """
        if self.provider != "auto":
            return self.provider
        return "openai" if self.api_key else "replay"


@dataclass(frozen=True)
class BudgetSettings:
    """预算：步数、token、金额三层上限。"""

    max_steps: int                  # 单任务最大步数
    max_task_tokens: int            # 单任务 token 上限
    max_cost_cny: float             # 单任务金额上限（元），与 token 上限同时生效
    price_in_cny_per_1k: float      # 输入单价（元 / 千 token）
    price_out_cny_per_1k: float     # 输出单价（元 / 千 token）


@dataclass(frozen=True)
class TimeoutSettings:
    """三处超时：模型调用、工具调用、任务总时长。缺一处即视为未配置。"""

    llm_ms: int
    tool_ms: int
    task_ms: int
    poll_interval_ms: int


@dataclass(frozen=True)
class SafetySettings:
    """安全开关。require_approval 默认开，是 0.0 第 10 项的判据。"""

    require_approval: bool         # 高危工具人工确认总开关，默认开
    input_filter: bool             # 入向过滤：敏感信息识别 + 注入特征标记
    output_filter: bool            # 出向过滤：答复脱敏
    external_content_isolation: bool  # 外部内容与指令分离
    approval_ttl_s: int            # 待确认项有效期（秒），超时必须重新发起


@dataclass(frozen=True)
class ContextSettings:
    """上下文预算表：五段配额 + 压缩阈值。全部来自配置，代码里不出现窗口魔数。"""

    window_tokens: int          # 模型窗口上限
    system: int                 # 系统提示词（不可变段）配额
    tools: int                  # 工具描述配额
    history: int                # 历史（含摘要）配额
    retrieval: int              # 检索与外部内容配额
    output_reserve: int         # 输出预留，不得少于 1.5 倍最大输出
    compress_at: float          # 达到该占比触发裁剪
    keep_recent_messages: int   # 至少保留的近期原文条数

    @property
    def usable_tokens(self) -> int:
        """可用总预算：窗口的 95%，留 5% 给估算误差与 tokenizer 开销。"""
        return int(self.window_tokens * 0.95)

    def validate(self, max_output_tokens: int) -> None:
        """校验预算表自洽，启动期就报错而不是等撞墙。

        :param max_output_tokens: 模型单次最大输出 token，用于校验输出预留
        :raises ValueError: 加总超窗口、输出预留不足或压缩阈值越界时抛出
        """
        # 1. 五段加总不得超过可用窗口
        total = self.system + self.tools + self.history + self.retrieval + self.output_reserve
        if total > self.usable_tokens:
            raise ValueError(
                "上下文预算加总 %d 超过可用窗口 %d" % (total, self.usable_tokens)
            )

        # 2. 输出预留至少 1.5 倍最大输出，禁止占满窗口再让模型说话
        floor = int(max_output_tokens * 1.5)
        if self.output_reserve < floor:
            raise ValueError("输出预留 %d 低于下限 %d（1.5 × 最大输出）" % (self.output_reserve, floor))

        # 3. 压缩阈值必须在 0.6~0.9，太晚压缩等于没压缩
        if not 0.6 <= self.compress_at <= 0.9:
            raise ValueError("compress_at 应在 0.6~0.9，当前 %s" % self.compress_at)


@dataclass(frozen=True)
class EvalSettings:
    """评测入口配置，0.0 第 8 项的判据。"""

    enabled: bool
    smoke_dir: str
    gate_min_pass_rate: float


@dataclass(frozen=True)
class RetrievalSettings:
    """检索参数。上限与阈值都必须可配，写死在代码里等于没有阈值。"""

    top_k: int                # 单次检索最多返回条数，防止一次把库塞进上下文
    similarity_threshold: int  # 最低命中分，低于该分按未命中处理，不得降级编造条款


@dataclass(frozen=True)
class IdempotencySettings:
    """写类工具的幂等配置。窗口必须覆盖整个任务时长与重试余量，否则重试会重复落库。"""

    enabled: bool   # 关闭后写类工具退化为无保护，只允许排障时临时关
    dir: str        # 存储目录，相对项目根；不进版本库
    window_s: int   # 去重窗口（秒），窗口内同键只落一次


@dataclass(frozen=True)
class SessionSettings:
    """会话快照配置。过期时间必须可配，写死在代码里等于没有生命周期管理。"""

    snapshot_ttl_h: int = 24   # 快照保留时长（小时），超过只能新开会话


@dataclass(frozen=True)
class Settings:
    """聚合配置。全部字段只读，运行时不允许被改写。"""

    name: str
    version: str
    prompt_version: str
    model: ModelSettings
    budget: BudgetSettings
    timeout: TimeoutSettings
    safety: SafetySettings
    eval: EvalSettings
    retrieval: RetrievalSettings
    idempotency: IdempotencySettings
    context: ContextSettings
    project_root: Path
    # 会话快照配置：放在最后并给默认值，避免老配置文件缺这一节时直接崩
    session: SessionSettings = SessionSettings()

    @property
    def prompt_text(self) -> str:
        """读取外置提示词正文，去掉 frontmatter。

        提示词必须在文件里（第 7 项），且版本号与配置一致，否则启动即报错。
        """
        path = self.project_root / "config" / "prompts" / "system.md"
        raw = path.read_text(encoding="utf-8")
        if not raw.startswith("---"):
            raise ValueError("提示词缺少 frontmatter 版本段：%s" % path)
        _, front, body = raw.split("---", 2)
        if ("version: %s" % self.prompt_version) not in front:
            raise ValueError(
                "提示词版本与配置不一致：配置 %s，文件 %s"
                % (self.prompt_version, front.strip())
            )
        return body.strip()


def load_settings(config_path: Path | None = None) -> Settings:
    """加载配置。环境变量可覆盖密钥与 provider，其余走 TOML。"""
    path = config_path or (PROJECT_ROOT / "config" / "agent.toml")
    with path.open("rb") as fh:
        data = tomllib.load(fh)

    agent = data["agent"]
    model = data["model"]
    budget = data["budget"]
    timeout = data["timeout"]
    safety = data["safety"]
    ev = data["eval"]
    retrieval = data["retrieval"]
    idem = data["idempotency"]
    ctx = data["context"]
    # 会话快照配置允许缺省：老配置文件没有这一节时按默认 24 小时处理，不能因此起不来
    sess = data.get("session") or {}

    # 预算表在装配期校验：加总超窗口或输出预留不足，启动就失败，不留到运行期撞墙
    context = ContextSettings(
        window_tokens=int(ctx["window_tokens"]),
        system=int(ctx["system"]),
        tools=int(ctx["tools"]),
        history=int(ctx["history"]),
        retrieval=int(ctx["retrieval"]),
        output_reserve=int(ctx["output_reserve"]),
        compress_at=float(ctx["compress_at"]),
        keep_recent_messages=int(ctx["keep_recent_messages"]),
    )
    context.validate(int(model["max_output_tokens"]))

    return Settings(
        name=agent["name"],
        version=agent["version"],
        prompt_version=agent["prompt_version"],
        model=ModelSettings(
            provider=os.environ.get("TGA_PROVIDER", model["provider"]),
            name=os.environ.get("TGA_MODEL_NAME", model["name"]),
            temperature=float(model["temperature"]),
            max_output_tokens=int(model["max_output_tokens"]),
            api_base=os.environ.get("TGA_API_BASE", model["api_base"]),
            api_key=os.environ.get("OPENAI_API_KEY", ""),
        ),
        budget=BudgetSettings(
            max_steps=int(budget["max_steps"]),
            max_task_tokens=int(budget["max_task_tokens"]),
            max_cost_cny=float(budget["max_cost_cny"]),
            price_in_cny_per_1k=float(budget["price_in_cny_per_1k"]),
            price_out_cny_per_1k=float(budget["price_out_cny_per_1k"]),
        ),
        timeout=TimeoutSettings(
            llm_ms=int(timeout["llm_ms"]),
            tool_ms=int(timeout["tool_ms"]),
            task_ms=int(timeout["task_ms"]),
            poll_interval_ms=int(data["cancellation"]["poll_interval_ms"]),
        ),
        safety=SafetySettings(
            require_approval=bool(safety["require_approval"]),
            input_filter=bool(safety["input_filter"]),
            output_filter=bool(safety["output_filter"]),
            external_content_isolation=bool(safety["external_content_isolation"]),
            approval_ttl_s=int(safety["approval_ttl_s"]),
        ),
        eval=EvalSettings(
            enabled=bool(ev["enabled"]),
            smoke_dir=str(ev["smoke_dir"]),
            gate_min_pass_rate=float(ev["gate_min_pass_rate"]),
        ),
        retrieval=RetrievalSettings(
            top_k=int(retrieval["top_k"]),
            similarity_threshold=int(retrieval["similarity_threshold"]),
        ),
        idempotency=IdempotencySettings(
            enabled=bool(idem["enabled"]),
            dir=str(idem["dir"]),
            window_s=int(idem["window_s"]),
        ),
        context=context,
        project_root=PROJECT_ROOT,
        session=SessionSettings(snapshot_ttl_h=int(sess.get("snapshot_ttl_h") or 24)),
    )
