"""条件时间止盈策略 (reason=7)。

对齐 engine.py:261-262 的触发 + engine.py:312-315 的执行价（按 Close）。
持仓 N 天后, 当根 High 达到盈利目标% 即清仓。
"""

from __future__ import annotations

from typing import List

from .base import ExitStrategy, TriggerResult
from ..state import Bar, Context, Position


class CondTimeStrategy:
    """条件时间止盈: 持仓 >= days 且 hi_pp >= profit 即触发。"""

    name = "cond_time"

    def __init__(self, days: int, profit: float):
        self.days = int(days)
        self.profit = float(profit)

    def check(self, pos: Position, bar: Bar, ctx: Context) -> List[TriggerResult]:
        if ctx.hold_days < self.days or ctx.hi_pp < self.profit:
            return []
        return [TriggerResult(
            reason=7, strategy_name=self.name, execution_price=bar.close,
        )]
