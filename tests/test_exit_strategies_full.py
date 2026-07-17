"""ExitStrategy 5 个 strategy 全量单元测试 (迭代 3, 2026-07-15).

覆盖 cost_stop / trailing / time_stop / cond_time / first_day 五个 strategy.
每个 strategy 测:
- 不触发条件 → 返回 []
- 触发条件 → 返回 [TriggerResult(reason=正确, ep=正确)]
- reason 码正确 (3/4/5/6/7/8/9/10)
- 边界条件 (ep=0 / NaN / 等于阈值)
"""
import math
import sys
from pathlib import Path

from dataclasses import replace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.loop.strategies.cost_stop import CostStopStrategy
from backtest.loop.strategies.trailing import TrailingStrategy
from backtest.loop.strategies.time_stop import TimeStopStrategy
from backtest.loop.strategies.cond_time import CondTimeStrategy
from backtest.loop.strategies.first_day import FirstDayStrategy
from backtest.loop.state import Bar, Context, Position


# ──────────────── fixtures ────────────────


@pytest.fixture
def empty_ctx():
    """标准空 ctx, 默认 5m 周期 (bpday=48)."""
    return Context(
        bar_index=10, ci=0, bpday=48, hold_days=5,
        entry_px=10.0, pp=0.05, hp_profit=0.06,
        peak_hi=11.0, peak_hi_profit=0.10,
        pos_high_px=10.5, pos_high_hi=11.0,
        hi_pp=0.06, lo_pp=-0.03,
        ladder_profits=np.array([0.06, 0.15], dtype=np.float64),
        ladder_ratios=np.array([0.30, 0.30], dtype=np.float64),
        n_ladder=2,
    )


@pytest.fixture
def empty_pos():
    return Position(
        code=np.int32(0), shares=np.float64(100.0),
        entry_px=np.float64(10.0), entry_idx=np.int32(8),
        high_px=np.float64(10.5), high_hi=np.float64(11.0),
        ladder_done=np.int32(0),
    )


@pytest.fixture
def bar_close():
    return Bar(close=10.0, high=11.0, low=9.5, open=10.2)


# ═══════════════════════════════════════════════════════════════
# CostStopStrategy (reason=3)
# ═══════════════════════════════════════════════════════════════


def test_cost_stop_no_trigger_when_above_threshold(empty_pos, empty_ctx, bar_close):
    """lo_pp > threshold → 不触发."""
    s = CostStopStrategy(threshold=-0.05)
    empty_ctx_local = replace(empty_ctx, lo_pp=-0.03)  # -3% > -5%
    assert s.check(empty_pos, bar_close, empty_ctx_local) == []


def test_cost_stop_triggers_when_lo_below_threshold(empty_pos, empty_ctx, bar_close):
    """lo_pp <= threshold → 触发 reason=3."""
    s = CostStopStrategy(threshold=-0.05)
    ctx = replace(empty_ctx, lo_pp=-0.06)  # -6% <= -5%
    results = s.check(empty_pos, bar_close, ctx)
    assert len(results) == 1
    r = results[0]
    assert r.reason == 3
    assert r.strategy_name == "cost_stop"
    # ep = entry*(1+threshold) = 10*(1-0.05) = 9.5
    assert r.execution_price == pytest.approx(9.5)


def test_cost_stop_gap_protection_uses_open_when_lower(empty_pos, empty_ctx):
    """open < stop_price 时按 open 成交 (跳空保护)."""
    s = CostStopStrategy(threshold=-0.05)
    ctx = replace(empty_ctx, lo_pp=-0.06)
    # bar.open = 9.0 (低于 stop_price 9.5), 应取 open
    bar = Bar(close=10.0, high=11.0, low=9.5, open=9.0)
    results = s.check(empty_pos, bar, ctx)
    assert results[0].execution_price == pytest.approx(9.0)


def test_cost_stop_no_gap_protection_when_open_higher(empty_pos, empty_ctx):
    """open >= stop_price 时仍按 stop_price."""
    s = CostStopStrategy(threshold=-0.05)
    ctx = replace(empty_ctx, lo_pp=-0.06)
    bar = Bar(close=10.0, high=11.0, low=9.5, open=10.5)  # open > stop_price
    results = s.check(empty_pos, bar, ctx)
    assert results[0].execution_price == pytest.approx(9.5)


def test_cost_stop_ignores_nan_open(empty_pos, empty_ctx):
    """open 为 NaN 时不触发跳空保护."""
    s = CostStopStrategy(threshold=-0.05)
    ctx = replace(empty_ctx, lo_pp=-0.06)
    bar = Bar(close=10.0, high=11.0, low=9.5, open=float("nan"))
    results = s.check(empty_pos, bar, ctx)
    assert results[0].execution_price == pytest.approx(9.5)


def test_cost_stop_boundary_equal_threshold_triggers(empty_pos, empty_ctx, bar_close):
    """lo_pp == threshold → 触发 (<=, 不容差 — 业务铁律 F-H6)."""
    s = CostStopStrategy(threshold=-0.05)
    ctx = replace(empty_ctx, lo_pp=-0.05)
    results = s.check(empty_pos, bar_close, ctx)
    assert len(results) == 1


# ═══════════════════════════════════════════════════════════════
# TrailingStrategy (reason=4 loss / reason=8 profit)
# ═══════════════════════════════════════════════════════════════


def test_trailing_no_trigger_below_activation(empty_pos, empty_ctx, bar_close):
    """peak_hi_profit < activation → 不触发."""
    s = TrailingStrategy(activation=0.05, drawdown=0.03)
    ctx = replace(empty_ctx, peak_hi_profit=0.04)  # 4% < 5%
    assert s.check(empty_pos, bar_close, ctx) == []


def test_trailing_triggers_after_activation_low_hits_line(empty_pos, empty_ctx, bar_close):
    """peak_hi_profit >= activation 且 bar.low <= trail_line → 触发."""
    s = TrailingStrategy(activation=0.05, drawdown=0.03)
    ctx = replace(empty_ctx,
        peak_hi_profit=0.10,  # 10% >= 5%
        peak_hi=11.0,          # trail_line = 11.0*(1-0.03) = 10.67
    )
    bar = Bar(close=10.5, high=11.0, low=10.5, open=10.7)  # low=10.5 <= 10.67
    results = s.check(empty_pos, bar, ctx)
    assert len(results) == 1
    r = results[0]
    assert r.execution_price == pytest.approx(10.67)
    # profit reason (8) because trail_line > entry
    assert r.reason == 8


def test_trailing_loss_reason_when_trail_below_entry(empty_pos, empty_ctx):
    """trail_line < entry → reason=4 (loss)."""
    s = TrailingStrategy(activation=0.05, drawdown=0.10)  # 10% drawdown
    pos = Position(
        code=np.int32(0), shares=np.float64(100.0),
        entry_px=np.float64(20.0), entry_idx=np.int32(8),  # entry 高
        high_px=np.float64(21.0), high_hi=np.float64(22.0),
        ladder_done=np.int32(0),
    )
    ctx = replace(empty_ctx,
        peak_hi_profit=0.10,
        peak_hi=22.0,  # trail_line = 22*(1-0.10) = 19.8 < entry 20
    )
    bar = Bar(close=19.0, high=22.0, low=19.0, open=21.0)
    results = s.check(pos, bar, ctx)
    assert results[0].reason == 4


def test_trailing_zero_entry_protection(empty_pos, empty_ctx):
    """ep==0 时防御除零 — 不抛."""
    s = TrailingStrategy(activation=0.05, drawdown=0.03)
    pos = Position(
        code=np.int32(0), shares=np.float64(100.0),
        entry_px=np.float64(0.0), entry_idx=np.int32(8),  # entry=0 防御点
        high_px=np.float64(11.0), high_hi=np.float64(11.0),
        ladder_done=np.int32(0),
    )
    ctx = replace(empty_ctx,
        peak_hi_profit=0.10,
        peak_hi=11.0,
    )
    bar = Bar(close=10.0, high=11.0, low=10.0, open=10.5)
    # 不应抛 ZeroDivisionError, 且 reason=4 (loss, 防 ep=0 误判 profit)
    results = s.check(pos, bar, ctx)
    assert len(results) == 1
    assert results[0].reason == 4


def test_trailing_no_low_hits_trail_no_trigger(empty_pos, empty_ctx):
    """bar.low > trail_line → 不触发."""
    s = TrailingStrategy(activation=0.05, drawdown=0.03)
    ctx = replace(empty_ctx,
        peak_hi_profit=0.10,
        peak_hi=11.0,  # trail_line = 10.67
    )
    bar = Bar(close=10.8, high=11.0, low=10.7, open=10.8)  # low 10.7 > 10.67
    assert s.check(empty_pos, bar, ctx) == []


# ═══════════════════════════════════════════════════════════════
# TimeStopStrategy (reason=6 loss / reason=9 profit)
# ═══════════════════════════════════════════════════════════════


def test_time_stop_no_trigger_below_max_hold(empty_pos, empty_ctx, bar_close):
    """hold_days < max_hold_days → 不触发."""
    s = TimeStopStrategy(max_hold_days=20)
    ctx = replace(empty_ctx, hold_days=15, pp=0.05)
    assert s.check(empty_pos, bar_close, ctx) == []


def test_time_stop_profit_reason_when_pp_positive(empty_pos, empty_ctx, bar_close):
    """hold_days >= max + pp > 0 → reason=9 (时间止盈)."""
    s = TimeStopStrategy(max_hold_days=20)
    ctx = replace(empty_ctx, hold_days=20, pp=0.08)
    results = s.check(empty_pos, bar_close, ctx)
    assert len(results) == 1
    assert results[0].reason == 9
    # ep = bar.close
    assert results[0].execution_price == bar_close.close


def test_time_stop_loss_reason_when_pp_negative(empty_pos, empty_ctx, bar_close):
    """hold_days >= max + pp < 0 → reason=6 (时间止损)."""
    s = TimeStopStrategy(max_hold_days=20)
    ctx = replace(empty_ctx, hold_days=25, pp=-0.05)
    results = s.check(empty_pos, bar_close, ctx)
    assert results[0].reason == 6


def test_time_stop_loss_reason_when_pp_zero(empty_pos, empty_ctx, bar_close):
    """pp=0 (平盘) → reason=6 (亏, 不是盈 — 业务铁律 F-H6 浮点尾巴不算盈利)."""
    s = TimeStopStrategy(max_hold_days=20)
    ctx = replace(empty_ctx, hold_days=20, pp=0.0)
    results = s.check(empty_pos, bar_close, ctx)
    assert results[0].reason == 6


def test_time_stop_boundary_equal_max_hold_triggers(empty_pos, empty_ctx, bar_close):
    """hold_days == max → 触发 (<=, 不容差)."""
    s = TimeStopStrategy(max_hold_days=20)
    ctx = replace(empty_ctx, hold_days=20, pp=0.0)
    assert len(s.check(empty_pos, bar_close, ctx)) == 1


# ═══════════════════════════════════════════════════════════════
# CondTimeStrategy (reason=7)
# ═══════════════════════════════════════════════════════════════


def test_cond_time_no_trigger_below_days(empty_pos, empty_ctx, bar_close):
    """hold_days < days → 不触发."""
    s = CondTimeStrategy(days=7, profit=0.10)
    ctx = replace(empty_ctx, hold_days=5, hi_pp=0.15)
    assert s.check(empty_pos, bar_close, ctx) == []


def test_cond_time_no_trigger_below_profit(empty_pos, empty_ctx, bar_close):
    """hi_pp < profit → 不触发."""
    s = CondTimeStrategy(days=7, profit=0.10)
    ctx = replace(empty_ctx, hold_days=10, hi_pp=0.05)
    assert s.check(empty_pos, bar_close, ctx) == []


def test_cond_time_triggers_when_both_met(empty_pos, empty_ctx, bar_close):
    """hold_days >= days 且 hi_pp >= profit → 触发 reason=7."""
    s = CondTimeStrategy(days=7, profit=0.10)
    ctx = replace(empty_ctx, hold_days=10, hi_pp=0.15)
    results = s.check(empty_pos, bar_close, ctx)
    assert len(results) == 1
    assert results[0].reason == 7
    assert results[0].execution_price == bar_close.close


def test_cond_time_boundary_both_equal(empty_pos, empty_ctx, bar_close):
    """hold_days == days 且 hi_pp == profit → 触发."""
    s = CondTimeStrategy(days=7, profit=0.10)
    ctx = replace(empty_ctx, hold_days=7, hi_pp=0.10)
    assert len(s.check(empty_pos, bar_close, ctx)) == 1


# ═══════════════════════════════════════════════════════════════
# FirstDayStrategy (reason=10)
# ═══════════════════════════════════════════════════════════════


def test_first_day_no_trigger_on_entry_day(empty_pos, empty_ctx, bar_close):
    """current_day == entry_day → 不触发."""
    # entry_idx=8, bpday=48, current bar=10 (同一天)
    s = FirstDayStrategy(target=0.05)
    ctx = replace(empty_ctx,
        bar_index=10, bpday=48,
    )
    pos = Position(
        code=np.int32(0), shares=np.float64(100.0),
        entry_px=np.float64(10.0), entry_idx=np.int32(10),  # 同 bar_index
        high_px=np.float64(10.5), high_hi=np.float64(11.0),
        ladder_done=np.int32(0),
    )
    assert s.check(pos, bar_close, ctx) == []


def test_first_day_no_trigger_on_day_after_next(empty_pos, empty_ctx, bar_close):
    """current_day > entry_day + 1 → 不触发."""
    s = FirstDayStrategy(target=0.05)
    ctx = replace(empty_ctx,
        bar_index=200, bpday=48,  # day 200/48 = 4, entry day 0/48 = 0, +1 = 1
    )
    pos = Position(
        code=np.int32(0), shares=np.float64(100.0),
        entry_px=np.float64(10.0), entry_idx=np.int32(0),
        high_px=np.float64(10.5), high_hi=np.float64(11.0),
        ladder_done=np.int32(0),
    )
    assert s.check(pos, bar_close, ctx) == []


def test_first_day_no_trigger_on_middle_bar(empty_pos, empty_ctx, bar_close):
    """T+1 日内但不是最后一根 bar → 不触发."""
    s = FirstDayStrategy(target=0.05)
    # entry_idx=0 (day 0), T+1 = day 1 = bar 48~95, 最后一根 = 95
    # bar_index=50 是 day 1 的中间 bar, 不应触发
    ctx = replace(empty_ctx,
        bar_index=50, bpday=48,
    )
    pos = Position(
        code=np.int32(0), shares=np.float64(100.0),
        entry_px=np.float64(10.0), entry_idx=np.int32(0),
        high_px=np.float64(10.5), high_hi=np.float64(11.0),
        ladder_done=np.int32(0),
    )
    assert s.check(pos, bar_close, ctx) == []


def test_first_day_triggers_when_target_not_met(empty_pos, empty_ctx, bar_close):
    """T+1 收盘 + 日内最高涨幅 < target → 触发 reason=10."""
    s = FirstDayStrategy(target=0.05)
    # entry day 0, T+1 day 1 = bar 48~95, 最后一根 = bar 95 (95 % 48 = 47 = bpday-1)
    ctx = replace(empty_ctx,
        bar_index=95, bpday=48,
    )
    pos = Position(
        code=np.int32(0), shares=np.float64(100.0),
        entry_px=np.float64(10.0), entry_idx=np.int32(0),
        # high_hi=10.3 < 10.0*1.05 = 10.5, 不达标
        high_px=np.float64(10.2), high_hi=np.float64(10.3),
        ladder_done=np.int32(0),
    )
    bar = Bar(close=10.1, high=10.4, low=10.0, open=10.2)
    results = s.check(pos, bar, ctx)
    assert len(results) == 1
    assert results[0].reason == 10
    assert results[0].execution_price == bar.close


def test_first_day_no_trigger_when_target_met(empty_pos, empty_ctx, bar_close):
    """T+1 收盘 + 日内最高涨幅 >= target → 不触发."""
    s = FirstDayStrategy(target=0.05)
    ctx = replace(empty_ctx,
        bar_index=95, bpday=48,
    )
    pos = Position(
        code=np.int32(0), shares=np.float64(100.0),
        entry_px=np.float64(10.0), entry_idx=np.int32(0),
        # high_hi=10.6 > 10.0*1.05 = 10.5, 达标
        high_px=np.float64(10.5), high_hi=np.float64(10.6),
        ladder_done=np.int32(0),
    )
    bar = Bar(close=10.4, high=10.6, low=10.2, open=10.3)
    assert s.check(pos, bar, ctx) == []


def test_first_day_zero_ep_or_high_protection(empty_pos, empty_ctx, bar_close):
    """ep=0 或 day_high=0 时防御性不触发."""
    s = FirstDayStrategy(target=0.05)
    ctx = replace(empty_ctx,
        bar_index=95, bpday=48,
    )
    pos = Position(
        code=np.int32(0), shares=np.float64(100.0),
        entry_px=np.float64(0.0), entry_idx=np.int32(0),  # entry=0
        high_px=np.float64(0.0), high_hi=np.float64(0.0),
        ladder_done=np.int32(0),
    )
    bar = Bar(close=10.0, high=11.0, low=9.5, open=10.2)
    assert s.check(pos, bar, ctx) == []


# ═══════════════════════════════════════════════════════════════
# TriggerResult 协议校验
# ═══════════════════════════════════════════════════════════════


def test_trigger_result_frozen():
    """TriggerResult 必须 frozen — 防运行时状态漂移."""
    from backtest.loop.strategies.base import TriggerResult
    r = TriggerResult(reason=3, strategy_name="cost_stop", execution_price=9.5)
    with pytest.raises(Exception):
        r.reason = 5


def test_trigger_result_default_sell_ratio_full():
    """TriggerResult 默认 sell_ratio=1.0 (全卖)."""
    from backtest.loop.strategies.base import TriggerResult
    r = TriggerResult(reason=3, strategy_name="cost_stop", execution_price=9.5)
    assert r.sell_ratio == 1.0
    assert r.is_partial is False
    assert r.actual_return is None


def test_trigger_result_partial_sell():
    """ladder 部分卖时 sell_ratio<1.0, is_partial=True."""
    from backtest.loop.strategies.base import TriggerResult
    r = TriggerResult(
        reason=5, strategy_name="ladder_tp",
        execution_price=10.6, sell_ratio=0.30, is_partial=True, actual_return=0.06,
    )
    assert r.sell_ratio == 0.30
    assert r.is_partial is True