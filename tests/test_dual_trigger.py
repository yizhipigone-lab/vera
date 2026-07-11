"""trailing_first 双触发测试: ladder 部分卖后 trailing 接力清仓

2026-07-07: 用户要求 trailing_first 模式下, ladder 部分卖后继续检查 trailing,
如果 Low 触及回撤线, 剩余仓位移动止盈清仓. 不再走 cost_stop (止盈优先).
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd
import pytest
from backtest.engine import _simulate_core_v3


def _make_dual_trigger_market():
    """
    构造 ladder + trailing 同 bar 双触发场景 (类似 002788 04-07):
      bar 5: entry 信号 → ep=100
      bar 6: high=110 (hi_pp=+10%, 触发 ladder TP1 3%), low=95 (触及 trailing 回撤线)
             close=98
    配置: ladder 3%/20%, trailing activation 3.9%/drawdown 1.7%
    peak_hi=110, trail_line = 110*0.983 = 108.13, low=95 ≤ 108.13 → trailing 触发
    """
    n_dates, n_stocks = 10, 1
    dates = pd.bdate_range("2024-01-02", periods=n_dates)
    columns = pd.Index(["002788.SZ"])
    close = pd.DataFrame(np.full((n_dates, n_stocks), 100.0), index=dates, columns=columns)
    high = pd.DataFrame(np.full((n_dates, n_stocks), 101.0), index=dates, columns=columns)
    low = pd.DataFrame(np.full((n_dates, n_stocks), 99.0), index=dates, columns=columns)
    # bar 6: ladder + trailing 双触发
    high.iloc[6, 0] = 110.0   # +10% 触发 ladder TP1 (3%)
    low.iloc[6, 0] = 95.0     # 触及 trailing 回撤线 108.13
    close.iloc[6, 0] = 98.0
    entries = pd.DataFrame(False, index=dates, columns=columns)
    entries.iloc[5, 0] = True
    return close, high, low, entries


def _make_args(close, entries, high, low, **kwargs):
    base = dict(
        price_np=close.values.astype(np.float64),
        entry_np=entries.values,
        initial_capital=1_000_000.0,
        commission=0.0003,
        min_buy_amount=1000.0, max_buy_amount=60_000.0,
        lot_size=100, min_lots=1,
        cost_stop_enabled=True, cost_stop_threshold=-0.046,  # 止损线 95.4
        trailing_enabled=True, trailing_activation=0.039, trailing_drawdown=0.017,
        ladder_enabled=True,
        ladder_profits=np.array([0.03, 0.13], dtype=np.float64),  # TP1 3%, TP2 13%
        ladder_ratios=np.array([0.20, 0.60], dtype=np.float64),   # 卖 20%, 卖 60%
        n_ladder=2,
        time_enabled=False, max_hold_days=9999,
        cond_time_enabled=False, cond_time_days=9999, cond_time_profit=0.99,
        first_day_enabled=False, first_day_target=0.99, first_day_n_bars=1,
        high_np=high.values.astype(np.float64),
        low_np=low.values.astype(np.float64),
        bpday=1, slippage=0.0, stamp_tax=0.0, open_np=None,
    )
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Test 1: trailing_first + ladder + trailing 同 bar 双触发 → 两笔交易 (ladder 部分 + trailing 全卖剩余)
# ---------------------------------------------------------------------------
def test_ladder_then_trailing_dual_trigger():
    """ladder 部分卖后, trailing 接力清仓剩余 → 两笔交易, reason=5 + reason=8, 同 bar."""
    close, high, low, entries = _make_dual_trigger_market()
    args = _make_args(close, entries, high, low, trailing_first=True)
    _, raw_trades = _simulate_core_v3(**args)
    real_trades = [t for t in raw_trades if t[8] != 0.0 or t[0] != 0.0]

    # 应有两笔: ladder 部分卖 (reason=5) + trailing 全卖剩余 (reason=8)
    reasons = [t[8] for t in real_trades]
    assert 5.0 in reasons, f"应有 ladder 部分卖 (reason=5), 实际 {reasons}"
    assert 8.0 in reasons, f"应有 trailing 接力清仓 (reason=8), 实际 {reasons}"

    # 关键: 两笔必须在同一 bar (bar 6, i=6) — 这是"同 bar 双触发"的核心
    exit_bars = [t[2] for t in real_trades]
    assert exit_bars.count(6.0) == 2, f"两笔应在同 bar (i=6) 双触发, 实际 exit_bars={exit_bars}"

    # ladder 笔: 卖 20% (lot_size 取整)
    ladder_trade = next(t for t in real_trades if t[8] == 5.0)
    assert ladder_trade[5] < 1000, f"ladder 应部分卖 (<1000 股), 实际 {ladder_trade[5]}"
    # trailing 笔: 卖剩余
    trailing_trade = next(t for t in real_trades if t[8] == 8.0)
    assert trailing_trade[5] > 0, f"trailing 应卖剩余 (>0 股), 实际 {trailing_trade[5]}"

    # 两笔 shares 之和 = 总仓位 (1000 股, max_buy 60000 / 100 元 ≈ 600 股, 但 lot 100 → 600)
    total_sold = ladder_trade[5] + trailing_trade[5]
    assert total_sold == 600, f"两笔总和应=总仓位 600 股, 实际 {total_sold}"


# ---------------------------------------------------------------------------
# Test 2: trailing_first + ladder 触发但 trailing 没触发 → 只 ladder 部分卖, 剩余持有
# ---------------------------------------------------------------------------
def test_ladder_only_no_trailing_trigger():
    """ladder 触发但 trailing 关闭 → 只 ladder 部分卖, 剩余持有, 不走 cost_stop."""
    close, high, low, entries = _make_dual_trigger_market()
    # bar 6: high=110 (ladder 触发), low=108.5 (> cost_stop 95.4, 不触发)
    low.iloc[6, 0] = 108.5
    args = _make_args(close, entries, high, low, trailing_first=True, trailing_enabled=False)
    _, raw_trades = _simulate_core_v3(**args)
    real_trades = [t for t in raw_trades if t[8] != 0.0 or t[0] != 0.0]

    reasons = [t[8] for t in real_trades]
    assert 5.0 in reasons, f"应有 ladder 部分卖 (reason=5), 实际 {reasons}"
    assert 3.0 not in reasons, f"cost_stop 不应触发 (low 108.5 > 止损线 95.4), 实际 {reasons}"


# ---------------------------------------------------------------------------
# Test 3: trailing_first + ladder 没触发 + cost_stop 触发 → cost_stop 全卖 (兜底)
# ---------------------------------------------------------------------------
def test_cost_stop_fallback_when_no_ladder_no_trailing():
    """ladder 没触发 + trailing 没激活 + cost_stop 触发 → cost_stop 全卖."""
    close, high, low, entries = _make_dual_trigger_market()
    # 改 bar 6: high=101 (没到 ladder 3%=103), low=94 (跌破 cost_stop 95.4)
    high.iloc[6, 0] = 101.0  # +1% < 3%, ladder 不触发
    low.iloc[6, 0] = 94.0    # -6% ≤ -4.6%, cost_stop 触发
    args = _make_args(close, entries, high, low, trailing_first=True)
    _, raw_trades = _simulate_core_v3(**args)
    real_trades = [t for t in raw_trades if t[8] != 0.0 or t[0] != 0.0]

    reasons = [t[8] for t in real_trades]
    assert 3.0 in reasons, f"应有 cost_stop 兜底 (reason=3), 实际 {reasons}"
    assert 5.0 not in reasons, f"ladder 不应触发, 实际 {reasons}"
    assert 8.0 not in reasons, f"trailing 不应触发, 实际 {reasons}"


# ---------------------------------------------------------------------------
# Test 4: 002788 场景验证 - ladder + trailing 双触发, 不走 cost_stop
#   ep=100, high=110 (+10%), low=95, cost_stop=-4.6% 止损线 95.4
#   low=95 < 95.4, cost_stop 也满足, 但止盈优先 → 只走 ladder+trailing, 不走 cost_stop
# ---------------------------------------------------------------------------
def test_002788_scenario_profit_priority_over_cost_stop():
    """002788 场景: ladder+trailing 都触发, cost_stop 也满足但被止盈优先跳过."""
    close, high, low, entries = _make_dual_trigger_market()
    # bar 6: high=110, low=95 → ladder(+10%) + trailing(回撤线108.13) + cost_stop(-5%< -4.6%) 都满足
    args = _make_args(close, entries, high, low, trailing_first=True)
    _, raw_trades = _simulate_core_v3(**args)
    real_trades = [t for t in raw_trades if t[8] != 0.0 or t[0] != 0.0]

    reasons = [t[8] for t in real_trades]
    assert 3.0 not in reasons, f"止盈优先: cost_stop 不应触发, 实际 {reasons}"
    assert 5.0 in reasons, f"应有 ladder, 实际 {reasons}"
    assert 8.0 in reasons, f"应有 trailing 接力, 实际 {reasons}"