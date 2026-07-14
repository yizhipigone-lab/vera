"""时间止损/止盈策略 (reason=6 loss / reason=9 profit)。

对齐 engine.py:258-259 的触发 + engine.py:312-315 的执行价（按 Close）。
"""

from __future__ import annotations

from typing import List

from .base import ExitStrategy, TriggerResult
from ..state import Bar, Context, Position


class TimeStopStrategy:
    """时间止损: 持仓天数 >= max_hold_days 即触发, 按盈亏区分止盈/止损。"""

    name = "time_stop"

    def __init__(self, max_hold_days: int):
        self.max_hold_days = int(max_hold_days)

    def check(self, pos: Position, bar: Bar, ctx: Context) -> List[TriggerResult]:
        if ctx.hold_days < self.max_hold_days:
            return []
        reason = 9 if ctx.pp > 0 else 6  # 9=时间止盈 6=时间止损
        return [TriggerResult(
            reason=reason, strategy_name=self.name, execution_price=bar.close,
        )]
