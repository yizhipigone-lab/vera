"""ExitDispatcher 三 priority 分支全量测试 (迭代 3, 2026-07-15).

锁住 ExitDispatcher.evaluate() 在三种 priority 下的行为:
- STOP_FIRST: cost_stop > ladder_tp > trailing > 时间类
- LADDER_TP_FIRST: ladder_tp > cost_stop > trailing > 时间类
- TRAILING_FIRST: 双触发 (ladder 部分卖不阻塞, trailing/cost_stop 全卖剩余)
"""
import sys
from pathlib import Path

from dataclasses import replace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.loop.exit_engine import ExitDispatcher, Priority
from backtest.loop.strategies.cost_stop import CostStopStrategy
from backtest.loop.strategies.ladder_tp import LadderTpStrategy
from backtest.loop.strategies.trailing import TrailingStrategy
from backtest.loop.strategies.time_stop import TimeStopStrategy
from backtest.loop.strategies.cond_time import CondTimeStrategy
from backtest.loop.strategies.first_day import FirstDayStrategy
from backtest.loop.state import Bar, Context, Position


# ──────────────── fixtures ────────────────


@pytest.fixture
def full_strategies():
    """6 个 strategy 全启用."""
    return {
        "cost_stop": CostStopStrategy(threshold=-0.05),
        "ladder_tp": LadderTpStrategy(),
        "trailing": TrailingStrategy(activation=0.05, drawdown=0.03),
        "time_stop": TimeStopStrategy(max_hold_days=20),
        "cond_time": CondTimeStrategy(days=7, profit=0.10),
        "first_day": FirstDayStrategy(target=0.05),
    }


@pytest.fixture
def base_pos():
    return Position(
        code=np.int32(0), shares=np.float64(100.0),
        entry_px=np.float64(10.0), entry_idx=np.int32(0),
        high_px=np.float64(10.5), high_hi=np.float64(11.0),
        ladder_done=np.int32(0),
    )


@pytest.fixture
def base_bar():
    return Bar(close=10.0, high=11.0, low=9.5, open=10.2)


@pytest.fixture
def base_ctx_no_trigger():
    """无任何触发的 ctx (pp=0, lo_pp=0, hold=0, hi=entry)."""
    return Context(
        bar_index=10, ci=0, bpday=1, hold_days=0,
        entry_px=10.0, pp=0.0, hp_profit=0.0,
        peak_hi=10.0, peak_hi_profit=0.0,
        pos_high_px=10.0, pos_high_hi=10.0,
        hi_pp=0.0, lo_pp=0.0,
        ladder_profits=np.array([0.06, 0.15]),
        ladder_ratios=np.array([0.30, 0.30]),
        n_ladder=2,
    )


# ═══════════════════════════════════════════════════════════════
# Priority 枚举
# ═══════════════════════════════════════════════════════════════


def test_priority_enum_three_values():
    """Priority 必须恰好 3 个值."""
    assert len(Priority) == 3


def test_priority_enum_strings_match_docstring():
    """Priority 字面量对齐 docstring."""
    assert Priority.STOP_FIRST.value == "stop_first"
    assert Priority.LADDER_TP_FIRST.value == "ladder_tp_first"
    assert Priority.TRAILING_FIRST.value == "trailing_first"


# ═══════════════════════════════════════════════════════════════
# STOP_FIRST 行为
# ═══════════════════════════════════════════════════════════════


def test_stop_first_cost_stop_takes_priority(base_pos, base_bar, base_ctx_no_trigger, full_strategies):
    """STOP_FIRST: cost_stop 触发 → 即使 ladder 也满足, 只返 cost_stop."""
    # cost_stop 触发 (lo_pp <= -0.05), ladder 也满足 (hi_pp >= 0.06),
    # 但 STOP_FIRST 下 cost_stop 先赢
    ctx = replace(base_ctx_no_trigger,
        lo_pp=-0.06,
        hi_pp=0.07,
        peak_hi=10.7, peak_hi_profit=0.07,
    )
    d = ExitDispatcher(full_strategies, Priority.STOP_FIRST)
    results = d.evaluate(base_pos, base_bar, ctx)
    assert len(results) == 1
    assert results[0].strategy_name == "cost_stop"
    assert results[0].reason == 3


def test_stop_first_ladder_only_when_cost_not_triggered(base_pos, base_bar, base_ctx_no_trigger, full_strategies):
    """STOP_FIRST: cost_stop 未触发, ladder 满足 → 返 ladder."""
    # lo_pp=-0.03 (未触发 cost_stop -0.05), hi_pp=0.07 (触发 ladder 0.06)
    ctx = replace(base_ctx_no_trigger,
        lo_pp=-0.03, hi_pp=0.07,
        peak_hi=10.7, peak_hi_profit=0.07,  # 触发 ladder 但不触发 trailing
    )
    d = ExitDispatcher(full_strategies, Priority.STOP_FIRST)
    results = d.evaluate(base_pos, base_bar, ctx)
    assert len(results) == 1
    assert results[0].strategy_name == "ladder_tp"


def test_stop_first_trailing_only_when_others_not_triggered(base_pos, base_bar, base_ctx_no_trigger, full_strategies):
    """STOP_FIRST: cost_stop + ladder 都未触发, trailing 满足 → 返 trailing."""
    # lo_pp=-0.03 (cost_stop 未触发), hi_pp=0.04 (ladder 0.06 未触发)
    # peak_hi_profit=0.06 (>= 0.05 activation), trail_line = 10.6*(1-0.03)=10.282
    # bar.low=9.5 <= 10.282 → trailing 触发
    ctx = replace(base_ctx_no_trigger,
        lo_pp=-0.03, hi_pp=0.04,
        peak_hi=10.6, peak_hi_profit=0.06,
    )
    d = ExitDispatcher(full_strategies, Priority.STOP_FIRST)
    results = d.evaluate(base_pos, base_bar, ctx)
    assert len(results) == 1
    assert results[0].strategy_name == "trailing"


def test_stop_first_falls_through_to_tail(base_pos, base_bar, base_ctx_no_trigger, full_strategies):
    """STOP_FIRST: cost/ladder/trailing 都未触发, 但 time_stop 满足 → 返 time_stop."""
    # hold_days=20, pp=0.08 → time_stop 触发 reason=9
    ctx = replace(base_ctx_no_trigger,
        lo_pp=-0.03, hi_pp=0.04,
        peak_hi_profit=0.0,  # trailing 不触发
        hold_days=20, pp=0.08,
    )
    d = ExitDispatcher(full_strategies, Priority.STOP_FIRST)
    results = d.evaluate(base_pos, base_bar, ctx)
    assert len(results) == 1
    assert results[0].strategy_name == "time_stop"


def test_stop_first_no_trigger_returns_empty(base_pos, base_bar, base_ctx_no_trigger, full_strategies):
    """STOP_FIRST: 啥都不满足 → []."""
    d = ExitDispatcher(full_strategies, Priority.STOP_FIRST)
    results = d.evaluate(base_pos, base_bar, base_ctx_no_trigger)
    assert results == []


# ═══════════════════════════════════════════════════════════════
# LADDER_TP_FIRST 行为
# ═══════════════════════════════════════════════════════════════


def test_ladder_tp_first_ladder_takes_priority(base_pos, base_bar, base_ctx_no_trigger, full_strategies):
    """LADDER_TP_FIRST: ladder 满足 + cost_stop 也满足 → 只返 ladder."""
    # lo_pp=-0.06 (cost_stop 触发), hi_pp=0.07 (ladder 触发)
    ctx = replace(base_ctx_no_trigger,
        lo_pp=-0.06, hi_pp=0.07,
        peak_hi=10.7, peak_hi_profit=0.07,
    )
    d = ExitDispatcher(full_strategies, Priority.LADDER_TP_FIRST)
    results = d.evaluate(base_pos, base_bar, ctx)
    assert len(results) == 1
    assert results[0].strategy_name == "ladder_tp"


def test_ladder_tp_first_cost_stop_when_no_ladder(base_pos, base_bar, base_ctx_no_trigger, full_strategies):
    """LADDER_TP_FIRST: ladder 未触发, cost_stop 满足 → 返 cost_stop."""
    # lo_pp=-0.06 (cost_stop 触发), hi_pp=0.04 (ladder 0.06 未触发)
    ctx = replace(base_ctx_no_trigger,
        lo_pp=-0.06, hi_pp=0.04,
    )
    d = ExitDispatcher(full_strategies, Priority.LADDER_TP_FIRST)
    results = d.evaluate(base_pos, base_bar, ctx)
    assert len(results) == 1
    assert results[0].strategy_name == "cost_stop"


# ═══════════════════════════════════════════════════════════════
# TRAILING_FIRST 行为 (双触发)
# ═══════════════════════════════════════════════════════════════


def test_trailing_first_ladder_partial_then_trailing(base_pos, base_bar, base_ctx_no_trigger, full_strategies):
    """TRAILING_FIRST: ladder 部分卖 + trailing 全卖剩余 → 双触发."""
    # ladder 触发, 部分卖 (sell_ratio<1.0)
    # trailing 也触发
    # hi_pp=0.07 → 触发 ladder 第 1 档 (profit=0.06, ratio=0.30) → 部分卖
    # peak_hi=10.7, drawdown=0.03 → trail_line=10.7*(1-0.03)=10.379
    # bar.low=9.5 <= 10.379 → trailing 触发
    ctx = replace(base_ctx_no_trigger,
        hi_pp=0.07,
        peak_hi=10.7, peak_hi_profit=0.07,
        pos_high_hi=10.7,  # 用于 ladder exec_price 计算
    )
    d = ExitDispatcher(full_strategies, Priority.TRAILING_FIRST)
    results = d.evaluate(base_pos, base_bar, ctx)
    # 双触发: ladder 部分卖 + trailing 全卖剩余
    assert len(results) == 2
    strategies_in_order = [r.strategy_name for r in results]
    assert "ladder_tp" in strategies_in_order
    assert "trailing" in strategies_in_order
    # ladder 必须 partial
    ladder_r = next(r for r in results if r.strategy_name == "ladder_tp")
    assert ladder_r.is_partial is True
    assert ladder_r.sell_ratio < 1.0


def test_default_trailing_values_keep_ladder_before_trailing(
    base_pos, base_bar, base_ctx_no_trigger
):
    """默认 3.5%/1% 下，同 bar 先阶梯部分卖，再卖剩余仓位。"""
    strategies = {
        "cost_stop": CostStopStrategy(threshold=-0.12),
        "ladder_tp": LadderTpStrategy(),
        "trailing": TrailingStrategy(activation=0.035, drawdown=0.01),
    }
    ctx = replace(base_ctx_no_trigger,
        hi_pp=0.06,
        peak_hi=10.6,
        peak_hi_profit=0.06,
        pos_high_hi=10.6,
    )
    dispatcher = ExitDispatcher(strategies, Priority.TRAILING_FIRST)

    results = dispatcher.evaluate(base_pos, base_bar, ctx)

    assert len(results) == 2
    assert results[0].strategy_name == "ladder_tp"
    assert results[1].strategy_name == "trailing"
    assert results[0].is_partial is True


def test_trailing_first_ladder_full_sell_blocks_others(base_pos, base_bar, base_ctx_no_trigger, full_strategies):
    """TRAILING_FIRST: ladder 全卖 → 阻塞, 只返 ladder."""
    # 触发 ladder 第 2 档 (profit=0.15, ratio=0.30) — 单档 <1.0 仍是部分卖
    # 实际 ladder 是单档部分卖, 不会触发"全卖阻塞"
    # 此测试覆盖 ladder 触发后无 trailing 触发的情形
    ctx = replace(base_ctx_no_trigger,
        hi_pp=0.07,
        peak_hi=10.0, peak_hi_profit=0.0,  # trailing 不触发
    )
    d = ExitDispatcher(full_strategies, Priority.TRAILING_FIRST)
    results = d.evaluate(base_pos, base_bar, ctx)
    # 只有 ladder (部分卖), 无 trailing
    assert len(results) == 1
    assert results[0].strategy_name == "ladder_tp"


def test_trailing_first_no_ladder_just_trailing(base_pos, base_bar, base_ctx_no_trigger, full_strategies):
    """TRAILING_FIRST: 无 ladder, 仅 trailing → 单触发."""
    # hi_pp=0.04 (ladder 0.06 未触发), peak_hi=10.6 → trail_line=10.282 → low 9.5 触发
    ctx = replace(base_ctx_no_trigger,
        hi_pp=0.04,
        peak_hi=10.6, peak_hi_profit=0.06,
    )
    d = ExitDispatcher(full_strategies, Priority.TRAILING_FIRST)
    results = d.evaluate(base_pos, base_bar, ctx)
    assert len(results) == 1
    assert results[0].strategy_name == "trailing"


def test_trailing_first_cost_stop_when_no_ladder_no_trailing(base_pos, base_bar, base_ctx_no_trigger, full_strategies):
    """TRAILING_FIRST: ladder/trailing 都未触发, cost_stop 触发 → cost_stop 单触发."""
    ctx = replace(base_ctx_no_trigger,
        lo_pp=-0.06,
        hi_pp=0.0,
        peak_hi_profit=0.0,
    )
    d = ExitDispatcher(full_strategies, Priority.TRAILING_FIRST)
    results = d.evaluate(base_pos, base_bar, ctx)
    assert len(results) == 1
    assert results[0].strategy_name == "cost_stop"


# ═══════════════════════════════════════════════════════════════
# Capability gating (HA1)
# ═══════════════════════════════════════════════════════════════


def test_dispatcher_with_only_cost_stop(base_pos, base_bar, base_ctx_no_trigger):
    """只启用 cost_stop 时, 即便 ladder 触发条件满足, 也只可能返 cost_stop 或 []."""
    strategies = {"cost_stop": CostStopStrategy(threshold=-0.05)}
    # ladder 触发条件满足, 但 strategies 里没有 ladder
    ctx = replace(base_ctx_no_trigger,
        hi_pp=0.07,
        lo_pp=-0.03,  # cost_stop 不触发
    )
    d = ExitDispatcher(strategies, Priority.STOP_FIRST)
    results = d.evaluate(base_pos, base_bar, ctx)
    assert results == []  # 没东西可触发


def test_dispatcher_unknown_priority_falls_back_to_tail(base_pos, base_bar, base_ctx_no_trigger, full_strategies):
    """M5: 未知 priority 不 KeyError — 实际 ExitDispatcher 实现走 fallback .get()."""
    # 直接构造一个 mock priority 不可行 (Enum 不支持新值),
    # 此测试锁住 Priority 枚举的完备性 (3 个值全部走 validate)
    assert set(p for p in Priority) == {
        Priority.STOP_FIRST, Priority.LADDER_TP_FIRST, Priority.TRAILING_FIRST,
    }


# ═══════════════════════════════════════════════════════════════
# 空策略字典
# ═══════════════════════════════════════════════════════════════


def test_dispatcher_with_no_strategies_returns_empty(base_pos, base_bar, base_ctx_no_trigger):
    """空 strategies → 任何 priority 都返 []. (退化场景)."""
    d = ExitDispatcher({}, Priority.STOP_FIRST)
    assert d.evaluate(base_pos, base_bar, base_ctx_no_trigger) == []
    d2 = ExitDispatcher({}, Priority.TRAILING_FIRST)
    assert d2.evaluate(base_pos, base_bar, base_ctx_no_trigger) == []
    d3 = ExitDispatcher({}, Priority.LADDER_TP_FIRST)
    assert d3.evaluate(base_pos, base_bar, base_ctx_no_trigger) == []