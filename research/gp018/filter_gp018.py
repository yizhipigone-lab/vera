"""GUPIAO_018 信号过滤变体: 服务端信号 + 因果特征过滤 + 引擎回测。

变体:
  G0  基线(不过滤)
  G1  趋势结构: C>MA60 或 MA20>MA60
  G2  追高规避: 信号前5日累计涨幅 ≤ 15%
  G3  量能上限: 信号日量比(V/MA5V) ≤ 3
  G4  G1+G2+G3 组合
  G5  G4 + 仅20cm板(30/68)
出场变体通过 stop_name 控制 (base/cost12/time20/nocost/trail15_3)。
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import pandas as pd
from backtest.engine import BacktestEngine
from research.gp018.run_gp018 import BASE_STOP, BT_CFG, stop_variant


def load_features(sel: pd.DataFrame) -> pd.DataFrame:
    """为信号 (stock_code, select_date) 计算因果特征。"""
    rows = []
    for code, g in sel.groupby("stock_code"):
        p = f"data/kline_cache/1d/{code}.parquet"
        if not os.path.exists(p):
            continue
        df = pd.read_parquet(p).sort_values("date").reset_index(drop=True)
        c, h, l, v = (df[x].astype(float) for x in ["close", "high", "low", "volume"])
        ma20 = c.rolling(20, min_periods=1).mean()
        ma60 = c.rolling(60, min_periods=1).mean()
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        feat = pd.DataFrame({
            "date": df["date"],
            "ret5": c / c.shift(5) - 1,
            "vol_ratio": v / v.rolling(5, min_periods=1).mean(),
            "above_ma60": (c > ma60).astype(float),
            "bias20": (c / ma20 - 1),
            "ma20_gt_ma60": (ma20 > ma60).astype(float),
            "atr14_pct": tr.rolling(14, min_periods=1).mean() / c,
        })
        m = g.merge(feat, left_on="select_date", right_on="date", how="left").drop(columns=["date"])
        rows.append(m)
    return pd.concat(rows, ignore_index=True)


def apply_variant(feat: pd.DataFrame, variant: str) -> pd.DataFrame:
    m = pd.Series(True, index=feat.index)
    if variant in ("G1", "G4", "G5"):
        m &= (feat["above_ma60"] == 1) | (feat["ma20_gt_ma60"] == 1)
    if variant in ("G2", "G4", "G5"):
        m &= feat["ret5"] <= 0.15
    if variant in ("G3", "G4", "G5"):
        m &= feat["vol_ratio"] <= 3.0
    if variant == "G5":
        m &= feat["stock_code"].str[:2].isin(["30", "68"])
    if variant == "G1only":
        m = (feat["above_ma60"] == 1) | (feat["ma20_gt_ma60"] == 1)
    if variant == "G2only":
        m = feat["ret5"] <= 0.15
    if variant == "G3only":
        m = feat["vol_ratio"] <= 3.0
    if variant == "G23":
        m = (feat["ret5"] <= 0.15) & (feat["vol_ratio"] <= 3.0)
    return feat[m]


def run_variant(selections_path, variant, stop_name, start, end, tag):
    sel = pd.read_parquet(selections_path)
    sel["select_date"] = pd.to_datetime(sel["select_date"])
    ds = sel["select_date"].dt.strftime("%Y%m%d")
    sel = sel[(ds >= start) & (ds <= end)]
    feat = load_features(sel)
    out = apply_variant(feat, variant)
    out = out[["stock_code", "select_date"]].sort_values(
        ["select_date", "stock_code"]).reset_index(drop=True)
    print(f"[INFO] {variant}/{stop_name} {start}~{end} 信号 {len(sel)}→{len(out)}", flush=True)
    if not len(out):
        print(f"[DONE] {tag} 无信号", flush=True)
        return
    import copy
    st = stop_variant(stop_name)
    if stop_name == "nocost":
        st = copy.deepcopy(BASE_STOP)
        st["cost_stop"] = {"enabled": False, "threshold": -0.08}
    engine = BacktestEngine(BT_CFG)
    t0 = time.time()
    result = engine.run(selections=out, start_time=start, end_time=end, stop_config=st)
    m = result.get("metrics") or {}
    print(f"[DONE] {tag} elapsed={time.time()-t0:.0f}s "
          f"cum={m.get('cumulative_return',0)*100:.2f}% ann={m.get('annualized_return',0)*100:.2f}% "
          f"dd={m.get('max_drawdown',0)*100:.2f}% sharpe={m.get('sharpe_ratio',0):.2f} "
          f"trades={m.get('total_trades')} wr={m.get('win_rate',0)*100:.1f}% "
          f"pf={m.get('profit_factor',0):.2f}", flush=True)
    os.makedirs("research/gp018/results", exist_ok=True)
    with open(f"research/gp018/results/{tag}.json", "w", encoding="utf-8") as f:
        json.dump({"variant": variant, "stop": stop_name, "range": [start, end],
                   "metrics": m}, f, ensure_ascii=False, default=str, indent=1)
    if hasattr(result.get("trades"), "to_parquet"):
        result["trades"].to_parquet(f"research/gp018/results/{tag}_trades.parquet")
    if hasattr(result.get("equity_curve"), "to_parquet"):
        result["equity_curve"].to_parquet(f"research/gp018/results/{tag}_equity.parquet")


if __name__ == "__main__":
    # 参数: selections.parquet variant stop start end tag
    run_variant(*sys.argv[1:7])
