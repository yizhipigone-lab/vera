"""候选 A 阶段 2 — 性能基准（CR1）。

固化"新 BacktestLoop vs legacy 甲骨文"wall-clock 退化阈值 < 2x。
非严格计时测试（CI 环境波动大）, 阈值放宽到 3x 防误报; 真实退化用本地脚本复核。
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from backtest.engine import _simulate_core_v3, _simulate_core_v3_legacy
from tests.test_loop_parity import BASE_PARAMS


def _make_data(n_dates=500, n_stocks=100, seed=1):
    rng = np.random.default_rng(seed)
    price = 10.0 * np.cumprod(1.0 + rng.normal(0, 0.02, (n_dates, n_stocks)), axis=0)
    high = price * (1 + np.abs(rng.normal(0, 0.02, (n_dates, n_stocks))))
    low = price * (1 - np.abs(rng.normal(0, 0.02, (n_dates, n_stocks))))
    op = price * (1 + rng.normal(0, 0.01, (n_dates, n_stocks)))
    entry = np.zeros((n_dates, n_stocks), dtype=bool)
    for ci in range(n_stocks):
        entry[rng.choice(n_dates // 2, size=5, replace=False), ci] = True
    return price, high, low, op, entry


def _args(price, high, low, op, entry):
    kw = BASE_PARAMS
    return (price, entry, kw["initial_capital"], kw["commission"],
            kw["min_buy_amount"], kw["max_buy_amount"], kw["lot_size"], kw["min_lots"],
            True, kw["cost_stop_threshold"], True, kw["trailing_activation"], kw["trailing_drawdown"],
            True, kw["ladder_profits"], kw["ladder_ratios"], kw["n_ladder"],
            True, kw["max_hold_days"], False, kw["cond_time_days"], kw["cond_time_profit"],
            False, kw["first_day_target"], 1, high, low, 1, kw["slippage"], kw["stamp_tax"],
            None, None, op, None, 1.0, 1, False, False, 1.0)


@pytest.mark.parametrize("runner", [_simulate_core_v3_legacy, _simulate_core_v3])
def test_perf_baseline(runner):
    """100 股 × 500 bar, 单次 < 1.0s (2026-07-17 Phase 2 收紧: 5.0s→1.0s, 本地实测 ~20ms)。"""
    price, high, low, op, entry = _make_data()
    args = _args(price, high, low, op, entry)
    runner(*args)  # 预热
    t0 = time.time()
    for _ in range(3):
        runner(*args)
    dt = (time.time() - t0) / 3
    assert dt < 1.0, f"{runner.__name__} 单次 {dt:.3f}s 过慢 (本地实测 ~0.02s)"


def test_perf_regression_ratio():
    """新壳 vs legacy wall-clock 比 < 2.0x (2026-07-17 Phase 2 收紧: 3.0x→2.0x)。

    本地实测基线: Phase 1 后新壳 ~1.5-1.7x (稀疏持仓), 密集持仓 ~1.05x。
    无 CI, 全本地跑, 2.0x 阈值不误报; 未来上 CI 再放宽。
    """
    price, high, low, op, entry = _make_data()
    args = _args(price, high, low, op, entry)
    _simulate_core_v3_legacy(*args)
    _simulate_core_v3(*args)
    t0 = time.time()
    for _ in range(3):
        _simulate_core_v3_legacy(*args)
    t_legacy = (time.time() - t0) / 3
    t0 = time.time()
    for _ in range(3):
        _simulate_core_v3(*args)
    t_new = (time.time() - t0) / 3
    ratio = t_new / t_legacy if t_legacy > 0 else 0
    assert ratio < 2.0, (
        f"性能退化 {ratio:.2f}x 超阈值(legacy={t_legacy:.3f}s new={t_new:.3f}s); "
        f"Phase 1 后本地应 < 1.8x")
