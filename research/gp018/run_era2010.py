"""GUPIAO_018 2010-2015 回测 (用户指定参数): 日线, 止损优先, -12% / 6%+15% / 3.5%+1% / 20天。"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from backtest.engine import BacktestEngine

STOP = {
    "priority": "stop_first",
    "cost_stop": {"enabled": True, "threshold": -0.12},
    "trailing_stop": {"enabled": True, "activation": 0.035, "drawdown": 0.01},
    "ladder_tp": {"enabled": True, "levels": [
        {"profit": 0.06, "sell_ratio": 0.30},
        {"profit": 0.15, "sell_ratio": 0.30},
    ]},
    "time_stop": {"enabled": True, "max_hold_days": 20},
    "cond_time_stop": {"enabled": False},
    "first_day": {"enabled": False},
    "formula_sell": {"enabled": False},
}

BT = {
    "initial_capital": 1000000.0,
    "commission": 0.0003,
    "slippage": 0.001,
    "period": "1d",
    "degrade_5m": False,
    "position_sizing": {"max_positions": 999, "min_buy_amount": 2000.0,
                        "max_buy_amount": float(os.environ.get("MAXBUY", "20000")), "lot_size": 100, "min_lots": 1},
}

START, END = "20100101", "20150101"

sel = pd.read_parquet("research/gp018/results/server_gp018_2010_2015.parquet")
sel["select_date"] = pd.to_datetime(sel["select_date"])
print(f"[INFO] 信号 {len(sel)} 条 / {sel['stock_code'].nunique()} 只", flush=True)
t0 = time.time()
result = BacktestEngine(BT).run(selections=sel, start_time=START, end_time=END, stop_config=STOP)
m = result.get("metrics") or {}
print(f"[DONE] elapsed={time.time()-t0:.0f}s "
      f"cum={m.get('cumulative_return',0)*100:.2f}% ann={m.get('annualized_return',0)*100:.2f}% "
      f"dd={m.get('max_drawdown',0)*100:.2f}% sharpe={m.get('sharpe_ratio',0):.2f} "
      f"trades={m.get('total_trades')} wr={m.get('win_rate',0)*100:.1f}% "
      f"pf={m.get('profit_factor',0):.2f} avghold={m.get('avg_hold_days',0):.1f}", flush=True)
with open("research/gp018/results/era2010_G0_userspec.json", "w", encoding="utf-8") as f:
    json.dump({"range": [START, END], "stop": STOP, "metrics": m}, f, ensure_ascii=False, default=str, indent=1)
if hasattr(result.get("trades"), "to_parquet"):
    result["trades"].to_parquet("research/gp018/results/era2010_G0_userspec_trades.parquet")
if hasattr(result.get("equity_curve"), "to_parquet"):
    result["equity_curve"].to_parquet("research/gp018/results/era2010_G0_userspec_equity.parquet")
print("[SAVED]", flush=True)
