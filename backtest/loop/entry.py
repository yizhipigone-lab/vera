"""EntryEngine — 买入循环 + 换股 (reason=1)。

候选 A 阶段 2 — stage 3。从 _simulate_core_v3_legacy 抽离买入块。

铁律（CLAUDE.md 业务铁律 2）: 尾盘选股 → 信号日 T 收盘价买入。entry_np[i,ci]=True
→ 以 price_np[i] (当日收盘) 成交。

换股 (reason=1) 特殊: 卖旧仓用 gross = sh*bp*(1-commission), 无滑点无印花税
（与正常卖出 (1-slippage)*(1-commission-stamp_tax) 不同, 必须精确保留）。

迭代 1 (2026-07-15): 显式声明走 BACKTEST_T_CLOSE 路径, 防止与未来 sim_trader T+1 路径混用.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from backtest._entry_basis import EntryPath

from .state import BacktestParams, PositionBook, TradeBuffer

# 业务铁律 2 — 本模块入场价口径单一真相源
ENTRY_PATH: EntryPath = EntryPath.BACKTEST_T_CLOSE


class EntryEngine:
    """每 bar 的买入循环: 换股先卖旧 + 新仓买入。

    buy_price_np (2026-08-20, open_t1 口径): 提供时买入价取该矩阵而非
    price_np (收盘价) — 调用方传 open 矩阵, 配合 entry_next_open.py 平移后的
    信号实现"T+1 开盘价买入"。None = 老行为 (收盘价), 零行为变化。
    entry_path 与 buy_price_np 必须配对: 传了 buy_price_np 就必须声明
    EntryPath.BACKTEST_T1_OPEN (防两套口径混用, 业务铁律 3)。
    """

    def __init__(self, params: BacktestParams,
                 buy_price_np: Optional[np.ndarray] = None,
                 entry_path: EntryPath = None):
        self.params = params
        if entry_path is None:
            entry_path = (EntryPath.BACKTEST_T1_OPEN if buy_price_np is not None
                          else ENTRY_PATH)
        if buy_price_np is not None and entry_path is not EntryPath.BACKTEST_T1_OPEN:
            raise ValueError(
                "buy_price_np 提供时 entry_path 必须是 BACKTEST_T1_OPEN "
                "(open_t1 口径), 防回测/实盘口径混用")
        self._buy_price_np = buy_price_np
        self.entry_path = entry_path
        # F7 [H4]: entry 因停牌/价缺失被 skip 的计数 (loop 结束汇总告警, 补圆"不静默吞信号")
        self.skipped_signal_count = 0
        # 2026-07-23: 卖出冷却跳过的买入信号计数 (loop 结束汇总)
        self.cooldown_skip_count = 0
        # 2026-08-08: 总仓位上限/连亏冷却 跳过的新仓信号计数 (loop 结束汇总)
        self.exposure_skip_count = 0
        self.halt_skip_count = 0

    def _record_skip(self):
        self.skipped_signal_count += 1

    def run_bar(self, i: int, cash: float, book: PositionBook,
                trade_buf: TradeBuffer, price_np: np.ndarray,
                entry_np: np.ndarray,
                tradable_np: Optional[np.ndarray],
                prev_equity: float,
                sig_cis: Optional[np.ndarray] = None,
                last_exit_bar: Optional[np.ndarray] = None,
                cur_mkt_value: float = 0.0,
                total_equity: float = 0.0,
                halt_until_bar: Optional[int] = None) -> float:
        """_simulate_core_v3_legacy 买入块的移植。返回更新后的 cash。

        sig_cis: 本 bar 有信号的股票列索引(升序)。None 时按旧路径全列扫描
        (兼容直调方); BacktestLoop.run 传入预计算的 np.nonzero 结果
        (2026-07-17 Phase 1 项2: 25.5ms→1.1ms)。升序保证与 range(n_stocks)
        扫描顺序一致 → 同 bar 多信号买入顺序不变 → parity 不破。
        last_exit_bar: 2026-07-23 卖出冷却 — 每票最近一次全清仓 bar 索引
        (BacktestLoop 传入; None = 冷却关闭, 老行为)。
        """
        p = self.params
        if sig_cis is None:
            sig_cis = np.nonzero(entry_np[i])[0]
        for ci in sig_cis:
            ci = int(ci)
            # 信号日停牌 → skip (F7: 计数, 补圆不静默吞信号)
            if tradable_np is not None and ci < tradable_np.shape[1] and not tradable_np[i, ci]:
                self._record_skip()
                continue
            bp = (self._buy_price_np[i, ci] if self._buy_price_np is not None
                  else price_np[i, ci])
            if np.isnan(bp) or bp <= 0.0:
                self._record_skip()
                continue
            entry_i = i
            # ── 换股: 已持有同股票 → 卖旧仓（reason=1, 无滑点无印花税）──
            # 2026-07-18 Phase 3: slot_of O(1) 替代逐槽扫描 (dense 场景 147ms→~10ms)
            old_p = book.slot_of(ci)
            if old_p >= 0:
                os_sh = book.shares_arr[old_p]
                os_ep = book.entry_px_arr[old_p]
                os_ei = book.entry_idx_arr[old_p]
                gross = os_sh * bp * (1.0 - p.commission)  # 仅手续费
                cash += gross
                os_pp = (bp - os_ep) / os_ep if os_ep > 0.0 else 0.0
                trade_buf.append(ci, os_ei, i, os_ep, bp, os_sh,
                                 gross - os_sh * os_ep, os_pp, 1)
                book.remove_swap_pop(old_p)
            # ── 卖出冷却 (2026-07-23): 空仓后的新买, 距上次全清仓不足
            # sell_cooldown_bars → skip。仅约束新买; 上方换股 (old_p>=0,
            # 持仓中卖旧买新) 不触发冷却。
            if (old_p < 0 and last_exit_bar is not None
                    and ci < last_exit_bar.shape[0]
                    and i - int(last_exit_bar[ci]) < p.sell_cooldown_bars):
                self.cooldown_skip_count += 1
                continue
            # ── 2026-08-08 新仓门槛 (仅约束净新仓 old_p<0; 换股 old_p>=0 不拦) ──
            if old_p < 0:
                # 全局连亏冷却期内禁开新仓 (持仓照常止损止盈)
                if halt_until_bar is not None and i < halt_until_bar:
                    self.halt_skip_count += 1
                    continue
                # 总仓位上限: 持仓市值/总权益 >= 上限禁开新仓
                if (p.max_total_exposure < 1.0 and total_equity > 0.0
                        and cur_mkt_value / total_equity >= p.max_total_exposure):
                    self.exposure_skip_count += 1
                    continue
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
            if cost <= cash and book.count < book.max_pos:
                cash -= cost
                book.add(code=ci, shares=float(sh), entry_px=bp, entry_idx=entry_i,
                         high_px=bp, high_hi=bp)
        return cash
