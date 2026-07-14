"""首日未达标策略 (reason=10)。

对齐 engine.py:264-273 的触发 + engine.py:312-315 的执行价（按 Close）。
首个可交易日的最后一根 bar, 持仓期最高价涨幅 < 目标 → 强制卖出。
"""

from __future__ import annotations

import math
from typing import List

from .base import ExitStrategy, TriggerResult
from ..state import Bar, Context, Position


class FirstDayStrategy:
    """首日未达标: 入场次日（首个可交易日）收盘时, 日内最高涨幅 < target 即卖。"""

    name = "first_day"

    def __init__(self, target: float):
        self.target = float(target)

    def check(self, pos: Position, bar: Bar, ctx: Context) -> List[TriggerResult]:
        current_day = ctx.bar_index // ctx.bpday
        entry_day = pos.entry_idx // ctx.bpday
        # 第一个可交易日的最后一根 bar（T+1 下即入场次日收盘）
        if current_day != entry_day + 1:
            return []
        if (ctx.bar_index % ctx.bpday) != ctx.bpday - 1:
            return []
        hi = bar.high
        day_high = pos.high_hi if pos.high_hi > 0 else (hi if not math.isnan(hi) else bar.close)
        ep = pos.entry_px
        if day_high <= 0 or ep <= 0:
            return []
        day1_return = (day_high - ep) / ep
        if day1_return >= self.target:
            return []
        return [TriggerResult(
            reason=10, strategy_name=self.name, execution_price=bar.close,
        )]
