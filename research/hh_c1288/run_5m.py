"""
航海突破 5m 终审 (2024-06-27 ~ 2026-08-02)
日线选股 + 真5分钟K线回测
"""
import json, os, sys, time, copy
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from backtest.engine import BacktestEngine
from backtest.stop_config import load_stop_config
from selection.selector import StockSelector

os.makedirs("research/hh_c1288", exist_ok=True)
START, END = "20240627", "20260802"
BASE_STOP = load_stop_config()

# 5m engine config
BT_5M = {"initial_capital": 1000000.0, "commission": 0.0003, "slippage": 0.001,
         "period": "5m", "degrade_5m": False,
         "position_sizing": {"max_positions": 999, "min_buy_amount": 2000.0,
                             "max_buy_amount": 20000.0, "lot_size": 100, "min_lots": 1}}

# 1d engine config (对比)
BT_1D = {"initial_capital": 1000000.0, "commission": 0.0003, "slippage": 0.001,
         "period": "1d", "degrade_5m": True,
         "position_sizing": {"max_positions": 999, "min_buy_amount": 2000.0,
                             "max_buy_amount": 20000.0, "lot_size": 100, "min_lots": 1}}

def run(label, period, stop_config, bt_cfg):
    # 拉信号 (始终1d)
    cache = f"research/hh_c1288/hh_5mwindow_selections.parquet"
    if os.path.exists(cache):
        sel = pd.read_parquet(cache)
    else:
        sc = {"formula_name": "航海突破", "formula_arg": "",
              "universe": {"type": "50", "exclude_st": True},
              "period": "1d", "dividend_type": 1}
        sel = StockSelector(sc).run(start_time=START, end_time=END)
        if sel is not None and len(sel):
            sel.to_parquet(cache)
    if sel is None or not len(sel):
        print(f"[{label}] 0 signals", flush=True)
        return None
    print(f"[{label}] 信号={len(sel)} stocks={sel['stock_code'].nunique()}", flush=True)

    t0 = time.time()
    engine = BacktestEngine(bt_cfg)
    result = engine.run(selections=sel, start_time=START, end_time=END,
                        stop_config=stop_config)
    m = result.get("metrics") or {}
    ann = m.get("annualized_return", 0)
    dd = m.get("max_drawdown", 0)
    sp = m.get("sharpe_ratio", 0)
    wr = m.get("win_rate", 0)
    trades = m.get("total_trades")
    elapsed = time.time() - t0
    print(f"[{label}] ann={ann*100:.1f}% dd={dd*100:.1f}% sharpe={sp:.2f} "
          f"wr={wr*100:.1f}% trades={trades} t={elapsed:.0f}s", flush=True)
    return {"label": label, "period": period, "ann": ann, "dd": dd,
            "sharpe": sp, "wr": wr, "trades": trades,
            "cum": m.get("cumulative_return", 0)}


if __name__ == "__main__":
    results = {}

    # 基准 stop
    r = run("HH_基准_1d", "1d", BASE_STOP, BT_1D)
    if r: results["HH_基准_1d"] = r

    # cs6 stop (1d)
    stop_cs6 = copy.deepcopy(BASE_STOP)
    stop_cs6["cost_stop"]["threshold"] = -0.06
    r = run("HH_cs6_1d", "1d", stop_cs6, BT_1D)
    if r: results["HH_cs6_1d"] = r

    # cs6 stop (5m) ← 终审
    r = run("HH_cs6_5m", "5m", stop_cs6, BT_5M)
    if r: results["HH_cs6_5m"] = r

    with open("research/hh_c1288/terminal_5m.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)

    print("\n=== 5m 终审对比 (2024-06-27 ~ 今) ===", flush=True)
    for k, r in results.items():
        print(f"  {k}: ann={r['ann']*100:.1f}% dd={r['dd']*100:.1f}% sharpe={r['sharpe']:.2f}", flush=True)
    print("\n5m/1d 比率 = %.2fx (1d 虚高倍数)" % (results.get("HH_cs6_1d",{}).get("ann",1) / max(results.get("HH_cs6_5m",{}).get("ann",0.001), 0.001)), flush=True)
