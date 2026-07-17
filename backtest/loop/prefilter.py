"""TriggerPreFilter — 卖出侧触发预筛 (Phase 3, 2026-07-18)。

跳过"本 bar 数学上不可能有任何触发"的持仓评估 (实测空跑率 84.7%)。
推导与零假阴性论证: docs/audit/2026-07-18_Phase3预筛条件推导.md。

设计:
- 条件即触发本身或更宽(保守充分条件), 任一策略拿不准 → 返回 True 走全路径
- 只引用策略的只读参数(threshold/activation/profits 等), 不碰任何可变状态
- 禁用策略(capability gating 未进 dispatcher)不参与判定
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np


class TriggerPreFilter:
    """从 dispatcher 策略 + absolutes 构造, could_trigger() 纯标量短路。"""

    def __init__(self, dispatcher, absolutes: Sequence):
        s = dispatcher.strategies
        self._cost = s.get("cost_stop")
        self._ladder = s.get("ladder_tp")
        self._trailing = s.get("trailing")
        self._time = s.get("time_stop")
        self._cond = s.get("cond_time")
        self._first = s.get("first_day")
        self._atr = s.get("atr_stop")
        self._formula = next(
            (a for a in absolutes if getattr(a, "name", None) == "formula_sell"), None)

    def could_trigger(self, *, ci: int, i: int, ep: float,
                      hi: float, lo: float, hi_pp: float, lo_pp: float,
                      peak_hi: float, peak_hi_profit: float,
                      hold_days: int, entry_idx: int, bpday: int,
                      ladder_done: int,
                      ladder_profits: np.ndarray, n_ladder: int) -> bool:
        """True=可能触发(走全路径); False=数学上不可能触发(可安全跳过)。"""
        # ── formula_sell (绝对优先, 条件即触发本身) ──
        f = self._formula
        if f is not None and f.signal is not None and i >= f.lag_bars \
                and 0 <= ci < f.signal.shape[1] \
                and bool(f.signal[i - f.lag_bars, ci]):
            return True
        # ── cost_stop: lo_pp <= threshold ──
        c = self._cost
        if c is not None and lo_pp <= c.threshold:
            return True
        # ── ladder_tp: hi_pp 达到任一未触发档位 ──
        if self._ladder is not None:
            for li in range(n_ladder):
                if not (ladder_done >> li) & 1 and hi_pp >= ladder_profits[li]:
                    return True
        # ── trailing: 激活 + 回撤线触及 ──
        t = self._trailing
        if t is not None and peak_hi_profit >= t.activation \
                and lo <= peak_hi * (1.0 - t.drawdown):
            return True
        # ── time_stop: 持仓到期 ──
        ts = self._time
        if ts is not None and hold_days >= ts.max_hold_days:
            return True
        # ── cond_time: 到期 + 当根 High 达标 ──
        ct = self._cond
        if ct is not None and hold_days >= ct.days and hi_pp >= ct.profit:
            return True
        # ── first_day: 仅时间条件(保守放宽, 价格条件留给全路径) ──
        fd = self._first
        if fd is not None and bpday >= 1 \
                and (i // bpday) == (entry_idx // bpday) + 1 \
                and (i % bpday) == bpday - 1:
            return True
        # ── atr_stop: atr 有效 + low 触及回撤线 ──
        a = self._atr
        if a is not None and a.atr_matrix is not None \
                and 0 <= i < a.atr_matrix.shape[0] and 0 <= ci < a.atr_matrix.shape[1]:
            atr = a.atr_matrix[i, ci]
            if atr > 0 and lo <= peak_hi - a.multiplier * atr:
                return True
        return False
