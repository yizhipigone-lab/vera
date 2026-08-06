"""移动止盈日频确认模式单测 (2026-08-04)。

confirm="low"  : 只在当日末根 bar 判定, 当日累计最低价触线 → 收盘价成交
confirm="close": 只在当日末根 bar 判定, 当日收盘价本身破线 → 收盘价成交
两种日频模式共同规则: 阶梯值班日 (ladder_fired_today) trailing 休息。
背景: 1D/5M 对比实验发现 intraday 模式在分钟级被日内噪音扫出
(同参数 1D +6.7% vs 5M -10.6%), 日频模式为无未来信息的可实盘替代。
"""
from __future__ import annotations

import numpy as np
import pytest

from backtest.loop import Bar, Context, Position, TrailingStrategy


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
        is_last_bar=True, day_lo=10.0, ladder_fired_today=False,
    )
    base.update(kw)
    return Context(**base)


LINE = 11.5 * 0.99  # 11.385


class TestDailyLowConfirm:
    def test_triggers_only_at_last_bar(self):
        s = TrailingStrategy(activation=0.05, drawdown=0.01, confirm="low")
        bar = Bar(close=11.0, high=11.2, low=10.9, open=11.1)
        # 非末根 bar: day_lo 已破线也不判
        assert s.check(make_pos(), bar, make_ctx(is_last_bar=False, day_lo=11.0)) == []
        # 末根 bar: day_lo=11.0 <= 11.385 → 触发, 按收盘价 11.0 成交
        res = s.check(make_pos(), bar, make_ctx(is_last_bar=True, day_lo=11.0))
        assert len(res) == 1
        assert res[0].execution_price == pytest.approx(11.0)
        assert res[0].reason == 8  # 收盘价高于成本 → 止盈

    def test_no_trigger_when_day_low_above_line(self):
        s = TrailingStrategy(activation=0.05, drawdown=0.01, confirm="low")
        bar = Bar(close=11.5, high=11.6, low=11.4, open=11.5)
        assert s.check(make_pos(), bar, make_ctx(day_lo=11.4)) == []  # 11.4 > 11.385

    def test_ladder_duty_day_rests(self):
        s = TrailingStrategy(activation=0.05, drawdown=0.01, confirm="low")
        bar = Bar(close=11.0, high=11.2, low=10.9, open=11.1)
        assert s.check(make_pos(), bar,
                       make_ctx(day_lo=11.0, ladder_fired_today=True)) == []

    def test_reason_loss_when_close_below_entry(self):
        s = TrailingStrategy(activation=0.05, drawdown=0.01, confirm="low")
        bar = Bar(close=9.5, high=10.0, low=9.4, open=9.9)
        res = s.check(make_pos(), bar, make_ctx(day_lo=9.4))
        assert res[0].reason == 4  # 收盘价低于成本 → 止损


class TestDailyCloseConfirm:
    def test_wick_recovery_does_not_trigger(self):
        """下影线破线但收盘站回线上 → 不触发 (动能还在)。"""
        s = TrailingStrategy(activation=0.05, drawdown=0.01, confirm="close")
        bar = Bar(close=11.4, high=11.5, low=11.0, open=11.3)
        # day_lo=11.0 破线 11.385, 但收盘 11.4 在线上 → 持有
        assert s.check(make_pos(), bar, make_ctx(day_lo=11.0)) == []

    def test_close_below_line_triggers(self):
        s = TrailingStrategy(activation=0.05, drawdown=0.01, confirm="close")
        bar = Bar(close=11.3, high=11.5, low=11.2, open=11.4)
        res = s.check(make_pos(), bar, make_ctx(day_lo=11.2))
        assert len(res) == 1
        assert res[0].execution_price == pytest.approx(11.3)

    def test_ladder_duty_day_rests(self):
        s = TrailingStrategy(activation=0.05, drawdown=0.01, confirm="close")
        bar = Bar(close=11.3, high=11.5, low=11.2, open=11.4)
        assert s.check(make_pos(), bar, make_ctx(ladder_fired_today=True)) == []


class TestConfirmParam:
    def test_invalid_confirm_raises(self):
        with pytest.raises(ValueError):
            TrailingStrategy(activation=0.05, drawdown=0.01, confirm="bogus")

    def test_default_is_intraday(self):
        s = TrailingStrategy(activation=0.05, drawdown=0.01)
        assert s.confirm == "intraday"
