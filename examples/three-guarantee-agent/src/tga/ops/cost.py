"""成本计量与预算控制。

口径来自 agent-ops-standards：prompt / completion 分开记，金额按配置单价算，
预算三层（步数由主循环判，本模块管 token 与金额）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..settings import BudgetSettings


@dataclass
class Usage:
    """单次或累计用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, prompt: int, completion: int) -> None:
        self.prompt_tokens += prompt
        self.completion_tokens += completion


class Budget:
    """单任务预算：超限返回原因，由主循环走「预算耗尽」终止分支。"""

    def __init__(self, settings: BudgetSettings):
        self._s = settings
        self.usage = Usage()

    def record(self, prompt_tokens: int, completion_tokens: int) -> None:
        """每次模型调用后必须调用一次，数据来自 API 返回而非本地估算。"""
        self.usage.add(prompt_tokens, completion_tokens)

    @property
    def cost_cny(self) -> float:
        """按千 token 单价折算金额。"""
        s = self._s
        return (
            self.usage.prompt_tokens / 1000.0 * s.price_in_cny_per_1k
            + self.usage.completion_tokens / 1000.0 * s.price_out_cny_per_1k
        )

    def exceeded(self) -> str | None:
        """返回超限原因（tokens / cost），未超返回 None。"""
        if self.usage.total_tokens > self._s.max_task_tokens:
            return "tokens"
        if self.cost_cny > self._s.max_cost_cny:
            return "cost"
        return None

    def snapshot(self) -> dict[str, float | int]:
        """给日志与结果用的只读快照。"""
        return {
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.completion_tokens,
            "total_tokens": self.usage.total_tokens,
            "cost_cny": round(self.cost_cny, 6),
        }
