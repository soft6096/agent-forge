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
    project_root: Path

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
        project_root=PROJECT_ROOT,
    )
