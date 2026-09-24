"""
航海突破 + C1288 基准回测 (全A股池, 2019-2026)
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from backtest.engine import BacktestEngine
from backtest.stop_config import load_stop_config
from selection.selector import StockSelector

os.makedirs("research/hh_c1288", exist_ok=True)

BT_CFG = {
    "initial_capital": 1000000.0,
    "commission": 0.0003, "slippage": 0.001,
    "period": "1d", "degrade_5m": True,
    "position_sizing": {"max_positions": 999, "min_buy_amount": 2000.0,
                        "max_buy_amount": 20000.0, "lot_size": 100, "min_lots": 1},
}

FORMULAS = [
    ("航海突破", ""),
    ("C1288", "10"),
]

def run_one(formula, formula_arg, start, end, stop_config):
    sel_cfg = {"formula_name": formula, "formula_arg": formula_arg,
               "universe": {"type": "50", "exclude_st": True},
               "period": "1d", "dividend_type": 1}
    t0 = time.time()
    selections = StockSelector(sel_cfg).run(start_time=start, end_time=end)
    n = 0 if selections is None else len(selections)
    n_stocks = 0 if not n else selections['stock_code'].nunique()
    print(f"[{formula}] 信号={n} 股票={n_stocks}", flush=True)
    if not n:
        return None

    safe = formula
    selections.to_parquet(f"research/hh_c1288/{safe}_selections.parquet")
    engine = BacktestEngine(BT_CFG)
    result = engine.run(selections=selections, start_time=start, end_time=end,
                        stop_config=stop_config)
    m = result.get("metrics") or {}
    elapsed = time.time() - t0
    print(f"[{formula}] elapsed={elapsed:.0f}s "
          f"cum={m.get('cumulative_return',0)*100:.2f}% ann={m.get('annualized_return',0)*100:.2f}% "
          f"dd={m.get('max_drawdown',0)*100:.2f}% sharpe={m.get('sharpe_ratio',0):.2f} "
          f"trades={m.get('total_trades')} wr={m.get('win_rate',0)*100:.1f}%", flush=True)

    with open(f"research/hh_c1288/{safe}_metrics.json", "w", encoding="utf-8") as f:
        json.dump({"formula": formula, "range": [start, end], "metrics": m},
                  f, ensure_ascii=False, default=str, indent=1)
    if hasattr(result.get("trades"), "to_parquet"):
        result["trades"].to_parquet(f"research/hh_c1288/{safe}_trades.parquet")
    if hasattr(result.get("equity_curve"), "to_parquet"):
        result["equity_curve"].to_parquet(f"research/hh_c1288/{safe}_equity.parquet")
    return m


if __name__ == "__main__":
    start, end = "20190101", "20260802"
    stop_config = load_stop_config()
    for fm, arg in FORMULAS:
        run_one(fm, arg, start, end, stop_config)
    print("[ALL DONE]", flush=True)
