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
    """移动止损/止盈: peak_hi 涨过激活线后, Low 触及回撤线即触发。

    gap_protection (2026-07-21 用户拍板, opt-in 默认关): 跳空低开直接跌穿回撤线时
    按 min(回撤线, 开盘价) 成交——实盘条件单开盘触发只能拿到开盘价, 原语义(始终按
    回撤线价)在 gap bar 上系统性高估成交价。对齐 cost_stop 的跳空保护思路。
    """

    name = "trailing"

    def __init__(self, activation: float, drawdown: float, gap_protection: bool = False):
        self.activation = float(activation)
        self.drawdown = float(drawdown)
        self.gap_protection = bool(gap_protection)

    def check(self, pos: Position, bar: Bar, ctx: Context) -> List[TriggerResult]:
        if ctx.peak_hi_profit < self.activation:
            return []
        trail_line = ctx.peak_hi * (1.0 - self.drawdown)
        if bar.low <= trail_line:
            ep = pos.entry_px
            # M3: 防御 ep==0 除零（上游已挡, 此处独立守卫）
            reason = 8 if (ep > 0.0 and (trail_line - ep) / ep > 0.0) else 4
            exec_px = trail_line
            if self.gap_protection and ctx.peak_hi > bar.high and bar.open < trail_line:
                # 隔夜跳空跌穿"已被更早 bar 激活"的回撤线: 实盘只能按开盘价成交。
                # 判别依据: peak_hi 来自更早 bar(> 本 bar high), 且开盘价已低于线。
                # 若本 bar 自己创出峰值(peak_hi == bar.high), 属同 bar 顺序不可知,
                # 保持原线价语义(那是另一个已知乐观源, 不属跳空保护范围)。
                exec_px = bar.open
            return [TriggerResult(
                reason=reason, strategy_name=self.name, execution_price=exec_px,
            )]
        return []
