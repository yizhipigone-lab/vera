"""移动止损/止盈策略 (reason=4 loss / reason=8 profit)。

对齐 engine.py:206-209/231-234/251-254 的触发 + engine.py:304-311 的执行价。

语义（2026-07-05 v3 改造）: Low 触及回撤线即触发, 按回撤线价成交（盘中锁利）。
  trail_line = peak_hi * (1 - drawdown)
  reason = 8（移动止盈）if (trail_line-ep)/ep > 0 else 4（移动止损）
不做跳空保护: 始终按回撤线价（与 cost_stop 不同）。
"""

from __future__ import annotations

from typing import List

from .base import ExitStrategy, TriggerResult
from ..state import Bar, Context, Position


class TrailingStrategy:
    """移动止损/止盈: peak_hi 涨过激活线后, Low 触及回撤线即触发。"""

    name = "trailing"

    def __init__(self, activation: float, drawdown: float):
        self.activation = float(activation)
        self.drawdown = float(drawdown)

    def check(self, pos: Position, bar: Bar, ctx: Context) -> List[TriggerResult]:
        if ctx.peak_hi_profit < self.activation:
            return []
        trail_line = ctx.peak_hi * (1.0 - self.drawdown)
        if bar.low <= trail_line:
            ep = pos.entry_px
            # M3: 防御 ep==0 除零（上游已挡, 此处独立守卫）
            reason = 8 if (ep > 0.0 and (trail_line - ep) / ep > 0.0) else 4
            return [TriggerResult(
                reason=reason, strategy_name=self.name, execution_price=trail_line,
            )]
        return []
