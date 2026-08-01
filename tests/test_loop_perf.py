"""候选 A 阶段 2 — 性能基准（CR1）。

2026-08-01 批次 3b C2: `_simulate_core_v3` 壳退役, benchmark 对象改为
`build_backtest_loop` 直调路径:
  1. test_perf_baseline — 核心循环绝对耗时阈值 (防性能退化)。
  2. test_perf_regression_ratio — 测试适配层 run_loop_direct (tests/loop_direct.py,
     原壳的等价展开) vs 裸 build_backtest_loop+loop.run 的开销比, 应 ~1.0x
     (适配层只做参数转发, 不允许引入可观 overhead)。
非严格计时测试（CI 环境波动大）, 阈值放宽防误报; 真实退化用本地脚本复核。
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from backtest.loop import build_backtest_loop
from tests.loop_direct import run_loop_direct
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
    """run_loop_direct 位置参数 (原 _simulate_core_v3 壳的 39 参顺序)。"""
    kw = BASE_PARAMS
    return (price, entry, kw["initial_capital"], kw["commission"],
            kw["min_buy_amount"], kw["max_buy_amount"], kw["lot_size"], kw["min_lots"],
            True, kw["cost_stop_threshold"], True, kw["trailing_activation"], kw["trailing_drawdown"],
            True, kw["ladder_profits"], kw["ladder_ratios"], kw["n_ladder"],
            True, kw["max_hold_days"], False, kw["cond_time_days"], kw["cond_time_profit"],
            False, kw["first_day_target"], 1, high, low, 1, kw["slippage"], kw["stamp_tax"],
            None, None, op, None, 1.0, 1, False, False, 1.0)


def _run_raw(price, high, low, op, entry):
    """裸 build_backtest_loop + loop.run (无测试适配层)。"""
    kw = BASE_PARAMS
    loop = build_backtest_loop(
        kw["initial_capital"], kw["commission"],
        kw["min_buy_amount"], kw["max_buy_amount"], kw["lot_size"], kw["min_lots"],
        True, kw["cost_stop_threshold"],
        True, kw["trailing_activation"], kw["trailing_drawdown"],
        True, kw["ladder_profits"], kw["ladder_ratios"], kw["n_ladder"],
        True, kw["max_hold_days"],
        False, kw["cond_time_days"], kw["cond_time_profit"],
        False, kw["first_day_target"],
        bpday=1, slippage=kw["slippage"], stamp_tax=kw["stamp_tax"],
        max_position_pct=1.0,
    )
    return loop.run(price, entry, high, low, op, None, None, None)


@pytest.mark.parametrize("runner", [run_loop_direct])
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
    """适配层 run_loop_direct vs 裸直调 wall-clock 比 < 2.0x (本地实测应 ~1.0x)。

    2026-08-01 批次 3b C2 改义: 原"新壳 vs legacy 甲骨文"对比对象 (legacy) 已删,
    现锁定测试适配层不引入可观开销 — 它只做参数转发, ratio 应接近 1.0。
    """
    price, high, low, op, entry = _make_data()
    args = _args(price, high, low, op, entry)
    _run_raw(price, high, low, op, entry)
    run_loop_direct(*args)
    t0 = time.time()
    for _ in range(3):
        _run_raw(price, high, low, op, entry)
    t_raw = (time.time() - t0) / 3
    t0 = time.time()
    for _ in range(3):
        run_loop_direct(*args)
    t_adapter = (time.time() - t0) / 3
    ratio = t_adapter / t_raw if t_raw > 0 else 0
    assert ratio < 2.0, (
        f"适配层开销 {ratio:.2f}x 超阈值(raw={t_raw:.3f}s adapter={t_adapter:.3f}s); "
        f"run_loop_direct 只做参数转发, 本地应 ~1.0x")
