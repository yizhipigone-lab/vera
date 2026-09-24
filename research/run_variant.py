"""变体定义 + 实验运行器。

变体 (信号过滤/排序 在 selections 层面, 出场 在 stop_config 层面):
  V0 诚实基线     : 无过滤, 基准止盈止损
  V1 量能确认     : vol_ratio>=1.5 且 body>0, 基准止盈止损
  V2 趋势过滤     : above_ma60 或 ma60_slope>0, 基准止盈止损
  V3 出场重构     : 无过滤, ATR 移动止损(×3) 替代 20%/2% 移动止盈, 成本-8%, 时间20天
  V4 大盘周期过滤 : regime != bear 才入场, 基准止盈止损
  V5 组合         : V1+V2+V4 过滤 + 每日 score 前10 + ATR 出场 + 仓位集中(单票上限4万)
"""
import argparse, json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd

from backtest.engine import BacktestEngine
from research.signals.reproduce_baseline import BASELINE_STOP, BT_CFG

POOL = "research/signals/universe_pool_50.csv"
FEATURES = "research/signals/causal_features.parquet"
REGIME = "research/signals/index_regime.parquet"

ATR_STOP = {
    "priority": "trailing_first",
    "cost_stop": {"enabled": True, "threshold": -0.08},
    "trailing_stop": {"enabled": False},
    "atr_stop": {"enabled": True, "period": 14, "multiplier": 3.0},
    "ladder_tp": {"enabled": False, "levels": []},
    "time_stop": {"enabled": True, "max_hold_days": 20},
    "cond_time_stop": {"enabled": False},
    "first_day": {"enabled": False},
    "formula_sell": {"enabled": False},
}


def load_features(start, end):
    feat = pd.read_parquet(FEATURES)
    pool = set(pd.read_csv(POOL, header=None)[0])
    feat = feat[feat.stock_code.isin(pool)].copy()
    ds = feat["select_date"].dt.strftime("%Y%m%d")
    feat = feat[(ds >= start) & (ds <= end)]
    reg = pd.read_parquet(REGIME)[["date", "regime"]]
    reg["date"] = pd.to_datetime(reg["date"])
    feat = feat.merge(reg, left_on="select_date", right_on="date", how="left")
    feat["regime"] = feat["regime"].fillna("range")
    return feat


def make_selections(variant: str, start: str, end: str) -> pd.DataFrame:
    feat = load_features(start, end)
    if variant == "V0":
        m = pd.Series(True, index=feat.index)
    elif variant == "V1":
        m = (feat["vol_ratio"] >= 1.5) & (feat["body"] > 0)
    elif variant == "V2":
        m = (feat["above_ma60"] == 1) | (feat["ma60_slope"] > 0)
    elif variant == "V3":
        m = pd.Series(True, index=feat.index)
    elif variant == "V4":
        m = feat["regime"] != "bear"
    elif variant == "V5":
        m = ((feat["vol_ratio"] >= 1.5) & (feat["body"] > 0)
             & ((feat["above_ma60"] == 1) | (feat["ma60_slope"] > 0))
             & (feat["regime"] != "bear"))
    elif variant == "V6":                      # V4 + 量能确认
        m = (feat["regime"] != "bear") & (feat["vol_ratio"] >= 1.5) & (feat["body"] > 0)
    elif variant in ("V8", "V4T1", "V4T2", "V4T3", "V4T4", "V8T2"):
        m = feat["regime"] != "bear"
    elif variant in ("V1K", "V1T2", "V1T4"):       # V1 及其止盈微调
        m = (feat["vol_ratio"] >= 1.5) & (feat["body"] > 0)
    elif variant in ("V1B4", "V1B6", "V1TR3"):
        m = (feat["vol_ratio"] >= 1.5) & (feat["body"] > 0)
    elif variant in ("V7B4",):
        m = ((feat["vol_ratio"] >= 1.5) & (feat["body"] > 0)
             & (feat["atr14_pct"] <= 0.035) & (feat["ret1"] <= 0.08))
    elif variant in ("V7", "V7T20", "V7NC", "V7NCT20"):  # V1 + 低波动 + 涨幅上限
        m = ((feat["vol_ratio"] >= 1.5) & (feat["body"] > 0)
             & (feat["atr14_pct"] <= 0.035) & (feat["ret1"] <= 0.08))
    elif variant in ("V6K", "V6T4"):             # V6 衍生
        m = (feat["regime"] != "bear") & (feat["vol_ratio"] >= 1.5) & (feat["body"] > 0)
    else:
        raise ValueError(variant)
    sel = feat[m].copy()
    if variant in ("V5", "V8", "V8T2", "V1K", "V6K"):
        sel = (sel.sort_values(["select_date", "score"], ascending=[True, False])
                  .groupby("select_date").head(10))
    return sel[["stock_code", "select_date"]].sort_values(
        ["select_date", "stock_code"]).reset_index(drop=True)


def stop_for(variant: str) -> dict:
    if variant in ("V3", "V5"):
        return ATR_STOP
    if variant == "V1TR3":
        import copy
        st = copy.deepcopy(BASELINE_STOP)
        st["trailing_stop"] = {"enabled": True, "activation": 0.20, "drawdown": 0.04}
        return st
    if variant in ("V7T20", "V7NC", "V7NCT20"):
        import copy
        st = copy.deepcopy(BASELINE_STOP)
        if variant in ("V7T20", "V7NCT20"):
            st["time_stop"] = {"enabled": True, "max_hold_days": 20}
        if variant in ("V7NC", "V7NCT20"):
            st["cost_stop"] = {"enabled": False, "threshold": -0.08}
        return st
    if variant.startswith("V4T") or variant in ("V8T2", "V6T4", "V1T2", "V1T4"):
        import copy
        st = copy.deepcopy(BASELINE_STOP)
        if variant in ("V4T1",):                     # 移动止盈 10%/3%
            st["trailing_stop"] = {"enabled": True, "activation": 0.10, "drawdown": 0.03}
        elif variant in ("V4T2", "V8T2", "V1T2"):     # 移动止盈 15%/3%
            st["trailing_stop"] = {"enabled": True, "activation": 0.15, "drawdown": 0.03}
        elif variant == "V4T3":                      # 时间止损 20天
            st["time_stop"] = {"enabled": True, "max_hold_days": 20}
        elif variant in ("V4T4", "V6T4", "V1T4"):     # 成本止损 -12%
            st["cost_stop"] = {"enabled": True, "threshold": -0.12}
        return st
    return BASELINE_STOP


def bt_for(variant: str) -> dict:
    bt = dict(BT_CFG)
    if variant in ("V5", "V8", "V8T2", "V1K", "V6K", "V1B4", "V7B4"):
        bt["position_sizing"] = dict(bt["position_sizing"], max_buy_amount=40000.0)
    if variant == "V1B6":
        bt["position_sizing"] = dict(bt["position_sizing"], max_buy_amount=60000.0)
    return bt


def run_one(variant, start, end, tag):
    sel = make_selections(variant, start, end)
    print(f"[INFO] {variant} {start}~{end} signals={len(sel)} stocks={sel['stock_code'].nunique()}", flush=True)
    if not len(sel):
        print(f"[SKIP] {tag} 无信号")
        return
    engine = BacktestEngine(bt_for(variant))
    t0 = time.time()
    result = engine.run(selections=sel, start_time=start, end_time=end, stop_config=stop_for(variant))
    m = result["metrics"]
    print(f"[DONE] {tag} elapsed={time.time()-t0:.0f}s "
          f"cum={m.get('cumulative_return',0)*100:.2f}% ann={m.get('annualized_return',0)*100:.2f}% "
          f"dd={m.get('max_drawdown',0)*100:.2f}% sharpe={m.get('sharpe_ratio',0):.2f} "
          f"trades={m.get('total_trades')} wr={m.get('win_rate',0)*100:.1f}% pf={m.get('profit_factor',0):.2f}",
          flush=True)
    os.makedirs("research/results", exist_ok=True)
    with open(f"research/results/{tag}.json", "w", encoding="utf-8") as f:
        json.dump({"variant": variant, "range": [start, end], "metrics": m}, f, ensure_ascii=False, default=str, indent=1)
    if hasattr(result.get("trades"), "to_parquet"):
        result["trades"].to_parquet(f"research/results/{tag}_trades.parquet")
    if hasattr(result.get("equity_curve"), "to_parquet"):
        result["equity_curve"].to_parquet(f"research/results/{tag}_equity.parquet")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", default="V0")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--tag", required=True)
    a = ap.parse_args()
    for v in a.variants.split(","):
        run_one(v, a.start, a.end, f"{a.tag}_{v}")
