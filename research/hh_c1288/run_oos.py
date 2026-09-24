"""
OOS 验证: 航海突破 (cs6) + C1288 (N=3) on 2024-01-01 ~ 2026-08-02
"""
import json, os, sys, time, copy
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from backtest.engine import BacktestEngine
from backtest.stop_config import load_stop_config
from selection.selector import StockSelector

os.makedirs("research/hh_c1288", exist_ok=True)
OOS_START, OOS_END = "20240101", "20260802"
BASE_STOP = load_stop_config()
BT_CFG = {"initial_capital": 1000000.0, "commission": 0.0003, "slippage": 0.001,
          "period": "1d", "degrade_5m": True,
          "position_sizing": {"max_positions": 999, "min_buy_amount": 2000.0,
                              "max_buy_amount": 20000.0, "lot_size": 100, "min_lots": 1}}

RUNS = [
    ("航海突破_cs6", "航海突破", "", {"cost_stop": {"enabled": True, "threshold": -0.06}}),
    ("航海突破_基准", "航海突破", "", None),
    ("C1288_n3", "C1288", "3", None),
    ("C1288_n10_基准", "C1288", "10", None),
]

def run_one(label, formula, formula_arg, stop_overrides):
    cache = f"research/hh_c1288/{label}_oos_selections.parquet"
    if os.path.exists(cache):
        sel = pd.read_parquet(cache)
    else:
        sc = {"formula_name": formula, "formula_arg": formula_arg,
              "universe": {"type": "50", "exclude_st": True},
              "period": "1d", "dividend_type": 1}
        sel = StockSelector(sc).run(start_time=OOS_START, end_time=OOS_END)
        if sel is not None and len(sel):
            sel.to_parquet(cache)

    if sel is None or not len(sel):
        print(f"[{label}] 0 signals", flush=True)
        return None

    stop = copy.deepcopy(BASE_STOP)
    if stop_overrides:
        for k, v in stop_overrides.items():
            if k in stop:
                stop[k].update(v)

    t0 = time.time()
    engine = BacktestEngine(BT_CFG)
    result = engine.run(selections=sel, start_time=OOS_START, end_time=OOS_END,
                        stop_config=stop)
    m = result.get("metrics") or {}
    ann = m.get("annualized_return", 0)
    dd = m.get("max_drawdown", 0)
    sp = m.get("sharpe_ratio", 0)
    wr = m.get("win_rate", 0)
    trades = m.get("total_trades")
    elapsed = time.time() - t0
    print(f"[{label}] OOS: ann={ann*100:.1f}% dd={dd*100:.1f}% sharpe={sp:.2f} "
          f"wr={wr*100:.1f}% trades={trades} t={elapsed:.0f}s", flush=True)

    if hasattr(result.get("trades"), "to_parquet"):
        result["trades"].to_parquet(f"research/hh_c1288/{label}_oos_trades.parquet")

    return {"label": label, "ann": ann, "dd": dd, "sharpe": sp, "wr": wr,
            "trades": trades, "cum": m.get("cumulative_return", 0)}


if __name__ == "__main__":
    all_results = {}
    for label, fm, arg, overrides in RUNS:
        r = run_one(label, fm, arg, overrides)
        if r:
            all_results[label] = r

    with open("research/hh_c1288/oos_results.json", "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=1)

    print("\n=== OOS 对比 ===", flush=True)
    for label, r in all_results.items():
        print(f"  {label}: ann={r['ann']*100:.1f}% dd={r['dd']*100:.1f}% "
              f"sharpe={r['sharpe']:.2f}", flush=True)
