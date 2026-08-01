"""1m vs 5m vs 1d 归因对比 (2026-07-26, 计划书 §3.6 阶段2)。

同配方同区间三周期跑, 回答: 5m 上验证的"2% 盘中触线是命门"在 1m 上是否成立。
用法: python tools/attr_1m_vs_5m.py [--universe-type 50] [--start 20260601] [--end 20260630]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


from backtest.engine import BacktestEngine
from core.connector import TdxConnector
from selection.selector import StockSelector

BT_BASE = {
    "initial_capital": 1000000.0,
    "commission": 0.0003,
    "slippage": 0.001,
    "stamp_tax": 0.0005,
    "enable_realistic_costs": True,
    "degrade_5m": True,   # 1m 路径应被守卫忽略 (warning 而非降级)
    "position_sizing": {"max_positions": 999, "min_buy_amount": 2000.0,
                        "max_buy_amount": 20000.0, "lot_size": 100, "min_lots": 1},
    "matrix_cache": True,
}
STOP = {
    "priority": "trailing_first",
    "cost_stop": {"enabled": True, "threshold": -0.08},
    "trailing_stop": {"enabled": True, "activation": 0.03, "drawdown": 0.02},
    "ladder_tp": {"enabled": True, "levels": [
        {"profit": 1.00, "sell_ratio": 0.20},
        {"profit": 1.20, "sell_ratio": 0.20}]},
    "time_stop": {"enabled": True, "max_hold_days": 12},
    "cond_time_stop": {"enabled": False},
    "first_day": {"enabled": False},
    "formula_sell": {"enabled": False},
}


def run(sel, period, start, end):
    eng = BacktestEngine({**BT_BASE, "period": period})
    t0 = time.perf_counter()
    r = eng.run(selections=sel, start_time=start, end_time=end, stop_config=STOP)
    dt = time.perf_counter() - t0
    m, t = r["metrics"], r["trades"]
    trailing = t[t["exit_reason"] == "移动止盈"] if len(t) else t
    print(f"[{period}] {dt:.1f}s trades={m['total_trades']} "
          f"cum={m['cumulative_return']:.4f} win={m['win_rate']:.4f} "
          f"maxdd={m['max_drawdown']:.4f}")
    if len(t):
        ret = t["return"]
        print(f"  ret: mean={ret.mean():.4f} median={ret.median():.4f} "
              f"p90={ret.quantile(0.9):.4f} max={ret.max():.4f} "
              f"hold_med={t['hold_days'].median():.0f}d")
        if len(trailing):
            print(f"  移动止盈 n={len(trailing)} ret_med={trailing['return'].median():.4f}")
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe-type", default="50")
    ap.add_argument("--start", default="20260601")
    ap.add_argument("--end", default="20260630")
    ap.add_argument("--formula", default="QUANTQQ")
    args = ap.parse_args()

    sel_cfg = {"formula_name": args.formula, "formula_arg": "",
               "universe": {"type": args.universe_type, "exclude_st": True},
               "period": "1d", "dividend_type": 1}
    TdxConnector.initialize()
    try:
        sel = StockSelector(sel_cfg).run(start_time=args.start, end_time=args.end)
        print(f"选股 (1d): {len(sel)} 条 [{args.start}~{args.end}]")
        results = {}
        for period in ("1d", "5m", "1m"):
            results[period] = run(sel, period, args.start, args.end)
        print("\n==== 三周期归因对比 ====")
        print(f"{'period':<8}{'trades':>8}{'cum':>10}{'win':>8}")
        for p, m in results.items():
            print(f"{p:<8}{m['total_trades']:>8}{m['cumulative_return']:>10.4f}"
                  f"{m['win_rate']:>8.4f}")
    finally:
        TdxConnector.close()


if __name__ == "__main__":
    main()
