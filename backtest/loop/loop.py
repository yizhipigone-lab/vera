"""BacktestLoop — 主回测循环。

候选 A 阶段 2 — stage 3。把 _simulate_core_v3 (engine.py:32-558) 的主循环
端口成: PositionBook + ExitDispatcher + AbsoluteStrategy + EntryEngine + EquityTracker。

设计（v3 计划书 §2.1）:
- 每 bar 顺序: ① 退市驱逐(reason=11) ② 停牌跳过 ③ T+1跳过 ④ formula_sell绝对优先
  ⑤ dispatcher.evaluate()→List[TriggerResult] ⑥ 执行(部分卖/全卖/双触发)
- 策略只读 pos/bar/ctx 返回 TriggerResult; loop 负责执行(cash/trade/仓位)。
- ladder_done 由 ladder 策略 mutate pos 快照, evaluate 后 loop 写回 book。
- cash 归 loop 持有（HA3）。
"""

from __future__ import annotations

import math

import numpy as np

from .state import (
    BacktestParams, Context, PositionBook, TradeBuffer, Bar,
)
from .strategies.base import TriggerResult
from .exit_engine import ExitDispatcher
from .absolute import FormulaSellStrategy
from .entry import EntryEngine
from .equity import EquityTracker


class BacktestLoop:
    """主回测循环协调器。~150 行, 只剩协调逻辑。"""

    def __init__(self, params: BacktestParams,
                 dispatcher: ExitDispatcher,
                 absolutes,
                 entry_engine: EntryEngine,
                 equity_tracker: EquityTracker,
                 position_book: PositionBook,
                 ladder_profits: np.ndarray,
                 ladder_ratios: np.ndarray,
                 n_ladder: int):
        self.params = params
        self.dispatcher = dispatcher
        self.absolutes = absolutes
        self.entry_engine = entry_engine
        self.equity_tracker = equity_tracker
        self.position_book = position_book
        self.ladder_profits = ladder_profits
        self.ladder_ratios = ladder_ratios
        self.n_ladder = n_ladder

    def run(self, price_np, entry_np, high_np, low_np, open_np,
            tradable_np, last_tradable_idx,
            formula_exit_np) -> tuple:
        """跑完整回测, 返回 (equity_arr, raw_trades)。"""
        n_dates = price_np.shape[0]
        n_stocks = price_np.shape[1]
        p = self.params
        cash = float(p.initial_capital)
        trade_buf = TradeBuffer(n_dates, n_stocks)
        book = self.position_book
        equity = self.equity_tracker
        equity.reset(n_dates)

        for i in range(n_dates):
            # ── 1. 卖出 ──
            cash = self._sell_bar(i, cash, book, trade_buf, price_np, high_np,
                                  low_np, open_np, tradable_np, last_tradable_idx)
            # ── 2. 买入 ──
            prev_eq = equity.equity_arr[i - 1] if i > 0 else float(p.initial_capital)
            cash = self.entry_engine.run_bar(i, cash, book, trade_buf, price_np,
                                             entry_np, tradable_np, prev_eq)
            # ── 3. 权益 ──
            equity.update(i, cash, price_np, book)

        # ── 4. 期末不平仓 ──
        equity.finalize(n_dates - 1, cash, price_np, book)
        return equity.equity_arr, trade_buf.to_array()

    # ─────────────────────────────────────────────────────────
    def _sell_bar(self, i, cash, book, trade_buf, price_np, high_np,
                  low_np, open_np, tradable_np, last_tradable_idx) -> float:
        """engine.py:91-463 的卖出循环。返回更新后的 cash。"""
        p = self.params
        bpday = p.bpday
        slippage = p.slippage
        comm_stamp = p.commission + p.stamp_tax
        pp = 0
        while pp < book.count:
            ci = int(book.code_arr[pp])
            if ci < 0:
                pp += 1
                continue
            xp = price_np[i, ci]
            # ── 退市/停牌 ──
            if (tradable_np is not None and ci < tradable_np.shape[1]
                    and not tradable_np[i, ci]):
                if (last_tradable_idx is not None and last_tradable_idx[ci] >= 0
                        and i > last_tradable_idx[ci]):
                    # 退市: 强制平仓 (engine.py:99-129)
                    total_sh = book.shares_arr[pp]
                    ep_d = book.entry_px_arr[pp]
                    sell_price = xp if xp > 0 else ep_d
                    sell_eff = sell_price * (1.0 - slippage)
                    gross = total_sh * sell_eff * (1.0 - comm_stamp)
                    cash += gross
                    ret = (sell_price - ep_d) / ep_d if ep_d > 0.0 else 0.0
                    trade_buf.append(ci, book.entry_idx_arr[pp], i, ep_d,
                                     sell_price, total_sh, gross - total_sh * ep_d,
                                     ret, 11)
                    book.remove_swap_pop(pp)
                    continue  # 不 pp+=1
                # 临时停牌: 跳过卖出检查
                pp += 1
                continue
            if math.isnan(xp) or xp <= 0.0:
                pp += 1
                continue
            # ── T+1: 当日买入不可当日卖 ──
            if (i // bpday) == (book.entry_idx_arr[pp] // bpday):
                if high_np is not None:
                    hi_t = high_np[i, ci]
                    if hi_t > book.high_hi_arr[pp]:
                        book.high_hi_arr[pp] = hi_t
                if xp > book.high_px_arr[pp]:
                    book.high_px_arr[pp] = xp
                pp += 1
                continue

            # ── 计算衍生量 (engine.py:146-167) ──
            ep = book.entry_px_arr[pp]
            pp_ret = (xp - ep) / ep if ep > 0.0 else 0.0
            hp = max(book.high_px_arr[pp], xp)
            book.high_px_arr[pp] = hp
            hi = high_np[i, ci] if high_np is not None else xp
            lo = low_np[i, ci] if low_np is not None else xp
            hi_pp = (hi - ep) / ep if ep > 0.0 else 0.0
            lo_pp = (lo - ep) / ep if ep > 0.0 else 0.0
            if high_np is not None and hi > book.high_hi_arr[pp]:
                book.high_hi_arr[pp] = hi
            peak_hi = book.high_hi_arr[pp] if book.high_hi_arr[pp] > 0 else ep
            peak_hi_profit = (peak_hi - ep) / ep if ep > 0.0 else 0.0
            hold_days = i - book.entry_idx_arr[pp]
            op = open_np[i, ci] if open_np is not None else xp

            pos = book.get(pp)
            bar = Bar(close=xp, high=hi, low=lo, open=op)
            ctx = Context(
                bar_index=i, ci=ci, bpday=bpday, hold_days=hold_days,
                entry_px=ep, pp=pp_ret, hp_profit=(hp - ep) / ep if ep > 0.0 else 0.0,
                peak_hi=peak_hi, peak_hi_profit=peak_hi_profit,
                pos_high_px=book.high_px_arr[pp], pos_high_hi=book.high_hi_arr[pp],
                hi_pp=hi_pp, lo_pp=lo_pp,
                ladder_profits=self.ladder_profits,
                ladder_ratios=self.ladder_ratios, n_ladder=self.n_ladder,
            )

            # ── 1. 绝对优先 (formula_sell, reason=12) ──
            fired = False
            for abs_s in self.absolutes:
                r = abs_s.check(pos, bar, ctx)
                if r:
                    cash, action = self._execute_single(
                        r[0], pp, ci, i, ep, book, trade_buf, cash, slippage, comm_stamp)
                    fired = True
                    break
            if fired:
                if action == "keep":
                    pp += 1
                # clear: swap-and-pop 已在 _execute_single 完成, 不 pp+=1
                continue

            # ── 2. dispatcher ──
            results = self.dispatcher.evaluate(pos, bar, ctx)
            # 写回 ladder_done（ladder 策略可能 mutate 了 pos 快照）
            book.set_ladder_done(pp, pos.ladder_done)

            if not results:
                pp += 1
                continue

            if len(results) == 1:
                cash, action = self._execute_single(
                    results[0], pp, ci, i, ep, book, trade_buf, cash, slippage, comm_stamp)
                if action == "keep":
                    pp += 1
                continue
            else:
                # 双触发: [ladder 部分卖, trailing/cost 全卖剩余] (engine.py:346-384)
                cash = self._execute_dual(
                    results, pp, ci, i, ep, book, trade_buf, cash, slippage, comm_stamp)
                # 清仓 (swap-and-pop), 不 pp+=1
                continue
        return cash

    # ─────────────────────────────────────────────────────────
    def _execute_single(self, tr: TriggerResult, pp, ci, i, ep, book,
                        trade_buf, cash, slippage, comm_stamp):
        """执行单触发。返回 (cash, action)。action ∈ {'keep','clear'}。

        keep  = 部分卖, 保留仓位 (pp+=1)
        clear = 全卖, swap-and-pop (不 pp+=1)
        """
        p = self.params
        total_sh = book.shares_arr[pp]
        if tr.is_partial:
            sell_sh = int(total_sh * tr.sell_ratio)
            sell_sh = max((sell_sh // p.lot_size) * p.lot_size, p.lot_size)
            if sell_sh < total_sh:
                sell_eff = tr.execution_price * (1.0 - slippage)
                gross = sell_sh * sell_eff * (1.0 - comm_stamp)
                cash += gross
                ret = self._ret(tr, ep)
                trade_buf.append(ci, book.entry_idx_arr[pp], i, ep,
                                 tr.execution_price, sell_sh,
                                 gross - sell_sh * ep, ret, tr.reason)
                book.set_shares(pp, total_sh - sell_sh)
                return cash, "keep"
            # sell_sh >= total_sh → 落到全卖
        # 全卖
        sell_eff = tr.execution_price * (1.0 - slippage)
        gross = total_sh * sell_eff * (1.0 - comm_stamp)
        cash += gross
        ret = self._ret(tr, ep)
        trade_buf.append(ci, book.entry_idx_arr[pp], i, ep,
                         tr.execution_price, total_sh,
                         gross - total_sh * ep, ret, tr.reason)
        book.remove_swap_pop(pp)
        return cash, "clear"

    @staticmethod
    def _ret(tr: TriggerResult, ep: float) -> float:
        """收益率: ladder 用精确 tp_profit（engine.py:303）, 其余重算 (engine.py:290/311/315)。"""
        if tr.actual_return is not None:
            return tr.actual_return
        return (tr.execution_price - ep) / ep if ep > 0.0 else 0.0

    def _execute_dual(self, results, pp, ci, i, ep, book, trade_buf,
                      cash, slippage, comm_stamp):
        """执行双触发: ladder 部分卖 + trailing/cost 全卖剩余。返回 cash。

        对齐 engine.py:346-384。两笔交易同 bar, 最后清仓。
        """
        p = self.params
        total_sh = book.shares_arr[pp]
        tr0 = results[0]  # ladder 部分卖
        tr1 = results[1]  # trailing/cost 全卖剩余
        remaining = total_sh
        # 1. ladder 部分卖
        if tr0.is_partial:
            sell_sh = int(total_sh * tr0.sell_ratio)
            sell_sh = max((sell_sh // p.lot_size) * p.lot_size, p.lot_size)
            if 0 < sell_sh < total_sh:
                sell_eff = tr0.execution_price * (1.0 - slippage)
                gross = sell_sh * sell_eff * (1.0 - comm_stamp)
                cash += gross
                ret0 = (tr0.execution_price - ep) / ep if ep > 0.0 else 0.0
                trade_buf.append(ci, book.entry_idx_arr[pp], i, ep,
                                 tr0.execution_price, sell_sh,
                                 gross - sell_sh * ep, ret0, tr0.reason)
                remaining = total_sh - sell_sh
                book.set_shares(pp, remaining)
        # 2. 全卖剩余 (trailing/cost)
        if remaining > 0:
            sell_eff = tr1.execution_price * (1.0 - slippage)
            gross = remaining * sell_eff * (1.0 - comm_stamp)
            cash += gross
            ret1 = (tr1.execution_price - ep) / ep if ep > 0.0 else 0.0
            trade_buf.append(ci, book.entry_idx_arr[pp], i, ep,
                             tr1.execution_price, remaining,
                             gross - remaining * ep, ret1, tr1.reason)
        # 3. 清仓
        book.remove_swap_pop(pp)
        return cash
