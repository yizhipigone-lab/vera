"""BacktestLoop — 主回测循环。

候选 A 阶段 2 — stage 3。把 _simulate_core_v3_legacy 的主循环
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
from typing import List, Optional, Sequence, Tuple

import numpy as np

from .state import (
    BacktestParams, Context, PositionBook, TradeBuffer, Bar,
)
from .strategies.base import AbsoluteStrategy, TriggerResult
from .exit_engine import ExitDispatcher
from .absolute import FormulaSellStrategy
from .entry import EntryEngine
from .equity import EquityTracker
from .signals import precompute_signal_lists
from .prefilter import TriggerPreFilter

from utils.logger import get_logger
# 2026-07-18: 协作式停止 (web「停止回测」按钮)。CLI/批量脚本从不置位, 行为不变。
from core.stop_flag import raise_if_stopped

logger = get_logger(__name__)


class BacktestLoop:
    """主回测循环协调器。~150 行, 只剩协调逻辑。"""

    def __init__(self, params: BacktestParams,
                 dispatcher: ExitDispatcher,
                 absolutes: Sequence[AbsoluteStrategy],
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
        self._has_run = False  # 2026-07-18 F2: 单实例单次使用守卫
        # 2026-07-18 Phase 3: 触发预筛 (跳过数学上不可能触发的持仓评估, 实测空跑率 84.7%)
        self._prefilter = TriggerPreFilter(dispatcher, absolutes)

    def run(self, price_np: np.ndarray, entry_np: np.ndarray,
            high_np: Optional[np.ndarray], low_np: Optional[np.ndarray],
            open_np: Optional[np.ndarray],
            tradable_np: Optional[np.ndarray],
            last_tradable_idx: Optional[np.ndarray],
            formula_exit_np: Optional[np.ndarray]) -> Tuple[np.ndarray, np.ndarray]:
        """跑完整回测, 返回 (equity_arr, raw_trades)。

        formula_exit_np: 信号已存于 FormulaSellStrategy(absolutes), 此参数保留作
        显式传递/未来扩展, run 内不直接引用（LOW-2）。

        2026-07-18 审计 F2: 单实例不可复用 — PositionBook 与 entry skip 计数
        不重置, 二次 run 会带上一轮持仓/计数, 资金数字静默错乱。fail-fast。
        """
        if self._has_run:
            raise RuntimeError(
                "BacktestLoop 实例不可复用: run() 已被调用过。请重新 build_backtest_loop() "
                "(PositionBook/skipped_signal_count 不重置, 复用会产出静默错误结果)")
        self._has_run = True
        n_dates = price_np.shape[0]
        n_stocks = price_np.shape[1]
        p = self.params
        cash = float(p.initial_capital)
        trade_buf = TradeBuffer(n_dates, n_stocks)
        book = self.position_book
        equity = self.equity_tracker
        equity.reset(n_dates)

        # 2026-07-17 Phase 1 项2: 入场信号一次预计算 (替代每 bar 全列扫描)。
        # 2026-07-18 审计 F1: 抽公共 helper + count_nonzero(按个数切段),
        # 非二值矩阵不再静默错位 (与 legacy truthy 语义一致)。
        sig_by_bar = precompute_signal_lists(entry_np)

        for i in range(n_dates):
            # 停止回测按钮: is_set() 开销 ~几十 ns, 逐 bar 检查可秒级响应
            raise_if_stopped()
            # ── 1. 卖出 ──
            cash = self._sell_bar(i, cash, book, trade_buf, price_np, high_np,
                                  low_np, open_np, tradable_np, last_tradable_idx)
            # ── 2. 买入 ──
            prev_eq = equity.equity_arr[i - 1] if i > 0 else float(p.initial_capital)
            cash = self.entry_engine.run_bar(i, cash, book, trade_buf, price_np,
                                             entry_np, tradable_np, prev_eq,
                                             sig_cis=sig_by_bar[i])
            # ── 3. 权益 ──
            equity.update(i, cash, price_np, book)

        # ── 4. 期末不平仓 ──
        equity.finalize(n_dates - 1, cash, price_np, book)
        # 2026-07-21: 期末未平仓持仓快照 (用户决策: 请求区间终点仍持仓的,
        # 按市值统计不平仓不强平)。挂实例属性, engine run() 路径读回导出
        # open_positions; _simulate_core_v3 冻结壳签名/返回不变。
        final_positions = []
        for pp in range(book.count):
            pos = book.get(pp)
            if pos.code >= 0:
                final_positions.append(pos)
        self.final_positions = final_positions
        # F7 [H4]: 汇总 entry 被停牌/数据缺失 skip 的告警 (补圆"不静默吞信号")
        skipped = getattr(self.entry_engine, "skipped_signal_count", 0)
        if skipped:
            logger.warning(
                "entry_signal_skip: 共 %d 个入场信号因停牌/数据缺失被跳过 (无成交, 见 _build_entry_signals 告警)",
                skipped)
        return equity.equity_arr, trade_buf.to_array()

    # ─────────────────────────────────────────────────────────
    def _sell_bar(self, i, cash, book, trade_buf, price_np, high_np,
                  low_np, open_np, tradable_np, last_tradable_idx) -> float:
        """_simulate_core_v3_legacy 卖出段的移植。返回更新后的 cash。"""
        p = self.params
        bpday = p.bpday
        slippage = p.slippage
        comm_factor = 1.0 - p.commission - p.stamp_tax
        # 2026-07-17 Phase 1 项3b: 热路径提升为局部变量 (每 bar 每持仓省 6+ 次 property 调用,
        # profile 实测这些 accessor 被调 4 万+ 次)。数组引用语义, remove_swap_pop 就地改内容
        # 不换数组对象, 局部引用全程有效。
        code_arr = book.code_arr
        shares_arr = book.shares_arr
        entry_px_arr = book.entry_px_arr
        entry_idx_arr = book.entry_idx_arr
        high_px_arr = book.high_px_arr
        high_hi_arr = book.high_hi_arr
        ladder_done_arr = book.ladder_done_arr
        pp = 0
        while pp < book.count:
            ci = int(code_arr[pp])
            if ci < 0:
                pp += 1
                continue
            xp = price_np[i, ci]
            # ── 退市/停牌 ──
            if (tradable_np is not None and ci < tradable_np.shape[1]
                    and not tradable_np[i, ci]):
                if (last_tradable_idx is not None and last_tradable_idx[ci] >= 0
                        and i > last_tradable_idx[ci]):
                    # 退市: 强制平仓 (对齐 _simulate_core_v3_legacy 退市段)
                    total_sh = shares_arr[pp]
                    ep_d = entry_px_arr[pp]
                    sell_price = xp if xp > 0 else ep_d
                    sell_eff = sell_price * (1.0 - slippage)
                    gross = total_sh * sell_eff * comm_factor
                    cash += gross
                    ret = (sell_price - ep_d) / ep_d if ep_d > 0.0 else 0.0
                    trade_buf.append(ci, entry_idx_arr[pp], i, ep_d,
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
            if (i // bpday) == (entry_idx_arr[pp] // bpday):
                if high_np is not None:
                    hi_t = high_np[i, ci]
                    if hi_t > high_hi_arr[pp]:
                        high_hi_arr[pp] = hi_t
                if xp > high_px_arr[pp]:
                    high_px_arr[pp] = xp
                pp += 1
                continue

            # ── 计算衍生量 (engine.py:146-167) ──
            ep = entry_px_arr[pp]
            pp_ret = (xp - ep) / ep if ep > 0.0 else 0.0
            hp = max(high_px_arr[pp], xp)
            high_px_arr[pp] = hp
            hi = high_np[i, ci] if high_np is not None else xp
            lo = low_np[i, ci] if low_np is not None else xp
            hi_pp = (hi - ep) / ep if ep > 0.0 else 0.0
            lo_pp = (lo - ep) / ep if ep > 0.0 else 0.0
            if high_np is not None and hi > high_hi_arr[pp]:
                high_hi_arr[pp] = hi
            peak_hi = high_hi_arr[pp] if high_hi_arr[pp] > 0 else ep
            peak_hi_profit = (peak_hi - ep) / ep if ep > 0.0 else 0.0
            hold_days = i - entry_idx_arr[pp]

            # ── Phase 3 预筛: 数学上不可能触发 → 跳过对象构造与策略评估 ──
            # 状态安全: high_px/high_hi 已在上方更新; ladder_done 只在真触发时变;
            # 零触发时 evaluate 后写回原值, 与跳过等价。推导见
            # docs/audit/2026-07-18_Phase3预筛条件推导.md
            if not self._prefilter.could_trigger(
                    ci=ci, i=i, ep=ep, hi=hi, lo=lo, hi_pp=hi_pp, lo_pp=lo_pp,
                    peak_hi=peak_hi, peak_hi_profit=peak_hi_profit,
                    hold_days=hold_days, entry_idx=entry_idx_arr[pp], bpday=bpday,
                    ladder_done=int(ladder_done_arr[pp]),
                    ladder_profits=self.ladder_profits, n_ladder=self.n_ladder):
                pp += 1
                continue

            # open_np=None 时 engine.py 不做跳空保护 → 传 NaN 让 CostStopStrategy 跳过
            op = open_np[i, ci] if open_np is not None else float("nan")

            pos = book.get(pp)
            bar = Bar(close=xp, high=hi, low=lo, open=op)
            ctx = Context(
                bar_index=i, ci=ci, bpday=bpday, hold_days=hold_days,
                entry_px=ep, pp=pp_ret, hp_profit=(hp - ep) / ep if ep > 0.0 else 0.0,
                peak_hi=peak_hi, peak_hi_profit=peak_hi_profit,
                pos_high_px=high_px_arr[pp], pos_high_hi=high_hi_arr[pp],
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
                        r[0], pp, ci, i, ep, book, trade_buf, cash, slippage, comm_factor)
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
                    results[0], pp, ci, i, ep, book, trade_buf, cash, slippage, comm_factor)
                if action == "keep":
                    pp += 1
                continue
            else:
                # 双触发: [ladder 部分卖, trailing/cost 全卖剩余] (engine.py:346-384)
                # M6 不变量: results[0] 必为 ladder 部分卖(is_partial=True), dispatcher
                # 仅在 ladder_partial 存在时才追加第二结果, 故此处 tr0.is_partial 恒真。
                cash = self._execute_dual(
                    results, pp, ci, i, ep, book, trade_buf, cash, slippage, comm_factor)
                # 清仓 (swap-and-pop), 不 pp+=1
                continue
        return cash

    # ─────────────────────────────────────────────────────────
    def _execute_single(self, tr: TriggerResult, pp, ci, i, ep, book,
                        trade_buf, cash, slippage, comm_factor):
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
                gross = sell_sh * sell_eff * comm_factor
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
        gross = total_sh * sell_eff * comm_factor
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
                      cash, slippage, comm_factor):
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
                gross = sell_sh * sell_eff * comm_factor
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
            gross = remaining * sell_eff * comm_factor
            cash += gross
            ret1 = (tr1.execution_price - ep) / ep if ep > 0.0 else 0.0
            trade_buf.append(ci, book.entry_idx_arr[pp], i, ep,
                             tr1.execution_price, remaining,
                             gross - remaining * ep, ret1, tr1.reason)
        # 3. 清仓
        book.remove_swap_pop(pp)
        return cash
