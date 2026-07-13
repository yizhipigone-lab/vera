"""
候选 A 阶段 1 — run_cached vs _simulate_core_v3 字节级 parity 测试 (RED)

同数据同配置下, run_cached(filter_limit_up=False, return_raw=True) 的
raw_equity / raw_trades 必须与直调 _simulate_core_v3 字节级一致。

这是最硬的等价证据: 证明深化 run_cached 没改回测口径, 只是补齐了 6 个能力 keyword。

深化前 run_cached 不认识 filter_limit_up/return_raw 等 keyword → TypeError (RED)。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest

from backtest.engine import BacktestEngine, _simulate_core_v3


def _make_engine():
    return BacktestEngine({
        "initial_capital": 1_000_000.0,
        "commission": 0.0003,
        "enable_realistic_costs": False,
        "period": "1d",
        "position_sizing": {
            "min_buy_amount": 1000.0,
            "max_buy_amount": 60_000.0,
            "lot_size": 100,
            "min_lots": 1,
        },
    })


def _stop_config_full(priority="stop_first"):
    return {
        "priority": priority,
        "cost_stop": {"enabled": True, "threshold": -0.08},
        "trailing_stop": {"enabled": True, "activation": 0.05, "drawdown": 0.03},
        "ladder_tp": {"enabled": True, "levels": [
            {"profit": 0.06, "sell_ratio": 0.30},
            {"profit": 0.15, "sell_ratio": 0.30},
        ]},
        "time_stop": {"enabled": True, "max_hold_days": 20},
        "cond_time_stop": {"enabled": False, "days": 7, "profit": 0.01},
        "first_day": {"enabled": False, "target": 0.03},
        "formula_sell": {"enabled": False, "formula_name": "", "formula_arg": "",
                         "sell_ratio": 1.0, "priority": 0},
        "capabilities": {"formula_exit": True, "gap_protection": True, "delisting": True},
    }


def _ladder_triplet(stop_config):
    levels = stop_config["ladder_tp"]["levels"]
    lv = sorted(levels, key=lambda x: x["profit"])
    lp = np.array([x["profit"] for x in lv], dtype=np.float64)
    lr = np.array([x["sell_ratio"] for x in lv], dtype=np.float64)
    return lp, lr, len(lv)


def _make_market(n_dates=40, n_stocks=3, seed=42):
    """随机游走市场, bar 5 多股票信号, 能产生多笔交易/多种 reason."""
    np.random.seed(seed)
    dates = pd.bdate_range('2024-01-02', periods=n_dates)
    cols = [f'{600000 + i}.SH' for i in range(n_stocks)]
    base = 500.0 + np.random.uniform(-5, 5, size=(n_dates, n_stocks)).cumsum(axis=0)
    close = pd.DataFrame(base, index=dates, columns=cols)
    high = close * (1 + np.abs(np.random.normal(0, 0.01, size=close.shape)))
    low = close * (1 - np.abs(np.random.normal(0, 0.01, size=close.shape)))
    open_ = close * (1 + np.random.normal(0, 0.005, size=close.shape))
    entries = pd.DataFrame(False, index=dates, columns=cols)
    entries.iloc[5, 0] = True
    entries.iloc[5, 1] = True
    return dates, close, high, low, open_, entries, cols


def _direct_call(eng, close, entries, high_np, low_np, stop_config,
                 lp, lr, nl, *,
                 open_np=None, tradable_np=None, last_tradable_idx=None,
                 formula_exit_np=None, formula_exit_ratio=None,
                 formula_exit_lag_bars=1):
    """精确复现 run_cached 深化后的参数准备 (含 capabilities gate), 直调 _simulate_core_v3."""
    stop = stop_config
    cost = stop.get("cost_stop", {})
    trail = stop.get("trailing_stop", {})
    time_s = stop.get("time_stop", {})
    cond_t = stop.get("cond_time_stop", {})
    first_day = stop.get("first_day", {})
    ladder = stop.get("ladder_tp", {})

    priority = str(stop.get("priority", "stop_first"))
    ladder_tp_first = (priority == "ladder_tp_first")
    trailing_first = (priority == "trailing_first")

    bpday = eng.bars_per_day
    mhd_scaled = int(time_s.get("max_hold_days", 20)) * bpday
    ctd_scaled = int(cond_t.get("days", 7)) * bpday
    fd_bars = bpday - 1 if bpday > 1 else 1

    # capabilities gate (与深化后 run_cached 一致)
    caps = stop.get("capabilities", {})
    if not caps.get("formula_exit", True):
        formula_exit_np = None
    if not caps.get("gap_protection", True):
        open_np = None
    if not caps.get("delisting", True):
        tradable_np = None
        last_tradable_idx = None

    if formula_exit_ratio is None:
        formula_exit_ratio = float(stop.get("formula_sell", {}).get("sell_ratio", 1.0))

    # filter_limit_up=False → entries 不变 (与 run_cached 一致)
    entry_np = entries.values

    return _simulate_core_v3(
        close.values.astype(np.float64), entry_np,
        float(eng.initial_capital), float(eng.eff_commission),
        float(eng.min_buy_amount), float(eng.max_buy_amount),
        int(eng.lot_size), int(eng.min_lots),
        cost.get("enabled", True), float(cost.get("threshold", -0.12)),
        trail.get("enabled", True), float(trail.get("activation", 0.08)),
        float(trail.get("drawdown", 0.05)),
        ladder.get("enabled", True), lp, lr, nl,
        time_s.get("enabled", True), mhd_scaled,
        cond_t.get("enabled", False), ctd_scaled, float(cond_t.get("profit", 0.01)),
        first_day_enabled=first_day.get("enabled", False),
        first_day_target=float(first_day.get("target", 0.03)),
        first_day_n_bars=fd_bars,
        high_np=high_np, low_np=low_np, bpday=bpday,
        slippage=float(eng.eff_slippage), stamp_tax=float(eng.eff_stamp_tax),
        tradable_np=tradable_np, last_tradable_idx=last_tradable_idx,
        open_np=open_np,
        formula_exit_np=formula_exit_np, formula_exit_ratio=formula_exit_ratio,
        formula_exit_lag_bars=formula_exit_lag_bars,
        ladder_tp_first=ladder_tp_first, trailing_first=trailing_first,
        max_position_pct=float(eng.max_position_pct),
    )


def _assert_parity(eng, close, entries, high, low, sc, **caps):
    lp, lr, nl = _ladder_triplet(sc)
    hn = high.values.astype(np.float64)
    ln = low.values.astype(np.float64)
    ea, rt = _direct_call(eng, close, entries, hn, ln, sc, lp, lr, nl, **caps)
    result = eng.run_cached(
        close, entries, hn, ln, sc, None, lp, lr, nl,
        skip_sm=True, filter_limit_up=False, return_raw=True, **caps,
    )
    assert np.array_equal(result["raw_equity"], ea), (
        f"raw_equity 不一致 (caps={caps})\n"
        f"  direct[-5:]={ea[-5:]}\n  cached[-5:]={result['raw_equity'][-5:]}"
    )
    assert result["raw_trades"].shape == rt.shape, (
        f"raw_trades shape 不一致 (caps={caps}): {result['raw_trades'].shape} vs {rt.shape}"
    )
    if rt.shape[0] > 0:
        assert np.array_equal(result["raw_trades"], rt), (
            f"raw_trades 不一致 (caps={caps})"
        )


# ---------------------------------------------------------------------------
# Parity: 无能力数据 (baseline)
# ---------------------------------------------------------------------------
def test_parity_no_capabilities():
    dates, close, high, low, open_, entries, cols = _make_market()
    eng = _make_engine()
    _assert_parity(eng, close, entries, high, low, _stop_config_full())


# ---------------------------------------------------------------------------
# Parity: formula_exit_np
# ---------------------------------------------------------------------------
def test_parity_with_formula_exit():
    dates, close, high, low, open_, entries, cols = _make_market()
    eng = _make_engine()
    matrix = np.zeros((len(dates), len(cols)), dtype=bool)
    matrix[8, 0] = True  # bar 9 触发
    _assert_parity(eng, close, entries, high, low, _stop_config_full(),
                   formula_exit_np=matrix, formula_exit_ratio=1.0)


# ---------------------------------------------------------------------------
# Parity: tradable_np (退市)
# ---------------------------------------------------------------------------
def test_parity_with_tradable():
    dates, close, high, low, open_, entries, cols = _make_market()
    eng = _make_engine()
    n = len(dates)
    tradable = np.ones((n, len(cols)), dtype=bool)
    tradable[15:, 0] = False  # bar 15 起股票0退市
    lti = np.full(len(cols), -1, dtype=np.int64)
    lti[0] = 14
    for c in range(1, len(cols)):
        lti[c] = n - 1
    _assert_parity(eng, close, entries, high, low, _stop_config_full(),
                   tradable_np=tradable, last_tradable_idx=lti)


# ---------------------------------------------------------------------------
# Parity: open_np (跳空保护)
# ---------------------------------------------------------------------------
def test_parity_with_open_np():
    dates, close, high, low, open_, entries, cols = _make_market()
    eng = _make_engine()
    _assert_parity(eng, close, entries, high, low, _stop_config_full(),
                   open_np=open_.values.astype(np.float64))


# ---------------------------------------------------------------------------
# Parity: 三 priority 模式
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("priority", ["stop_first", "ladder_tp_first", "trailing_first"])
def test_parity_each_priority_mode(priority):
    dates, close, high, low, open_, entries, cols = _make_market()
    eng = _make_engine()
    _assert_parity(eng, close, entries, high, low, _stop_config_full(priority=priority),
                   open_np=open_.values.astype(np.float64))


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
