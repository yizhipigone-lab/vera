"""GUPIAO_018 训练/OOS 网格驱动: 特征算一次, 多变体复用。

用法: python -X utf8 research/gp018/run_grid.py <start> <end> <tag> <variant:stop>...
例: python -X utf8 research/gp018/run_grid.py 20190101 20231231 train G0:base G4:cost12
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from research.gp018.filter_gp018 import load_features, apply_variant
from research.gp018.run_gp018 import BASE_STOP, BT_CFG, stop_variant

if os.environ.get("DEGRADE") == "1":
    BT_CFG = dict(BT_CFG, degrade_5m=True)
if os.environ.get("PERIOD"):
    BT_CFG = dict(BT_CFG, period=os.environ["PERIOD"], degrade_5m=True)
import copy
import json
import time

import pandas as pd
from backtest.engine import BacktestEngine

SEL = "research/gp018/results/base_hs_a_base_selections.parquet"


def main():
    start, end, tag = sys.argv[1], sys.argv[2], sys.argv[3]
    specs = [s.split(":") for s in sys.argv[4:]]
    sel = pd.read_parquet(SEL)
    sel["select_date"] = pd.to_datetime(sel["select_date"])
    ds = sel["select_date"].dt.strftime("%Y%m%d")
    sel = sel[(ds >= start) & (ds <= end)]
    t0 = time.time()
    feat = load_features(sel)
    print(f"[INFO] 特征就绪 {len(feat)} 条 elapsed={time.time()-t0:.0f}s", flush=True)
    for variant, stop_name in specs:
        out = apply_variant(feat, variant)
        out = out[["stock_code", "select_date"]].sort_values(
            ["select_date", "stock_code"]).reset_index(drop=True)
        name = f"{tag}_{variant}_{stop_name}"
        print(f"[INFO] {name} 信号={len(out)}", flush=True)
        if not len(out):
            continue
        st = stop_variant(stop_name)
        if stop_name == "nocost":
            st = copy.deepcopy(BASE_STOP)
            st["cost_stop"] = {"enabled": False, "threshold": -0.08}
        if stop_name == "nocost_lad":
            st = copy.deepcopy(BASE_STOP)
            st["cost_stop"] = {"enabled": False, "threshold": -0.08}
            st["ladder_tp"] = {"enabled": True, "levels": [
                {"profit": 0.15, "sell_ratio": 0.30}, {"profit": 0.30, "sell_ratio": 0.30}]}
        if stop_name == "nocost_t25":
            st = copy.deepcopy(BASE_STOP)
            st["cost_stop"] = {"enabled": False, "threshold": -0.08}
            st["trailing_stop"] = {"enabled": True, "activation": 0.25, "drawdown": 0.03}
        if stop_name in ("nocost20", "nocost30"):
            st = copy.deepcopy(BASE_STOP)
            st["cost_stop"] = {"enabled": False, "threshold": -0.08}
            st["time_stop"] = {"enabled": True,
                               "max_hold_days": 20 if stop_name == "nocost20" else 30}
        engine = BacktestEngine(BT_CFG)
        t1 = time.time()
        try:
            result = engine.run(selections=out, start_time=start, end_time=end, stop_config=st)
        except Exception as e:
            print(f"[FAIL] {name}: {type(e).__name__} {str(e)[:80]}", flush=True)
            continue
        m = result.get("metrics") or {}
        print(f"[DONE] {name} elapsed={time.time()-t1:.0f}s "
              f"cum={m.get('cumulative_return',0)*100:.2f}% ann={m.get('annualized_return',0)*100:.2f}% "
              f"dd={m.get('max_drawdown',0)*100:.2f}% sharpe={m.get('sharpe_ratio',0):.2f} "
              f"trades={m.get('total_trades')} wr={m.get('win_rate',0)*100:.1f}% "
              f"pf={m.get('profit_factor',0):.2f}", flush=True)
        with open(f"research/gp018/results/{name}.json", "w", encoding="utf-8") as f:
            json.dump({"variant": variant, "stop": stop_name, "range": [start, end],
                       "metrics": m}, f, ensure_ascii=False, default=str, indent=1)
        if hasattr(result.get("trades"), "to_parquet"):
            result["trades"].to_parquet(f"research/gp018/results/{name}_trades.parquet")
        if hasattr(result.get("equity_curve"), "to_parquet"):
            result["equity_curve"].to_parquet(f"research/gp018/results/{name}_equity.parquet")
    print("[ALL DONE]", flush=True)


if __name__ == "__main__":
    main()
