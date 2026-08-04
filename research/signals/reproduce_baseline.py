"""用落盘的 selections parquet + 指定止盈止损配置复现基准回测。

用法:
  python -X utf8 research/signals/reproduce_baseline.py <selections.parquet> <start> <end> [tag] [degrade] [priority]
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from backtest.engine import BacktestEngine
from utils.config_loader import ConfigLoader

# 2026-07-31 1161.89% 那次的止盈止损结构 (从 stop_config_summary 反推):
# 成本止损 -8% (Low触发); 阶梯 盈利10000%卖2000% ×2 (profit=100/120, 单位错配=实际不触发);
# 移动止盈 20%激活/2%回撤; 时间止损 12天
BASELINE_STOP = {
    "priority": "trailing_first",
    "cost_stop": {"enabled": True, "threshold": -0.08},
    "trailing_stop": {"enabled": True, "activation": 0.20, "drawdown": 0.02},
    "ladder_tp": {"enabled": True, "levels": [
        {"profit": 100.0, "sell_ratio": 20.0},
        {"profit": 120.0, "sell_ratio": 20.0},
    ]},
    "time_stop": {"enabled": True, "max_hold_days": 12},
    "cond_time_stop": {"enabled": False},
    "first_day": {"enabled": False},
    "formula_sell": {"enabled": False},
}

BT_CFG = {
    "initial_capital": 1000000.0,
    "commission": 0.0003,
    "slippage": 0.001,
    "period": "1d",
    "position_sizing": {"max_positions": 999, "min_buy_amount": 2000.0,
                        "max_buy_amount": 20000.0, "lot_size": 100, "min_lots": 1},
}


def main():
    sel_path, start, end = sys.argv[1], sys.argv[2], sys.argv[3]
    tag = sys.argv[4] if len(sys.argv) > 4 else "repro"
    degrade = (sys.argv[5].lower() == "true") if len(sys.argv) > 5 else True
    priority = sys.argv[6] if len(sys.argv) > 6 else "trailing_first"

    selections = pd.read_parquet(sel_path)
    selections["select_date"] = pd.to_datetime(selections["select_date"])
    stop = dict(BASELINE_STOP)
    stop["priority"] = priority
    bt = dict(BT_CFG)
    bt["degrade_5m"] = degrade

    print(f"[INFO] selections={len(selections)} stocks={selections['stock_code'].nunique()} "
          f"range={start}~{end} degrade={degrade} priority={priority}", flush=True)
    t0 = time.time()
    engine = BacktestEngine(bt)
    result = engine.run(selections=selections, start_time=start, end_time=end, stop_config=stop)
    m = result["metrics"]
    print(f"[DONE] elapsed={time.time()-t0:.0f}s tag={tag}")
    print(f"  cumulative={m.get('cumulative_return',0)*100:.2f}%  annualized={m.get('annualized_return',0)*100:.2f}%")
    print(f"  maxDD={m.get('max_drawdown',0)*100:.2f}%  sharpe={m.get('sharpe_ratio',0):.2f}  "
          f"trades={m.get('total_trades')}  winrate={m.get('win_rate',0)*100:.1f}%  PF={m.get('profit_factor',0):.2f}")
    os.makedirs("research/results", exist_ok=True)
    out = f"research/results/{tag}.json"
    import json
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"metrics": m, "stop": {k: v for k, v in stop.items()},
                   "selections": sel_path, "range": [start, end]}, f, ensure_ascii=False, default=str, indent=1)
    trades = result.get("trades")
    if hasattr(trades, "to_parquet"):
        trades.to_parquet(f"research/results/{tag}_trades.parquet")
    eq = result.get("equity_curve")
    if hasattr(eq, "to_parquet"):
        eq.to_parquet(f"research/results/{tag}_equity.parquet")
    print(f"  saved -> {out}")


if __name__ == "__main__":
    main()
