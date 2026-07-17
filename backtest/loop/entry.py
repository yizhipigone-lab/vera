"""EntryEngine — 买入循环 + 换股 (reason=1)。

候选 A 阶段 2 — stage 3。从 _simulate_core_v3 抽离买入块（engine.py:465-538）。

铁律（CLAUDE.md 业务铁律 2）: 尾盘选股 → 信号日 T 收盘价买入。entry_np[i,ci]=True
→ 以 price_np[i] (当日收盘) 成交。

换股 (reason=1) 特殊: 卖旧仓用 gross = sh*bp*(1-commission), 无滑点无印花税
（与正常卖出 (1-slippage)*(1-commission-stamp_tax) 不同, 必须精确保留）。

迭代 1 (2026-07-15): 显式声明走 BACKTEST_T_CLOSE 路径, 防止与未来 sim_trader T+1 路径混用.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from backtest._entry_basis import EntryPath, assert_single_path, ENTRY_BASIS_BACKTEST
from .state import BacktestParams, PositionBook, TradeBuffer

# 业务铁律 2 — 本模块入场价口径单一真相源
ENTRY_PATH: EntryPath = EntryPath.BACKTEST_T_CLOSE

MAX_POS = 5000


class EntryEngine:
    """每 bar 的买入循环: 换股先卖旧 + 新仓买入。"""

    def __init__(self, params: BacktestParams):
        self.params = params
        # F7 [H4]: entry 因停牌/价缺失被 skip 的计数 (loop 结束汇总告警, 补圆"不静默吞信号")
        self.skipped_signal_count = 0

    def _record_skip(self, i: int, ci: int):
        self.skipped_signal_count += 1

    def run_bar(self, i: int, cash: float, book: PositionBook,
                trade_buf: TradeBuffer, price_np: np.ndarray,
                entry_np: np.ndarray,
                tradable_np: Optional[np.ndarray],
                prev_equity: float) -> float:
        """engine.py:465-538。返回更新后的 cash。"""
        p = self.params
        n_stocks = price_np.shape[1]
        for ci in range(n_stocks):
            if not entry_np[i, ci]:
                continue
            # 信号日停牌 → skip (F7: 计数, 补圆不静默吞信号)
            if tradable_np is not None and ci < tradable_np.shape[1] and not tradable_np[i, ci]:
                self._record_skip(i, ci)
                continue
            bp = price_np[i, ci]
            if np.isnan(bp) or bp <= 0.0:
                self._record_skip(i, ci)
                continue
            entry_i = i
            # ── 换股: 已持有同股票 → 卖旧仓（reason=1, 无滑点无印花税）──
            for old_p in range(book.count):
                if book.code_arr[old_p] == ci:
                    os_sh = book.shares_arr[old_p]
                    os_ep = book.entry_px_arr[old_p]
                    os_ei = book.entry_idx_arr[old_p]
                    gross = os_sh * bp * (1.0 - p.commission)  # 仅手续费
                    cash += gross
                    os_pp = (bp - os_ep) / os_ep if os_ep > 0.0 else 0.0
                    trade_buf.append(ci, os_ei, i, os_ep, bp, os_sh,
                                     gross - os_sh * os_ep, os_pp, 1)
                    book.remove_swap_pop(old_p)
                    break
            # ── 买入新仓 ──
            buy_amount = min(cash, p.max_buy_amount)
            if p.max_position_pct < 1.0:
                buy_amount = min(buy_amount, prev_equity * p.max_position_pct)
            if buy_amount < p.min_buy_amount:
                continue
            raw_sh = int(buy_amount / bp)
            sh = (raw_sh // p.lot_size) * p.lot_size
            if sh < p.lot_size * p.min_lots:
                continue
            bp_eff = bp * (1.0 + p.slippage)
            cost = sh * bp_eff * (1.0 + p.commission)
            if cost <= cash and book.count < MAX_POS:
                cash -= cost
                book.add(code=ci, shares=float(sh), entry_px=bp, entry_idx=entry_i,
                         high_px=bp, high_hi=bp)
        return cash
