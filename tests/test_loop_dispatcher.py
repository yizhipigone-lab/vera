"""候选 A 阶段 2 — stage 2 ExitDispatcher 单测。

验证 3 套优先级 + trailing_first 双触发（CA1 核心场景）+ 公共尾部 + formula_sell 绝对优先级。
对照 engine.py:188-273（3 分支）+ 346-384（双触发执行）。
"""

from __future__ import annotations

import numpy as np
import pytest

from backtest.loop import (
    Context, Position, Bar, Priority, ExitDispatcher, PRE_DISPATCH_STRATEGIES,
    CostStopStrategy, LadderTpStrategy, TrailingStrategy,
    TimeStopStrategy, CondTimeStrategy, FirstDayStrategy,
    FormulaSellStrategy,
)


def make_pos(entry_px=10.0, entry_idx=0, high_hi=10.0, ladder_done=0) -> Position:
    return Position(code=3, shares=1000.0, entry_px=entry_px, entry_idx=entry_idx,
                    high_px=entry_px, high_hi=high_hi, ladder_done=ladder_done)


def make_bar(close=10.0, high=10.0, low=10.0, open_=10.0) -> Bar:
    return Bar(close=close, high=high, low=low, open=open_)


def make_ctx(**kw) -> Context:
    base = dict(
        bar_index=5, ci=0, bpday=1, hold_days=3,
        entry_px=10.0, pp=0.0, hp_profit=0.0,
        peak_hi=10.0, peak_hi_profit=0.0,
        pos_high_px=10.0, pos_high_hi=10.0,
        hi_pp=0.0, lo_pp=0.0,
        ladder_profits=np.array([0.06, 0.15], dtype=np.float64),
        ladder_ratios=np.array([0.5, 0.5], dtype=np.float64),
        n_ladder=2,
    )
    base.update(kw)
    return Context(**base)


def all_strategies(cost_thr=-0.05, trail_act=0.05, trail_dd=0.10,
                   max_hold=5, cond_days=3, cond_profit=0.08, fd_target=0.03):
    """构造 6 策略 dict。"""
    return {
        "cost_stop": CostStopStrategy(threshold=cost_thr),
        "ladder_tp": LadderTpStrategy(),
        "trailing": TrailingStrategy(activation=trail_act, drawdown=trail_dd),
        "time_stop": TimeStopStrategy(max_hold_days=max_hold),
        "cond_time": CondTimeStrategy(days=cond_days, profit=cond_profit),
        "first_day": FirstDayStrategy(target=fd_target),
    }


# ─────────────────────────────────────────────────────────────
# stop_first
# ─────────────────────────────────────────────────────────────
class TestStopFirst:
    def test_cost_stop_wins_over_ladder(self):
        # cost 与 ladder 都满足 → stop_first 下 cost 先赢, ladder 不被调用（ladder_done 不变）
        d = ExitDispatcher(all_strategies(), Priority.STOP_FIRST)
        pos = make_pos(entry_px=10.0, ladder_done=0)
        bar = make_bar(close=9.4, high=10.8, low=9.3, open_=9.5)
        ctx = make_ctx(lo_pp=-0.07, hi_pp=0.08)  # cost 触发 + ladder 第一档也满足
        res = d.evaluate(pos, bar, ctx)
        assert len(res) == 1
        assert res[0].reason == 3
        assert pos.ladder_done == 0  # ladder 没被调用, bitmask 不变

    def test_ladder_when_cost_not_trigger(self):
        d = ExitDispatcher(all_strategies(), Priority.STOP_FIRST)
        pos = make_pos(entry_px=10.0, ladder_done=0)
        bar = make_bar(close=10.8, high=10.8, low=10.5)
        ctx = make_ctx(lo_pp=0.05, hi_pp=0.08)  # cost 不触发, ladder 触发
        res = d.evaluate(pos, bar, ctx)
        assert res[0].reason == 5
        assert pos.ladder_done == 1  # ladder 调用了

    def test_tail_time_stop_when_nothing_fires(self):
        d = ExitDispatcher(all_strategies(), Priority.STOP_FIRST)
        pos = make_pos(entry_px=10.0)
        ctx = make_ctx(hold_days=5, pp=0.03)  # time_stop 触发
        res = d.evaluate(pos, make_bar(close=10.3), ctx)
        assert res[0].reason == 9


# ─────────────────────────────────────────────────────────────
# ladder_tp_first
# ─────────────────────────────────────────────────────────────
class TestLadderTpFirst:
    def test_ladder_wins_over_cost(self):
        # ladder 与 cost 都满足 → ladder_tp_first 下 ladder 先赢
        d = ExitDispatcher(all_strategies(), Priority.LADDER_TP_FIRST)
        pos = make_pos(entry_px=10.0, ladder_done=0)
        bar = make_bar(close=9.4, high=10.8, low=9.3, open_=9.5)
        ctx = make_ctx(lo_pp=-0.07, hi_pp=0.08)
        res = d.evaluate(pos, bar, ctx)
        assert res[0].reason == 5  # ladder 先
        assert pos.ladder_done == 1


# ─────────────────────────────────────────────────────────────
# trailing_first 双触发（CA1 核心）
# ─────────────────────────────────────────────────────────────
class TestTrailingFirstDualTrigger:
    def test_dual_trigger_ladder_partial_plus_trailing(self):
        # ladder 第一档部分卖 + trailing 全卖剩余 → 2 个触发
        d = ExitDispatcher(all_strategies(), Priority.TRAILING_FIRST)
        pos = make_pos(entry_px=10.0, high_hi=11.5, ladder_done=0)
        # hi_pp=0.08 → ladder 第一档(0.06)部分卖; peak_hi=11.5→profit=0.15激活
        # trail_line=11.5*0.9=10.35; low=10.3<=10.35 → trailing 触发 reason=8
        bar = make_bar(close=10.3, high=10.8, low=10.3)
        ctx = make_ctx(hi_pp=0.08, peak_hi=11.5, peak_hi_profit=0.15, lo_pp=0.03)
        res = d.evaluate(pos, bar, ctx)
        assert len(res) == 2
        assert res[0].reason == 5 and res[0].is_partial is True
        assert res[0].sell_ratio == pytest.approx(0.5)
        assert res[1].reason == 8  # trailing 全卖剩余
        assert pos.ladder_done == 1

    def test_dual_trigger_ladder_partial_plus_cost_stop(self):
        # ladder 部分卖 + cost_stop 兜底全卖剩余
        d = ExitDispatcher(all_strategies(), Priority.TRAILING_FIRST)
        pos = make_pos(entry_px=10.0, high_hi=11.5, ladder_done=0)
        # ladder 部分卖; trailing 不触发(low>trail_line); cost_stop 触发(lo_pp<=thr)
        # peak_hi=11.5→trail_line=10.35; low=9.3 → trailing 也触发? lo=9.3<=10.35 yes
        # 要让 trailing 不触发, 得 peak_hi_profit < activation. 但 ladder 需要 hi_pp=0.08.
        # 用 peak_hi=10.0 → peak_hi_profit=0 < 0.05 不激活 trailing; cost lo_pp=-0.07 触发
        bar = make_bar(close=9.4, high=10.8, low=9.3, open_=9.5)
        ctx = make_ctx(hi_pp=0.08, peak_hi=10.0, peak_hi_profit=0.0, lo_pp=-0.07)
        res = d.evaluate(pos, bar, ctx)
        assert len(res) == 2
        assert res[0].reason == 5 and res[0].is_partial is True
        assert res[1].reason == 3  # cost_stop 兜底

    def test_ladder_partial_sole_when_trailing_cost_not_fire(self):
        # ladder 部分卖, trailing/cost 都不触发 → ladder 是唯一触发
        d = ExitDispatcher(all_strategies(), Priority.TRAILING_FIRST)
        pos = make_pos(entry_px=10.0, high_hi=10.8, ladder_done=0)
        # hi_pp=0.08 ladder 部分卖; peak_hi=10.8→profit=0.08>=0.05激活
        # trail_line=10.8*0.9=9.72; low=10.0>9.72 → trailing 不触发; lo_pp=0 > -0.05 cost 不触发
        bar = make_bar(close=10.5, high=10.8, low=10.0)
        ctx = make_ctx(hi_pp=0.08, peak_hi=10.8, peak_hi_profit=0.08, lo_pp=0.0)
        res = d.evaluate(pos, bar, ctx)
        assert len(res) == 1
        assert res[0].reason == 5 and res[0].is_partial is True

    def test_ladder_full_sells_blocks_trailing(self):
        # ladder 两档全卖 → 阻塞, trailing 不检查
        d = ExitDispatcher(all_strategies(), Priority.TRAILING_FIRST)
        pos = make_pos(entry_px=10.0, high_hi=11.5, ladder_done=0)
        # hi_pp=0.20 → 两档都触发, ratio=1.0 全卖
        bar = make_bar(close=11.0, high=12.0, low=10.3)
        ctx = make_ctx(hi_pp=0.20, peak_hi=11.5, peak_hi_profit=0.15)
        res = d.evaluate(pos, bar, ctx)
        assert len(res) == 1
        assert res[0].reason == 5 and res[0].is_partial is False
        assert res[0].sell_ratio == pytest.approx(1.0)

    def test_trailing_only_when_ladder_not_fire(self):
        # ladder 不触发, trailing 触发 → 单 trailing
        d = ExitDispatcher(all_strategies(), Priority.TRAILING_FIRST)
        pos = make_pos(entry_px=10.0, high_hi=11.5, ladder_done=0)
        bar = make_bar(close=10.3, high=10.5, low=10.3)
        ctx = make_ctx(hi_pp=0.03, peak_hi=11.5, peak_hi_profit=0.15)  # hi_pp<0.06 ladder 不触发
        res = d.evaluate(pos, bar, ctx)
        assert len(res) == 1
        assert res[0].reason == 8
        assert pos.ladder_done == 0  # ladder 没触发, bitmask 不变

    def test_tail_when_nothing_fires(self):
        d = ExitDispatcher(all_strategies(), Priority.TRAILING_FIRST)
        pos = make_pos(entry_px=10.0)
        ctx = make_ctx(hold_days=5, pp=0.03)  # 都不触发, time_stop 尾部触发
        res = d.evaluate(pos, make_bar(close=10.3), ctx)
        assert res[0].reason == 9


# ─────────────────────────────────────────────────────────────
# capability gating（HA1）
# ─────────────────────────────────────────────────────────────
class TestCapabilityGating:
    def test_disabled_strategy_not_in_dict(self):
        # cost_stop 禁用 → 不进 dict → 评估时跳过
        strats = all_strategies()
        del strats["cost_stop"]
        d = ExitDispatcher(strats, Priority.STOP_FIRST)
        pos = make_pos(entry_px=10.0, ladder_done=0)
        bar = make_bar(close=9.4, high=10.8, low=9.3)
        ctx = make_ctx(lo_pp=-0.07, hi_pp=0.08)  # cost 本会触发, 但被禁用 → ladder 触发
        res = d.evaluate(pos, bar, ctx)
        assert res[0].reason == 5


# ─────────────────────────────────────────────────────────────
# FormulaSellStrategy（AbsoluteStrategy, reason=12）
# ─────────────────────────────────────────────────────────────
class TestFormulaSell:
    def test_pre_dispatch_set(self):
        assert "formula_sell" in PRE_DISPATCH_STRATEGIES

    def test_fires_when_signal_true(self):
        signal = np.zeros((10, 3), dtype=bool)
        signal[4, 0] = True  # i=5, lag=1 → signal[4] 触发
        fs = FormulaSellStrategy(signal, ratio=1.0, lag_bars=1)
        pos = make_pos()
        bar = make_bar(close=10.5)
        ctx = make_ctx(bar_index=5, ci=0)
        res = fs.check(pos, bar, ctx)
        assert len(res) == 1
        assert res[0].reason == 12
        assert res[0].execution_price == pytest.approx(10.5)
        assert res[0].is_partial is False

    def test_partial_when_ratio_below_one(self):
        signal = np.zeros((10, 3), dtype=bool)
        signal[4, 0] = True
        fs = FormulaSellStrategy(signal, ratio=0.3, lag_bars=1)
        res = fs.check(make_pos(), make_bar(close=10.5), make_ctx(bar_index=5, ci=0))
        assert res[0].reason == 12
        assert res[0].sell_ratio == pytest.approx(0.3)
        assert res[0].is_partial is True

    def test_no_fire_when_signal_false(self):
        signal = np.zeros((10, 3), dtype=bool)
        fs = FormulaSellStrategy(signal, ratio=1.0, lag_bars=1)
        assert fs.check(make_pos(), make_bar(), make_ctx(bar_index=5, ci=0)) == []

    def test_no_fire_before_lag(self):
        signal = np.zeros((10, 3), dtype=bool)
        signal[0, 0] = True
        fs = FormulaSellStrategy(signal, ratio=1.0, lag_bars=1)
        # bar_index=0 < lag_bars=1 → 不触发（防越界）
        assert fs.check(make_pos(), make_bar(), make_ctx(bar_index=0, ci=0)) == []

    def test_none_signal_no_fire(self):
        fs = FormulaSellStrategy(None)
        assert fs.check(make_pos(), make_bar(), make_ctx()) == []
