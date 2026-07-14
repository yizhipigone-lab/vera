"""ATR 波动率止损策略 (reason=13)。

候选 A 阶段 2 — 加新策略范例（docs/architecture/loop.md §6）。

回撤超过 N 倍 ATR（Average True Range, 平均真实波幅）即全卖。
  trail_line = peak_hi - multiplier * ATR
  Low 触及 trail_line → 按 trail_line 成交（与 trailing 同款锁利语义, 不做跳空保护）

ATR 值由调用方预算成 (n_dates, n_stocks) 矩阵传入（策略不自己算 ATR, 关注触发逻辑）。
属新 API 策略: 经 build_backtest_loop(atr_enabled=True, atr_matrix=...) 启用,
不走冻结 39 参的 _simulate_core_v3 兼容壳（legacy 无 ATR）。
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np

from .base import ExitStrategy, TriggerResult
from ..state import Bar, Context, Position


class AtrStopStrategy:
    """ATR 波动率止损: Low 跌破 peak_hi - N*ATR 即全卖。"""

    name = "atr_stop"

    def __init__(self, atr_matrix: Optional[np.ndarray], multiplier: float = 3.0):
        self.atr_matrix = atr_matrix   # (n_dates, n_stocks) float64, 可为 None
        self.multiplier = float(multiplier)

    def check(self, pos: Position, bar: Bar, ctx: Context) -> List[TriggerResult]:
        if self.atr_matrix is None:
            return []
        if not (0 <= ctx.bar_index < self.atr_matrix.shape[0]
                and 0 <= ctx.ci < self.atr_matrix.shape[1]):
            return []
        atr = self.atr_matrix[ctx.bar_index, ctx.ci]
        ep = pos.entry_px
        if not (atr > 0) or ep <= 0:
            return []
        trail_line = ctx.peak_hi - self.multiplier * atr
        if bar.low <= trail_line:
            return [TriggerResult(
                reason=13, strategy_name=self.name, execution_price=trail_line,
            )]
        return []
