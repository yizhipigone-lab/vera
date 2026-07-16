"""EquityTracker — 权益曲线计算。

候选 A 阶段 2 — stage 3。从 _simulate_core_v3 抽离权益计算块
（engine.py:540-557）。期末不平仓, 按市值计入。
"""

from __future__ import annotations

import numpy as np

from .state import PositionBook


class EquityTracker:
    """持有 equity_arr, 每 bar 更新 = cash + 持仓市值。"""

    def __init__(self, n_dates: int = 0):
        self.equity_arr = np.empty(n_dates, dtype=np.float64)

    def reset(self, n_dates: int) -> None:
        """按实际 bar 数重建 equity_arr（BacktestLoop.run 调用）。"""
        self.equity_arr = np.empty(n_dates, dtype=np.float64)

    def update(self, i: int, cash: float, price_np: np.ndarray,
               book: PositionBook) -> None:
        """engine.py:540-547: equity_arr[i] = cash + sum(shares*close)。"""
        pv = 0.0
        for p in range(book.count):
            ci = book.code_arr[p]
            if ci >= 0:
                px = price_np[i, ci]
                if not np.isnan(px):
                    pv += book.shares_arr[p] * px
        self.equity_arr[i] = cash + pv

    def finalize(self, last: int, cash: float, price_np: np.ndarray,
                 book: PositionBook) -> None:
        """engine.py:549-557: 期末不平仓, 按市值计入最末 bar。

        注意: equity_arr[last] 在循环内已被 update(last) 写过一次（当日买入后的权益），
        此处重写为期末持仓市值（如果当日有卖出，现金已更新），语义上是对最末 bar 的"结算修正"。
        对于末 bar 无交易的情况，update 写的值和 finalize 写的值相同（冗余但无害）。
        """
        pv = 0.0
        for p in range(book.count):
            ci = book.code_arr[p]
            if ci >= 0:
                px = price_np[last, ci]
                if not np.isnan(px):
                    pv += book.shares_arr[p] * px
        self.equity_arr[last] = cash + pv
