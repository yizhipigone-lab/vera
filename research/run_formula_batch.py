"""批量公式回测: TDX 服务端选股 (全A非ST) + VeraCore 引擎 (当前前端止盈止损配置)。

用法: python -X utf8 research/run_formula_batch.py <start> <end> <公式1> <公式2> ...
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from backtest.engine import BacktestEngine
from backtest.stop_config import load_stop_config
from selection.selector import StockSelector

BT_CFG = {
    "initial_capital": 1000000.0,
    "commission": 0.0003,
    "slippage": 0.001,
    "period": "1d",
    "degrade_5m": True,
    "position_sizing": {"max_positions": 999, "min_buy_amount": 2000.0,
                        "max_buy_amount": 20000.0, "lot_size": 100, "min_lots": 1},
}


def run_one(formula, start, end, stop_config):
    sel_cfg = {"formula_name": formula, "formula_arg": "",
               "universe": {"type": "50", "exclude_st": True},
               "period": "1d", "dividend_type": 1}
    t0 = time.time()
    try:
        selections = StockSelector(sel_cfg).run(start_time=start, end_time=end)
    except Exception as e:
        print(f"[FAIL] {formula} 选股异常: {type(e).__name__} {str(e)[:80]}", flush=True)
        return
    n = 0 if selections is None else len(selections)
    print(f"[INFO] {formula} 信号={n} 股票={0 if not n else selections['stock_code'].nunique()}", flush=True)
    if not n:
        print(f"[DONE] {formula} 无信号, 跳过回测", flush=True)
        return
    os.makedirs("research/formula_batch", exist_ok=True)
    safe = formula.replace("/", "_")
    selections.to_parquet(f"research/formula_batch/{safe}_selections.parquet")
    engine = BacktestEngine(BT_CFG)
    try:
        result = engine.run(selections=selections, start_time=start, end_time=end,
                            stop_config=stop_config)
    except Exception as e:
        print(f"[FAIL] {formula} 回测异常: {type(e).__name__} {str(e)[:80]}", flush=True)
        return
    m = result.get("metrics") or {}
    print(f"[DONE] {formula} elapsed={time.time()-t0:.0f}s "
          f"cum={m.get('cumulative_return',0)*100:.2f}% ann={m.get('annualized_return',0)*100:.2f}% "
          f"dd={m.get('max_drawdown',0)*100:.2f}% sharpe={m.get('sharpe_ratio',0):.2f} "
          f"trades={m.get('total_trades')} wr={m.get('win_rate',0)*100:.1f}% "
          f"pf={m.get('profit_factor',0):.2f} avghold={m.get('avg_hold_days',0):.1f}", flush=True)
    with open(f"research/formula_batch/{safe}_metrics.json", "w", encoding="utf-8") as f:
        json.dump({"formula": formula, "range": [start, end], "metrics": m},
                  f, ensure_ascii=False, default=str, indent=1)
    if hasattr(result.get("trades"), "to_parquet"):
        result["trades"].to_parquet(f"research/formula_batch/{safe}_trades.parquet")
    if hasattr(result.get("equity_curve"), "to_parquet"):
        result["equity_curve"].to_parquet(f"research/formula_batch/{safe}_equity.parquet")


if __name__ == "__main__":
    start, end = sys.argv[1], sys.argv[2]
    formulas = sys.argv[3:]
    stop_config = load_stop_config()  # config/default.yaml + current.yaml 合并口径
    print(f"[INFO] stop_config: {json.dumps(stop_config, ensure_ascii=False)[:300]}", flush=True)
    for fm in formulas:
        run_one(fm, start, end, stop_config)
    print("[ALL DONE]", flush=True)
