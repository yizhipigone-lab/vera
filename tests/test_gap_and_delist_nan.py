"""审计 F6+F7 (2026-07-18): 跳空保护端到端 + 退市遇 NaN 按成本平。

F6: run_cached → shell → loop 全链路, gap-down 时硬止损必须按 open 成交
    (而非 stop_price)。此前 capabilities 测试只断言"不崩", 断链探测不到。
F7: 退市强平遇价格 NaN/<=0 时按成本价 ep_d 成交 (reason=11) 的资金语义 pin。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.engine import BacktestEngine, _simulate_core_v3


def _engine():
    return BacktestEngine({'initial_capital': 100000.0, 'slippage': 0.0,
                           'commission': 0.0, 'stamp_tax': 0.0})


def test_gap_down_executes_at_open_not_stop_price():
    """F6: open 低于止损线 → 成交价 == open (跳空保护), 不是 stop_price。"""
    dates = pd.date_range('2024-01-02', periods=4, freq='B')
    close = pd.DataFrame({'600001': [10.0, 10.0, 8.0, 8.0]}, index=dates)
    entries = pd.DataFrame({'600001': [True, False, False, False]}, index=dates)
    # T+1 (bar2) 跳空低开 8.0, 止损线 = 10*(1-0.12) = 8.8 → 应按 8.0 成交
    open_np = np.array([[10.0], [10.0], [8.0], [8.0]])
    high_np = close.values * 1.01
    low_np = close.values * 0.99
    low_np[2, 0] = 7.5  # 确保 lo_pp 触发
    stop = {'cost_stop': {'enabled': True, 'threshold': -0.12},
            'trailing_stop': {'enabled': False}, 'ladder_tp': {'enabled': False},
            'time_stop': {'enabled': False}}
    eng = _engine()
    r = eng.run_cached(close, entries, high_np, low_np, stop, None,
                       np.array([0.06]), np.array([0.5]), 1,
                       filter_limit_up=False, open_np=open_np, return_raw=True)
    raw = r.raw_trades
    assert len(raw) == 1, f"应 1 笔交易, 实际 {len(raw)}"
    assert raw[0, 8] == 3.0, "退出原因应为硬止损(3)"
    assert raw[0, 4] == 8.0, f"跳空低开应按 open=8.0 成交, 实际 {raw[0, 4]}"


def test_no_gap_executes_at_stop_price():
    """对照: open 高于止损线 → 成交价 == stop_price (不受 open 影响)。"""
    dates = pd.date_range('2024-01-02', periods=4, freq='B')
    close = pd.DataFrame({'600001': [10.0, 10.0, 8.7, 8.7]}, index=dates)
    entries = pd.DataFrame({'600001': [True, False, False, False]}, index=dates)
    open_np = np.array([[10.0], [10.0], [9.5], [8.7]])  # bar2 open 9.5 > 8.8
    high_np = close.values * 1.01
    low_np = close.values * 0.99
    low_np[2, 0] = 8.5  # lo_pp = -0.15 <= -0.12 触发
    stop = {'cost_stop': {'enabled': True, 'threshold': -0.12},
            'trailing_stop': {'enabled': False}, 'ladder_tp': {'enabled': False},
            'time_stop': {'enabled': False}}
    eng = _engine()
    r = eng.run_cached(close, entries, high_np, low_np, stop, None,
                       np.array([0.06]), np.array([0.5]), 1,
                       filter_limit_up=False, open_np=open_np, return_raw=True)
    raw = r.raw_trades
    assert len(raw) == 1
    assert raw[0, 8] == 3.0
    assert abs(raw[0, 4] - 8.8) < 1e-9, f"应按 stop_price=8.8 成交, 实际 {raw[0, 4]}"


def test_delist_with_nan_price_exits_at_entry_price():
    """F7: 退市强平遇 NaN 价 → 按成本价 ep_d 成交 (reason=11), 不产生幻觉盈亏。"""
    # 2 股票: 600001 在 bar2 后退市(不可交易+价格 NaN), 600002 正常
    price = np.array([[10.0, 20.0], [10.0, 20.0], [np.nan, 20.0], [np.nan, 20.0]])
    entry = np.zeros((4, 2), dtype=bool)
    entry[0, 0] = True
    tradable = np.array([[True, True], [True, True], [False, True], [False, True]])
    last_tradable_idx = np.array([1, 3])  # 600001 最后可交易 bar=1
    eq, raw = _simulate_core_v3(
        price, entry, 100000.0, 0.0, 2000.0, 20000.0, 100, 1,
        False, -0.12, False, 0.05, 0.03, False, np.array([0.06]), np.array([0.5]), 1,
        False, 20, False, 7, 0.01, False, 0.03, 1, None, None, 1, 0.0, 0.0,
        tradable, last_tradable_idx, None, None, 1.0, 1, False, False, 1.0)
    assert len(raw) == 1, f"应 1 笔退市强平, 实际 {len(raw)}"
    assert raw[0, 8] == 11.0, "退出原因应为退市(11)"
    assert raw[0, 4] == 10.0, f"NaN 价退市应按成本价 10.0 成交, 实际 {raw[0, 4]}"
    assert raw[0, 7] == 0.0, "按成本平 → 收益率应为 0"
