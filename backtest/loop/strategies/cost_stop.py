"""硬止损策略 (reason=3)。

对齐 engine.py:211/228/238 的 cost_stop 触发条件 + engine.py:281-290 的执行价。
"""

from __future__ import annotations

import math
from typing import List

from .base import ExitStrategy, TriggerResult
from ..state import Bar, Context, Position


class CostStopStrategy:
    """硬止损: 当根 Low 跌破阈值即触发。

    触发: lo_pp <= threshold  （lo_pp = (low-entry)/entry）
    执行价: stop_price = entry*(1+threshold); 跳空低开取 min(stop_price, open)
    """

    name = "cost_stop"

    def __init__(self, threshold: float):
        self.threshold = float(threshold)

    def check(self, pos: Position, bar: Bar, ctx: Context) -> List[TriggerResult]:
        if ctx.lo_pp <= self.threshold:
            ep = pos.entry_px
            stop_price = ep * (1.0 + self.threshold)
            # 跳空保护（engine.py:285-288）: open < stop_price 时按 open 成交
            op = bar.open
            if not math.isnan(op) and op < stop_price:
                stop_price = op
            return [TriggerResult(
                reason=3, strategy_name=self.name, execution_price=stop_price,
            )]
        return []
