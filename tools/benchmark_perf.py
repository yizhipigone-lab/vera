"""性能基准工具 — 新 BacktestLoop vs legacy 甲骨文 wall-clock 对比。

审计要求（2026-07-15 独立审计）: 工作成果报告中声称的 1.40x 无对应
benchmark 脚本。本工具提供可复现的性能测量。

用法:
    python tools/benchmark_perf.py              # 默认: 3 组数据规模, 各 7 轮
    python tools/benchmark_perf.py --quick      # 快速: 仅 500×100, 3 轮
    python tools/benchmark_perf.py --full       # 完整: 含 2500×500, 5 轮

输出: 每行一个数据规模的 wall-clock 比 + 均值/标准差。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 确保项目根目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from backtest.engine import _simulate_core_v3, _simulate_core_v3_legacy

# ── 参数（复用 test_loop_parity.BASE_PARAMS） ──
BASE_PARAMS = dict(
    initial_capital=1_000_000.0, commission=0.0003,
    min_buy_amount=1000.0, max_buy_amount=200_000.0, lot_size=100, min_lots=1,
    cost_stop_threshold=-0.05,
    trailing_activation=0.05, trailing_drawdown=0.10,
    ladder_profits=np.array([0.06, 0.15], dtype=np.float64),
    ladder_ratios=np.array([0.5, 0.5], dtype=np.float64),
    n_ladder=2,
    max_hold_days=10,
    cond_time_days=3, cond_time_profit=0.08,
    first_day_target=0.03,
    bpday=1, slippage=0.001, stamp_tax=0.001,
    max_position_pct=1.0,
)


def make_data(n_dates: int, n_stocks: int, seed: int = 1):
    """生成合成 OHLC + 信号。"""
    rng = np.random.default_rng(seed)
    price = 10.0 * np.cumprod(1.0 + rng.normal(0, 0.02, (n_dates, n_stocks)), axis=0)
    high = price * (1 + np.abs(rng.normal(0, 0.02, (n_dates, n_stocks))))
    low = price * (1 - np.abs(rng.normal(0, 0.02, (n_dates, n_stocks))))
    op = price * (1 + rng.normal(0, 0.01, (n_dates, n_stocks)))
    entry = np.zeros((n_dates, n_stocks), dtype=bool)
    for ci in range(n_stocks):
        entry[rng.choice(n_dates // 2, size=max(5, n_dates // 40), replace=False), ci] = True
    return price, high, low, op, entry


def build_args(price, high, low, op, entry):
    """构造 _simulate_core_v3 的位置参数 tuple。"""
    kw = BASE_PARAMS
    return (price, entry, kw["initial_capital"], kw["commission"],
            kw["min_buy_amount"], kw["max_buy_amount"], kw["lot_size"], kw["min_lots"],
            True, kw["cost_stop_threshold"], True, kw["trailing_activation"],
            kw["trailing_drawdown"],
            True, kw["ladder_profits"], kw["ladder_ratios"], kw["n_ladder"],
            True, kw["max_hold_days"], False, kw["cond_time_days"], kw["cond_time_profit"],
            False, kw["first_day_target"], 1, high, low, 1, kw["slippage"], kw["stamp_tax"],
            None, None, op, None, 1.0, 1, False, False, 1.0)


def measure(runner, args, warmup: int = 1, repeat: int = 5) -> list[float]:
    """测量 runner 的单次执行时间（ms）。返回 repeat 个测量值。"""
    # warm-up
    for _ in range(warmup):
        runner(*args)
    times = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        runner(*args)
        times.append((time.perf_counter() - t0) * 1000)  # ms
    return times


def bench_one(shape: tuple[int, int], warmup: int = 2, repeat: int = 5):
    """对一个数据规模做 legacy vs new 对比。"""
    n_dates, n_stocks = shape
    price, high, low, op, entry = make_data(n_dates, n_stocks)
    args = build_args(price, high, low, op, entry)

    t_legacy = measure(_simulate_core_v3_legacy, args, warmup=warmup, repeat=repeat)
    t_new = measure(_simulate_core_v3, args, warmup=warmup, repeat=repeat)

    ratios = [n / l for n, l in zip(t_new, t_legacy)]

    avg_legacy = np.mean(t_legacy)
    avg_new = np.mean(t_new)
    avg_ratio = np.mean(ratios)
    std_ratio = np.std(ratios, ddof=1) if len(ratios) > 1 else 0.0

    return avg_legacy, avg_new, avg_ratio, std_ratio, ratios


def main():
    parser = argparse.ArgumentParser(description="BacktestLoop vs legacy 性能基准")
    parser.add_argument("--quick", action="store_true",
                        help="快速模式: 仅 500×100, 3 轮")
    parser.add_argument("--full", action="store_true",
                        help="完整模式: 3 组规模含 2500×500")
    args = parser.parse_args()

    if args.quick:
        shapes = [(500, 100)]
        warmup, repeat = 1, 3
    elif args.full:
        shapes = [(500, 100), (1000, 250), (2500, 500)]
        warmup, repeat = 2, 5
    else:
        # 默认: 中等覆盖
        shapes = [(500, 100), (1000, 250)]
        warmup, repeat = 2, 5

    print(f"{'='*68}")
    print(f"  BacktestLoop vs _simulate_core_v3_legacy  性能基准")
    print(f"  数据规模 × {len(shapes)} | warmup={warmup} | repeat={repeat}")
    print(f"{'='*68}")
    print(f"{'规模':>12}  {'legacy(ms)':>10}  {'new(ms)':>10}  {'ratio':>7}  {'std':>6}  {'判定'}")
    print(f"{'-'*12}  {'-'*10}  {'-'*10}  {'-'*7}  {'-'*6}  {'-'*10}")

    all_ok = True
    for shape in shapes:
        avg_l, avg_n, avg_r, std_r, ratios = bench_one(shape, warmup, repeat)
        label = f"{shape[0]}×{shape[1]}"
        ratio_str = f"{avg_r:.2f}x"
        std_str = f"±{std_r:.2f}"
        verdict = "OK" if avg_r < 2.0 else "WARN"
        if avg_r >= 2.0:
            all_ok = False
        print(f"{label:>12}  {avg_l:>8.1f}ms  {avg_n:>8.1f}ms  {ratio_str:>7}  {std_str:>6}  {verdict}")

    print(f"{'='*68}")
    if all_ok:
        print(f"  结论: 所有规模退化 < 2x 阈值，无需优化。")
    else:
        print(f"  结论: 部分规模退化 ≥ 2x，建议加 __slots__ 或 buffer 复用。")
    print(f"  注意: 以上为 wall-clock 比，包含 Python 函数调用开销；")
    print(f"        大 numpy 运算占比高的规模下 ratio 会趋近 1.0。")
    print(f"{'='*68}")


if __name__ == "__main__":
    main()
