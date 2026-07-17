"""临时基准: 回测引擎速度量化 + 热点定位 + 优化点 A/B 验证。

用法: python tools/bench_engine_speed.py [scale]
  scale: small(默认, 500d x 100股) / mid(750d x 500股) / 5m(48bar/day x 250d x 100股)
"""
import cProfile
import io
import pstats
import sys
import time

import numpy as np

sys.path.insert(0, ".")
from backtest.engine import _simulate_core_v3, _simulate_core_v3_legacy
from tests.test_loop_parity import BASE_PARAMS


def make_data(n_dates, n_stocks, seed=1, entries_per_stock=8):
    rng = np.random.default_rng(seed)
    price = 10.0 * np.cumprod(1.0 + rng.normal(0, 0.02, (n_dates, n_stocks)), axis=0)
    high = price * (1 + np.abs(rng.normal(0, 0.02, (n_dates, n_stocks))))
    low = price * (1 - np.abs(rng.normal(0, 0.02, (n_dates, n_stocks))))
    op = price * (1 + rng.normal(0, 0.01, (n_dates, n_stocks)))
    entry = np.zeros((n_dates, n_stocks), dtype=bool)
    for ci in range(n_stocks):
        idx = rng.choice(n_dates // 2, size=min(entries_per_stock, n_dates // 2), replace=False)
        entry[idx, ci] = True
    return price, high, low, op, entry


def args_of(price, high, low, op, entry, bpday=1):
    kw = BASE_PARAMS
    return (price, entry, kw["initial_capital"], kw["commission"],
            kw["min_buy_amount"], kw["max_buy_amount"], kw["lot_size"], kw["min_lots"],
            True, kw["cost_stop_threshold"], True, kw["trailing_activation"], kw["trailing_drawdown"],
            True, kw["ladder_profits"], kw["ladder_ratios"], kw["n_ladder"],
            True, kw["max_hold_days"], False, kw["cond_time_days"], kw["cond_time_profit"],
            False, kw["first_day_target"], 1, high, low, bpday, kw["slippage"], kw["stamp_tax"],
            None, None, op, None, 1.0, 1, False, False, 1.0)


def bench(fn, args, n=3):
    fn(*args)  # 预热
    best = float("inf")
    for _ in range(n):
        t0 = time.perf_counter()
        fn(*args)
        best = min(best, time.perf_counter() - t0)
    return best


def main():
    scale = sys.argv[1] if len(sys.argv) > 1 else "small"
    if scale == "small":
        n_dates, n_stocks, bpday = 500, 100, 1
    elif scale == "mid":
        n_dates, n_stocks, bpday = 750, 500, 1
    else:  # 5m
        n_dates, n_stocks, bpday = 250 * 48, 100, 48
    price, high, low, op, entry = make_data(n_dates, n_stocks)
    args = args_of(price, high, low, op, entry, bpday)

    t_legacy = bench(_simulate_core_v3_legacy, args)
    t_new = bench(_simulate_core_v3, args)
    n_bars = n_dates * n_stocks
    print(f"scale={scale}  bars={n_bars:,}  bpday={bpday}")
    print(f"legacy : {t_legacy*1000:8.1f} ms  ({n_bars/t_legacy/1e6:.2f} Mbar/s)")
    print(f"new    : {t_new*1000:8.1f} ms  ({n_bars/t_new/1e6:.2f} Mbar/s)  ratio={t_new/t_legacy:.2f}x")

    # cProfile 热点
    pr = cProfile.Profile()
    pr.enable()
    _simulate_core_v3(*args)
    pr.disable()
    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
    ps.print_stats(25)
    print("\n===== cProfile top 25 (cumulative) =====")
    print(s.getvalue())

    s2 = io.StringIO()
    ps2 = pstats.Stats(pr, stream=s2).sort_stats("tottime")
    ps2.print_stats(25)
    print("\n===== cProfile top 25 (tottime) =====")
    print(s2.getvalue())


if __name__ == "__main__":
    main()
