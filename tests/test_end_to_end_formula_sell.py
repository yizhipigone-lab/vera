"""
formula_sell 端到端集成测试

不依赖 TDX, 直接调 _simulate_core_v3 + 合成数据, 验证:
  1. formula_exit_np 传入 → reason=12 触发 → trade.exit_reason="formula_sell"
  2. formula_exit_ratio < 1 → 部分卖出
  3. formula_exit_np=None → 旧路径不变 (无 reason=12 触发)
  4. formula_exit 早于 cost_stop 触发 (优先级最高)

买入语义 (2026-07-04 起恢复基本原则: 尾盘选股 → 信号日收盘价买入):
  bar 5: entries.iloc[5,0]=True → bar 5 是信号日, 当日收盘买入 → pos_entry_idx = 5
  bar 5: 买入循环在卖出循环之后, 当天买的仓位当天不会再进卖出循环 (天然 T+1)
  bar 6: 卖出循环首次检查该仓位 → 查 formula_exit_np[6-1, 0] = matrix[5, 0]
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest

from backtest.engine import _simulate_core_v3
from backtest.formula_exit import build_formula_exit_matrix


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def synthetic_market():
    """
    30 个交易日 / 3 只股票.
    第 5 天某只股票被选股命中 → 第 6 天 T+1 开盘买入.
    价格 ~500 元/股, 资金 100 万, max_buy=6 万, 足够买 1 手 100 股.
    价格单调上涨 0.5%/日 (避免触发任何其他止损机制).
    """
    n_dates, n_stocks = 30, 3
    dates = pd.bdate_range("2024-01-02", periods=n_dates)
    columns = pd.Index(["600519.SH", "000002.SZ", "300750.SZ"])
    base = 500.0
    daily_ret = 0.005
    close = pd.DataFrame(
        np.array([base * (1 + daily_ret) ** i for i in range(n_dates)]).reshape(-1, 1) * np.ones((1, n_stocks)),
        index=dates,
        columns=columns,
    )
    high = close * 1.005
    low = close * 0.995
    open_ = close.copy()  # T+1 路径需要 open_np

    # 在第 5 天 (i=5) 给 "600519.SH" 一个入场信号
    entries = pd.DataFrame(False, index=dates, columns=columns)
    entries.iloc[5, 0] = True
    return dates, close, high, low, open_, entries, columns


def _make_args(close, entries, **kwargs):
    """构造 _simulate_core_v3 的标准参数 (尽量宽松: 不触发任何其他止损)."""
    n_dates, n_stocks = close.shape
    base = dict(
        price_np=close.values.astype(np.float64),
        entry_np=entries.values,
        initial_capital=1_000_000.0,            # 100 万
        commission=0.0003,
        min_buy_amount=1000.0,
        max_buy_amount=60_000.0,                # 6 万, 515*100=51500 足够买 1 手
        lot_size=100,
        min_lots=1,
        cost_stop_enabled=True, cost_stop_threshold=-0.30,    # 很宽松, 不触发
        trailing_enabled=True, trailing_activation=0.50,      # 很宽松
        trailing_drawdown=0.30,
        ladder_enabled=False, ladder_profits=np.array([]), ladder_ratios=np.array([]),
        n_ladder=0,
        time_enabled=False, max_hold_days=9999,               # 关闭时间止盈
        cond_time_enabled=False, cond_time_days=9999, cond_time_profit=0.99,
        first_day_enabled=False,
        first_day_target=0.99, first_day_n_bars=1,
        high_np=kwargs.pop("high_np", None),
        low_np=kwargs.pop("low_np", None),
        bpday=1,
        slippage=0.0, stamp_tax=0.0,                          # 关闭成本, 便于数值断言
        open_np=kwargs.pop("open_np", None),
    )
    base.update(kwargs)
    return base


def _set_matrix(n_dates, n_stocks, exit_bar, code_idx=0):
    """构造公式卖出矩阵: matrix[exit_bar, code_idx] = True.

    注意: 主循环在 bar i 时查 matrix[i - lag, ci], lag=1.
    所以要触发 bar i, 应设 matrix[i-1, ci]=True.
    """
    m = np.zeros((n_dates, n_stocks), dtype=bool)
    m[exit_bar, code_idx] = True
    return m


# ---------------------------------------------------------------------------
# Test 1: formula_exit_np 在入场次日触发 → reason=12
# ---------------------------------------------------------------------------
def test_formula_exit_triggers_after_t1(synthetic_market):
    """
    bar 5: 选股命中 → 当日收盘买入 (pos_entry_idx=5)
    bar 6: 卖出循环查 formula_exit_np[5, 0] = matrix[5, 0] = False → 不触发
    bar 7: 卖出循环查 formula_exit_np[6, 0] = matrix[6, 0] = True → reason=12
    """
    dates, close, high, low, open_, entries, columns = synthetic_market
    formula_exit_np = _set_matrix(len(dates), len(columns), exit_bar=6, code_idx=0)

    kwargs = _make_args(
        close, entries,
        formula_exit_np=formula_exit_np, formula_exit_ratio=1.0,
        open_np=open_.values.astype(np.float64),
        high_np=high.values.astype(np.float64),
        low_np=low.values.astype(np.float64),
    )
    equity_arr, raw_trades = _simulate_core_v3(**kwargs)

    assert len(raw_trades) >= 1, "应有 1 笔交易 (买入 + 公式卖)"
    trade = raw_trades[0]
    entry_idx = int(trade[1])
    exit_idx = int(trade[2])
    reason = int(trade[8])
    assert entry_idx == 5, f"入场 bar 应=5 (信号日收盘买入), 实际 {entry_idx}"
    assert exit_idx == 7, f"退出 bar 应=7 (查 matrix[6] 触发), 实际 {exit_idx}"
    assert reason == 12.0, f"reason 应=12 (formula_sell), 实际 {reason}"


# ---------------------------------------------------------------------------
# Test 2: 部分卖出 (formula_exit_ratio < 1)
# ---------------------------------------------------------------------------
def test_formula_exit_partial_sell_ratio(synthetic_market):
    """formula_exit_ratio=0.4 → 部分卖出."""
    dates, close, high, low, open_, entries, columns = synthetic_market
    formula_exit_np = _set_matrix(len(dates), len(columns), exit_bar=6, code_idx=0)

    kwargs = _make_args(
        close, entries,
        formula_exit_np=formula_exit_np, formula_exit_ratio=0.4,
        open_np=open_.values.astype(np.float64),
        high_np=high.values.astype(np.float64),
        low_np=low.values.astype(np.float64),
    )
    equity_arr, raw_trades = _simulate_core_v3(**kwargs)

    assert len(raw_trades) >= 1, "应有 1 笔 (部分卖出)"
    trade = raw_trades[0]
    sell_sh = int(trade[5])
    assert sell_sh > 0 and sell_sh < 60_000 // 500, (
        f"部分卖出, 0 < sell_sh < 120, 实际 {sell_sh}"
    )
    assert int(trade[8]) == 12.0, "部分卖出也是 reason=12"


# ---------------------------------------------------------------------------
# Test 3: 不传 formula_exit → 旧路径不变 (无 reason=12)
# ---------------------------------------------------------------------------
def test_call_without_formula_exit_uses_legacy_path(synthetic_market):
    """不传 formula_exit_np → 期末持仓不卖, trade 为空 (旧行为)."""
    dates, close, high, low, open_, entries, columns = synthetic_market

    kwargs = _make_args(
        close, entries,
        open_np=open_.values.astype(np.float64),
        high_np=high.values.astype(np.float64),
        low_np=low.values.astype(np.float64),
    )  # 不传 formula_exit_np
    equity_arr, raw_trades = _simulate_core_v3(**kwargs)

    # 无 sell 触发, raw_trades 为空 (期末不平仓的持仓不出现在 trades)
    assert len(raw_trades) == 0, (
        f"无公式卖, 期末持仓不出现在 trades, 实际 {len(raw_trades)} 笔"
    )


# ---------------------------------------------------------------------------
# Test 4: formula_exit 优先级高于 cost_stop (最高优先级)
# ---------------------------------------------------------------------------
def test_formula_exit_beats_cost_stop_in_priority(synthetic_market):
    """formula_exit 与 cost_stop 同时触发 → reason=12 (公式卖), 不是 3 (cost_stop)."""
    dates, close, high, low, open_, entries, columns = synthetic_market

    # 构造暴跌场景: 让 cost_stop 也满足触发
    close_local = close.copy()
    close_local.iloc[6:, :] = close.iloc[6:, :] * 0.5   # 第 6 天起腰斩 (cost_stop 必触发)
    open_local = close_local.copy()

    formula_exit_np = _set_matrix(len(dates), len(columns), exit_bar=6, code_idx=0)

    kwargs = _make_args(
        close_local, entries,
        formula_exit_np=formula_exit_np,
        formula_exit_ratio=1.0,
        cost_stop_enabled=True, cost_stop_threshold=-0.12,   # 严格, 暴跌必触发
        open_np=open_local.values.astype(np.float64),
        high_np=high.values.astype(np.float64),
        low_np=low.values.astype(np.float64),
    )
    equity_arr, raw_trades = _simulate_core_v3(**kwargs)

    assert len(raw_trades) >= 1, "应有交易"
    trade = raw_trades[0]
    assert int(trade[8]) == 12.0, (
        f"公式卖出 (12) 应优先于成本止损 (3), 实际 reason={int(trade[8])}"
    )


# ---------------------------------------------------------------------------
# Test 5: build_formula_exit_matrix → _simulate_core_v3 端到端
# ---------------------------------------------------------------------------
def test_end_to_end_with_build_matrix(synthetic_market):
    """完整链路: build_formula_exit_matrix (TDX 模拟) → _simulate_core_v3."""
    dates, close, high, low, open_, entries, columns = synthetic_market

    # 模拟 TDX: 在 bar 6 返回 (600519.SH, idx[6]) 一条卖出信号
    signals = pd.DataFrame([
        {"stock_code": "600519.SH", "select_date": dates[6]},
    ])
    matrix = build_formula_exit_matrix(signals, dates, columns)

    assert matrix.shape == (len(dates), len(columns))
    assert matrix[6, 0], "信号应写到 (6, 0)"

    kwargs = _make_args(
        close, entries,
        formula_exit_np=matrix, formula_exit_ratio=1.0,
        open_np=open_.values.astype(np.float64),
        high_np=high.values.astype(np.float64),
        low_np=low.values.astype(np.float64),
    )
    equity_arr, raw_trades = _simulate_core_v3(**kwargs)

    assert len(raw_trades) >= 1
    trade = raw_trades[0]
    assert int(trade[8]) == 12.0, "端到端触发应 reason=12"
    assert int(trade[2]) == 7, "卖出 bar 应=7 (T+1 保护后)"


# ---------------------------------------------------------------------------
# Test 6: T+1 交易制度: 买入次日才能卖 (matrix[5] 在 bar6 触发, 不在 bar5)
# ---------------------------------------------------------------------------
def test_t1_protection_blocks_same_day_sell(synthetic_market):
    """A 股 T+1: 信号日 bar5 收盘买入, 当天不再进卖出循环; matrix[5] 在 bar6 触发卖出."""
    dates, close, high, low, open_, entries, columns = synthetic_market

    # matrix[5, 0] = True → 主循环 i=6 时查 matrix[5, 0] = True → 触发
    # bar5 买入循环在卖出循环之后, 当天买的仓位当天不会被卖出循环处理 (天然 T+1)
    formula_exit_np = _set_matrix(len(dates), len(columns), exit_bar=5, code_idx=0)

    kwargs = _make_args(
        close, entries,
        formula_exit_np=formula_exit_np, formula_exit_ratio=1.0,
        open_np=open_.values.astype(np.float64),
        high_np=high.values.astype(np.float64),
        low_np=low.values.astype(np.float64),
    )
    equity_arr, raw_trades = _simulate_core_v3(**kwargs)

    assert len(raw_trades) == 1, (
        f"应 1 笔 (bar5 买入 + bar6 卖出), 实际 {len(raw_trades)} 笔"
    )
    trade = raw_trades[0]
    assert int(trade[1]) == 5, f"入场 bar 应=5 (信号日收盘买入), 实际 {int(trade[1])}"
    assert int(trade[2]) == 6, f"卖出 bar 应=6 (T+1 次日, 查 matrix[5]), 实际 {int(trade[2])}"
    assert int(trade[8]) == 12, f"reason 应=12, 实际 {int(trade[8])}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
