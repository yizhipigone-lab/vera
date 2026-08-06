"""移动止盈 simple / real 模式单测 (2026-08-06, 审计 P1-2 补测试)。

confirm="simple": 1D(bpday==1) 收盘价破线才触发按收盘成交;
  分钟级每 bar Low 触线按该 bar 收盘价成交; 无阶梯休息。
confirm="real" (条件单语义): 本 bar 创峰值跳过; 跳空(open<线)按开盘价;
  Low 触线按线价; 1D/5M 同规则; 无阶梯休息。
"""
from __future__ import annotations

import numpy as np
import pytest

from backtest.loop import Bar, Context, Position, TrailingStrategy
from backtest.loop.prefilter import TriggerPreFilter


def make_pos(entry_px=10.0) -> Position:
    return Position(code=3, shares=1000.0, entry_px=entry_px,
                    entry_idx=0, high_px=entry_px, high_hi=entry_px)


def make_ctx(**kw) -> Context:
    base = dict(
        bar_index=47, ci=0, bpday=48, hold_days=3,
        entry_px=10.0, pp=0.0, hp_profit=0.0,
        peak_hi=11.5, peak_hi_profit=0.15,   # 激活 (>=0.05)
        pos_high_px=11.5, pos_high_hi=11.5,
        hi_pp=0.15, lo_pp=0.0,
        ladder_profits=np.array([0.06, 0.15], dtype=np.float64),
        ladder_ratios=np.array([0.3, 0.3], dtype=np.float64),
        n_ladder=2,
        is_last_bar=False, day_lo=10.0, ladder_fired_today=False,
    )
    base.update(kw)
    return Context(**base)


LINE = 11.5 * 0.99  # 11.385


class TestSimpleMode:
    def test_5m_low_touch_executes_at_bar_close(self):
        """5M: Low 触线即触发, 按该 bar 收盘价成交 (不用线价)。"""
        s = TrailingStrategy(activation=0.05, drawdown=0.01, confirm="simple")
        bar = Bar(close=11.30, high=11.40, low=11.20, open=11.35)
        res = s.check(make_pos(), bar, make_ctx(bpday=48))
        assert len(res) == 1
        assert res[0].execution_price == pytest.approx(11.30)
        assert res[0].reason == 8  # 高于成本 → 止盈

    def test_1d_only_close_counts(self):
        """1D: 盘中下影线破线不算, 收盘价破线才触发。"""
        s = TrailingStrategy(activation=0.05, drawdown=0.01, confirm="simple")
        # 收盘 11.40 在线上方, 尽管最低 11.20 破线 → 不触发
        bar = Bar(close=11.40, high=11.45, low=11.20, open=11.30)
        assert s.check(make_pos(), bar, make_ctx(bpday=1)) == []
        # 收盘 11.30 破线 → 触发, 按收盘价
        bar2 = Bar(close=11.30, high=11.45, low=11.20, open=11.30)
        res = s.check(make_pos(), bar2, make_ctx(bpday=1))
        assert len(res) == 1
        assert res[0].execution_price == pytest.approx(11.30)

    def test_no_ladder_rest(self):
        """simple 无阶梯值班休息: ladder_fired_today 当天照样触发。"""
        s = TrailingStrategy(activation=0.05, drawdown=0.01, confirm="simple")
        bar = Bar(close=11.30, high=11.40, low=11.20, open=11.35)
        res = s.check(make_pos(), bar,
                      make_ctx(bpday=48, ladder_fired_today=True))
        assert len(res) == 1


class TestRealMode:
    def test_peak_making_bar_skipped(self):
        """本 bar 自己创峰值 (peak_hi == bar.high) → 不判定, 等下一根。"""
        s = TrailingStrategy(activation=0.05, drawdown=0.01, confirm="real")
        # 本 bar high=11.5 正是峰值, low=11.20 已破线, 仍不触发
        bar = Bar(close=11.30, high=11.5, low=11.20, open=11.40)
        assert s.check(make_pos(), bar, make_ctx()) == []

    def test_prior_peak_low_touch_executes_at_line(self):
        """峰值来自更早 bar: Low 触线 → 按线价成交。"""
        s = TrailingStrategy(activation=0.05, drawdown=0.01, confirm="real")
        bar = Bar(close=11.32, high=11.45, low=11.30, open=11.42)
        res = s.check(make_pos(), bar, make_ctx())  # peak 11.5 > bar.high
        assert len(res) == 1
        assert res[0].execution_price == pytest.approx(LINE)
        assert res[0].reason == 8

    def test_gap_open_executes_at_open(self):
        """跳空低开跌穿线 → 按开盘价成交, 不按线价。"""
        s = TrailingStrategy(activation=0.05, drawdown=0.01, confirm="real")
        bar = Bar(close=11.20, high=11.30, low=11.10, open=11.25)  # open < 11.385
        res = s.check(make_pos(), bar, make_ctx())
        assert len(res) == 1
        assert res[0].execution_price == pytest.approx(11.25)

    def test_no_touch_no_trigger(self):
        """low 在线上方 → 不触发。"""
        s = TrailingStrategy(activation=0.05, drawdown=0.01, confirm="real")
        bar = Bar(close=11.50, high=11.48, low=11.40, open=11.42)
        assert s.check(make_pos(), bar, make_ctx()) == []

    def test_not_activated(self):
        """峰值涨幅未过激活线 → 不触发。"""
        s = TrailingStrategy(activation=0.05, drawdown=0.01, confirm="real")
        bar = Bar(close=10.20, high=10.30, low=10.10, open=10.25)
        assert s.check(make_pos(), bar,
                       make_ctx(peak_hi=10.30, peak_hi_profit=0.03)) == []

    def test_reason_loss_when_exec_below_entry(self):
        """成交价低于成本 → reason=4 (移动止损)。"""
        s = TrailingStrategy(activation=0.05, drawdown=0.01, confirm="real")
        pos = make_pos(entry_px=11.40)   # 成本高, 线价 11.385 在其下
        bar = Bar(close=11.32, high=11.45, low=11.30, open=11.42)
        ctx = make_ctx(entry_px=11.40)
        res = s.check(pos, bar, ctx)
        assert len(res) == 1
        assert res[0].reason == 4

    def test_no_ladder_rest(self):
        """real 无阶梯值班休息。"""
        s = TrailingStrategy(activation=0.05, drawdown=0.01, confirm="real")
        bar = Bar(close=11.32, high=11.45, low=11.30, open=11.42)
        res = s.check(make_pos(), bar, make_ctx(ladder_fired_today=True))
        assert len(res) == 1


class _FakeDispatcher:
    def __init__(self, strategies):
        self.strategies = strategies


def _pf_kwargs(**kw):
    base = dict(
        ci=0, i=5, ep=10.0, hi=11.45, lo=11.30, hi_pp=0.145, lo_pp=0.13,
        peak_hi=11.5, peak_hi_profit=0.15, hold_days=3, entry_idx=0,
        bpday=48, ladder_done=0,
        ladder_profits=np.array([0.06, 0.15], dtype=np.float64), n_ladder=2,
    )
    base.update(kw)
    return base


class TestPrefilterSimpleReal:
    """审计回归: simple/real 必须每 bar 都可能放行 (i=5 非末根 bar)。"""

    def _make_pf(self, confirm):
        t = TrailingStrategy(activation=0.05, drawdown=0.01, confirm=confirm)
        return TriggerPreFilter(_FakeDispatcher({"trailing": t}), [])

    def test_real_passes_on_non_last_bar(self):
        pf = self._make_pf("real")
        # lo=11.30 <= 线 11.385 → 必须放行 (i=5, bpday=48, 非末根)
        assert pf.could_trigger(**_pf_kwargs()) is True

    def test_simple_passes_on_non_last_bar(self):
        pf = self._make_pf("simple")
        assert pf.could_trigger(**_pf_kwargs()) is True

    def test_real_gap_bar_covered(self):
        """real 跳空分支 open<线 蕴含 lo<=线: 预筛条件 lo<=线 足够覆盖。"""
        pf = self._make_pf("real")
        # 构造跳空 bar: open=11.25 < 线, low=11.10 (low<=open 恒成立)
        assert pf.could_trigger(**_pf_kwargs(hi=11.30, lo=11.10)) is True

    def test_no_touch_not_passed(self):
        """lo 在线上方 → 不放行。"""
        pf = self._make_pf("real")
        assert pf.could_trigger(**_pf_kwargs(lo=11.40)) is False
