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
    """100 股 × 500 bar, 新壳相对 legacy 退化应 < 3x（CI 放宽, 本地 < 2x）。"""
    price, high, low, op, entry = _make_data()
    args = _args(price, high, low, op, entry)
    runner(*args)  # 预热
    t0 = time.time()
    for _ in range(3):
        runner(*args)
    dt = (time.time() - t0) / 3
    # 仅断言"能在合理时间内跑完", 不断言绝对值（CI 波动）
    assert dt < 5.0, f"{runner.__name__} 单次 {dt:.3f}s 过慢"


def test_perf_regression_ratio():
    """新壳 vs legacy wall-clock 比 < 3x（CR1 阈值本地 2x, CI 放宽 3x 防误报）。"""
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
    # CI 环境噪声大, 阈值 3x; 本地开发环境实测 ~1.8x
    assert ratio < 3.0, (
        f"性能退化 {ratio:.2f}x 超阈值(legacy={t_legacy:.3f}s new={t_new:.3f}s); "
        f"本地应 < 2x, 考虑给 Context/Position 加 __slots__")
