"""MMT 策略研究运行器: 本地信号文件 + 变体过滤 + 出场变体 + 引擎回测。

用法: python -X utf8 research/mmt/run.py <variant> <stop> <start> <end> <tag>
  variant: base / g23 / trend / atrlow / regime
  stop:    base(技能默认 BASE_STOP) / cost12 / time20 / trail15_3 / fast6
环境变量: PERIOD=5m 切真5分钟口径
"""
import copy, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from backtest.engine import BacktestEngine
from research.gp018.filter_gp018 import load_features

# 技能默认出场架构 (stop_first, -6%, 3.5%/1%, 阶梯5%/50+10%/50, 12天)
BASE_STOP = {
    "priority": "stop_first",
    "cost_stop": {"enabled": True, "threshold": -0.06},
    "trailing_stop": {"enabled": True, "activation": 0.035, "drawdown": 0.01},
    "ladder_tp": {"enabled": True, "levels": [
        {"profit": 0.05, "sell_ratio": 0.50},
        {"profit": 0.10, "sell_ratio": 0.50},
    ]},
    "time_stop": {"enabled": True, "max_hold_days": 12},
    "cond_time_stop": {"enabled": False},
    "first_day": {"enabled": False},
    "formula_sell": {"enabled": False},
}

BT = {
    "initial_capital": 1000000.0,
    "commission": 0.0003,
    "slippage": 0.001,
    "period": os.environ.get("PERIOD", "1d"),
    "degrade_5m": True,
    "position_sizing": {"max_positions": 999, "min_buy_amount": 2000.0,
                        "max_buy_amount": 20000.0, "lot_size": 100, "min_lots": 1},
}

SIG_FILES = ["data/baseline/MMT_signals_2015_2023.parquet",
             "data/baseline/MMT_signals_baseline.parquet"]


def stop_variant(name):
    st = copy.deepcopy(BASE_STOP)
    if name == "base":
        return st
    if name == "cost12":
        st["cost_stop"] = {"enabled": True, "threshold": -0.12}
    elif name == "time20":
        st["time_stop"] = {"enabled": True, "max_hold_days": 20}
    elif name == "trail15_3":
        st["trailing_stop"] = {"enabled": True, "activation": 0.15, "drawdown": 0.03}
    elif name == "trail10_2":
        st["trailing_stop"] = {"enabled": True, "activation": 0.10, "drawdown": 0.02}
    elif name == "trail6_1":
        st["trailing_stop"] = {"enabled": True, "activation": 0.06, "drawdown": 0.01}
    return st


def apply_variant(feat, variant):
    m = pd.Series(True, index=feat.index)
    if variant == "base":
        pass
    elif variant == "g23":
        m = (feat["ret5"] <= 0.15) & (feat["vol_ratio"] <= 3.0)
    elif variant == "trend":
        m = (feat["above_ma60"] == 1) | (feat["ma20_gt_ma60"] == 1)
    elif variant == "atrlow":
        m = feat["atr14_pct"] <= 0.035
    elif variant == "volconfirm":
        m = (feat["vol_ratio"] >= 1.5) & (feat["body"] > 0)
    elif variant == "bias":
        m = feat["bias20"] <= 0.10
    elif variant == "combo":
        m = ((feat["ret5"] <= 0.15) & (feat["vol_ratio"] <= 3.0)
             & (feat["atr14_pct"] <= 0.035))
    else:
        raise ValueError(variant)
    return feat[m]


def load_signals(start, end):
    dfs = []
    for f in SIG_FILES:
        df = pd.read_parquet(f)
        df["select_date"] = pd.to_datetime(df["select_date"])
        dfs.append(df)
    sel = pd.concat(dfs, ignore_index=True).drop_duplicates(["stock_code", "select_date"])
    ds = sel["select_date"].dt.strftime("%Y%m%d")
    return sel[(ds >= start) & (ds <= end)].sort_values(
        ["select_date", "stock_code"]).reset_index(drop=True)


def main():
    variant, stop_name, start, end, tag = sys.argv[1:6]
    sel = load_signals(start, end)
    print(f"[INFO] 信号 {len(sel)} 条 (原始)", flush=True)
    if variant != "base":
        feat = load_features(sel)
        sel = apply_variant(feat, variant)[["stock_code", "select_date"]]
    print(f"[INFO] {variant}/{stop_name} {start}~{end} 信号 {len(sel)}", flush=True)
    if not len(sel):
        print(f"[DONE] {tag} 无信号", flush=True)
        return
    t0 = time.time()
    result = BacktestEngine(BT).run(selections=sel, start_time=start, end_time=end,
                                    stop_config=stop_variant(stop_name))
    m = result.get("metrics") or {}
    print(f"[DONE] {tag} elapsed={time.time()-t0:.0f}s "
          f"cum={m.get('cumulative_return',0)*100:.2f}% ann={m.get('annualized_return',0)*100:.2f}% "
          f"dd={m.get('max_drawdown',0)*100:.2f}% sharpe={m.get('sharpe_ratio',0):.2f} "
          f"trades={m.get('total_trades')} wr={m.get('win_rate',0)*100:.1f}% "
          f"pf={m.get('profit_factor',0):.2f}", flush=True)
    os.makedirs("research/mmt/results", exist_ok=True)
    with open(f"research/mmt/results/{tag}.json", "w", encoding="utf-8") as f:
        json.dump({"variant": variant, "stop": stop_name, "range": [start, end],
                   "period": BT["period"], "metrics": m}, f, ensure_ascii=False, default=str, indent=1)
    if hasattr(result.get("trades"), "to_parquet"):
        result["trades"].to_parquet(f"research/mmt/results/{tag}_trades.parquet")
    if hasattr(result.get("equity_curve"), "to_parquet"):
        result["equity_curve"].to_parquet(f"research/mmt/results/{tag}_equity.parquet")


if __name__ == "__main__":
    main()
