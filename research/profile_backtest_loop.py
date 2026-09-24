"""回测核心循环性能画像 (2026-08-16, 药3 numba 决策前置测量)。

用途: 量化 BacktestLoop 核心循环的绝对耗时 + 热点分布, 判断 numba 值不值。
用法:
    python research/profile_backtest_loop.py [n_days] [n_stocks] [bpday]
默认: 120 交易日 × 500 股 × 240 bar/日 (1m, 半年的中等规模), 约 28800 根 bar。
不依赖 TDX/真实行情 —— 合成随机游走数据, 只测循环本身 (数据加载/选股另算)。
"""
from __future__ import annotations

import cProfile
import io
import os
import pstats
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from backtest.loop import build_backtest_loop
from tests.test_loop_parity import BASE_PARAMS


def make_1m_like(n_days: int, n_stocks: int, bpday: int, seed: int = 7,
                 p_signal: float = 0.08):
    """合成 1m 形状数据: 信号在每日首根 bar (i % bpday == 0), 每只股每天 p_signal 概率。"""
    rng = np.random.default_rng(seed)
    n_dates = n_days * bpday
    vol = 0.001  # 1m 单根波动约千1
    price = 10.0 * np.cumprod(1.0 + rng.normal(0, vol, (n_dates, n_stocks)), axis=0)
    high = price * (1 + np.abs(rng.normal(0, vol, (n_dates, n_stocks))))
    low = price * (1 - np.abs(rng.normal(0, vol, (n_dates, n_stocks))))
    open_ = price * (1 + rng.normal(0, vol * 0.5, (n_dates, n_stocks)))
    entry = np.zeros((n_dates, n_stocks), dtype=bool)
    day_bars = np.arange(0, n_dates, bpday)
    for ci in range(n_stocks):
        mask = rng.random(len(day_bars)) < p_signal
        entry[day_bars[mask], ci] = True
    return price.astype(np.float32), high.astype(np.float32), \
        low.astype(np.float32), open_.astype(np.float32), entry


def build_and_run(price, high, low, open_, entry, max_buy_amount=None):
    kw = BASE_PARAMS
    max_ba = kw["max_buy_amount"] if max_buy_amount is None else max_buy_amount
    loop = build_backtest_loop(
        kw["initial_capital"], kw["commission"],
        kw["min_buy_amount"], max_ba, kw["lot_size"], kw["min_lots"],
        True, kw["cost_stop_threshold"],
        True, kw["trailing_activation"], kw["trailing_drawdown"],
        True, kw["ladder_profits"], kw["ladder_ratios"], kw["n_ladder"],
        True, kw["max_hold_days"],
        False, kw["cond_time_days"], kw["cond_time_profit"],
        False, kw["first_day_target"],
        bpday=1, slippage=kw["slippage"], stamp_tax=kw["stamp_tax"],
        max_position_pct=1.0,
    )
    return loop.run(price, entry, high, low, open_, None, None, None)


def main() -> None:
    n_days = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    n_stocks = int(sys.argv[2]) if len(sys.argv) > 2 else 500
    bpday = int(sys.argv[3]) if len(sys.argv) > 3 else 240
    n_dates = n_days * bpday
    print(f"规模: {n_days} 交易日 × {n_stocks} 股 × {bpday} bar/日 = {n_dates} 根 bar "
          f"(≈{n_dates * n_stocks / 1e6:.0f}M 单元格)")

    price, high, low, open_, entry = make_1m_like(n_days, n_stocks, bpday)
    print(f"矩阵内存: price={price.nbytes/1e6:.1f}MB ×5 ≈ {price.nbytes*5/1e6:.1f}MB")

    build_and_run(price, high, low, open_, entry)  # 预热
    t0 = time.time()
    build_and_run(price, high, low, open_, entry)
    dt = time.time() - t0
    print(f"\n核心循环耗时: {dt:.3f}s  ({dt / n_dates * 1e6:.1f} µs/bar)")

    prof = cProfile.Profile()
    prof.enable()
    build_and_run(price, high, low, open_, entry)
    prof.disable()
    s = io.StringIO()
    ps = pstats.Stats(prof, stream=s).sort_stats("tottime")
    ps.print_stats(18)
    print("\n── 热点 Top (tottime) ──")
    print(s.getvalue())


if __name__ == "__main__":
    main()
