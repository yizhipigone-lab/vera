"""GUPIAO_018 研究: 基准复现 + 板块池扫描 + 变体回测。

用法:
  python -X utf8 research/gp018/run_gp018.py <start> <end> <tag> <universe1,universe2,...> [stop_variant]
universe: 名称(chuangyeban/zzA500/...) 或 TDX list_type 数字
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from backtest.engine import BacktestEngine
from selection.selector import StockSelector, UNIVERSE_TYPE_MAP

# 基准止盈止损 (20260802_015030 那次, 同 QUANTQQ 旧结构)
BASE_STOP = {
    "priority": "trailing_first",
    "cost_stop": {"enabled": True, "threshold": -0.08},
    "trailing_stop": {"enabled": True, "activation": 0.20, "drawdown": 0.02},
    "ladder_tp": {"enabled": True, "levels": [
        {"profit": 1.00, "sell_ratio": 0.20},
        {"profit": 1.20, "sell_ratio": 0.20},
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
    "degrade_5m": False,
    "position_sizing": {"max_positions": 999, "min_buy_amount": 2000.0,
                        "max_buy_amount": 20000.0, "lot_size": 100, "min_lots": 1},
}


def stop_variant(name):
    import copy
    st = copy.deepcopy(BASE_STOP)
    if name == "base":
        return st
    if name == "stop_first":
        st["priority"] = "stop_first"
        return st
    if name == "fast":      # 当前前端配置 (current.yaml)
        return {
            "priority": "trailing_first",
            "cost_stop": {"enabled": True, "threshold": -0.12},
            "trailing_stop": {"enabled": True, "activation": 0.035, "drawdown": 0.01},
            "ladder_tp": {"enabled": True, "levels": [
                {"profit": 0.06, "sell_ratio": 0.30}, {"profit": 0.15, "sell_ratio": 0.30}]},
            "time_stop": {"enabled": True, "max_hold_days": 20},
            "cond_time_stop": {"enabled": False},
            "first_day": {"enabled": False},
            "formula_sell": {"enabled": False},
        }
    if name == "fast6":     # 中间结构: 移动 6%/2%
        st["cost_stop"] = {"enabled": True, "threshold": -0.12}
        st["trailing_stop"] = {"enabled": True, "activation": 0.06, "drawdown": 0.02}
        st["ladder_tp"] = {"enabled": True, "levels": [
            {"profit": 0.10, "sell_ratio": 0.30}, {"profit": 0.20, "sell_ratio": 0.30}]}
        st["time_stop"] = {"enabled": True, "max_hold_days": 20}
        return st
    if name == "trail15_3":
        st["trailing_stop"] = {"enabled": True, "activation": 0.15, "drawdown": 0.03}
    elif name == "trail10_2":
        st["trailing_stop"] = {"enabled": True, "activation": 0.10, "drawdown": 0.02}
    elif name == "time20":
        st["time_stop"] = {"enabled": True, "max_hold_days": 20}
    elif name == "cost12":
        st["cost_stop"] = {"enabled": True, "threshold": -0.12}
    elif name == "ladder_on":
        st["ladder_tp"] = {"enabled": True, "levels": [
            {"profit": 0.15, "sell_ratio": 0.30}, {"profit": 0.30, "sell_ratio": 0.30}]}
    return st


def resolve_utype(u):
    return UNIVERSE_TYPE_MAP.get(u, u)


def run_one(formula, universe, start, end, tag, stop_name="base"):
    utype = resolve_utype(universe)
    sel_cfg = {"formula_name": formula, "formula_arg": "",
               "universe": {"type": utype, "exclude_st": True},
               "period": "1d", "dividend_type": 1}
    t0 = time.time()
    try:
        selections = StockSelector(sel_cfg).run(start_time=start, end_time=end)
    except Exception as e:
        print(f"[FAIL] {formula}/{universe} 选股异常: {type(e).__name__} {str(e)[:80]}", flush=True)
        return
    n = 0 if selections is None else len(selections)
    print(f"[INFO] {formula}/{universe}(type={utype}) 信号={n}", flush=True)
    if not n:
        print(f"[DONE] {tag}_{universe}_{stop_name} 无信号", flush=True)
        return
    os.makedirs("research/gp018/results", exist_ok=True)
    name = f"{tag}_{universe}_{stop_name}"
    engine = BacktestEngine(BT_CFG)
    try:
        result = engine.run(selections=selections, start_time=start, end_time=end,
                            stop_config=stop_variant(stop_name))
    except Exception as e:
        print(f"[FAIL] {name} 回测异常: {type(e).__name__} {str(e)[:80]}", flush=True)
        return
    m = result.get("metrics") or {}
    print(f"[DONE] {name} elapsed={time.time()-t0:.0f}s "
          f"cum={m.get('cumulative_return',0)*100:.2f}% ann={m.get('annualized_return',0)*100:.2f}% "
          f"dd={m.get('max_drawdown',0)*100:.2f}% sharpe={m.get('sharpe_ratio',0):.2f} "
          f"trades={m.get('total_trades')} wr={m.get('win_rate',0)*100:.1f}% "
          f"pf={m.get('profit_factor',0):.2f}", flush=True)
    with open(f"research/gp018/results/{name}.json", "w", encoding="utf-8") as f:
        json.dump({"formula": formula, "universe": universe, "stop": stop_name,
                   "range": [start, end], "metrics": m}, f, ensure_ascii=False, default=str, indent=1)
    if hasattr(result.get("trades"), "to_parquet"):
        result["trades"].to_parquet(f"research/gp018/results/{name}_trades.parquet")
    if hasattr(result.get("equity_curve"), "to_parquet"):
        result["equity_curve"].to_parquet(f"research/gp018/results/{name}_equity.parquet")
    selections.to_parquet(f"research/gp018/results/{name}_selections.parquet")


if __name__ == "__main__":
    start, end, tag = sys.argv[1], sys.argv[2], sys.argv[3]
    universes = sys.argv[4].split(",")
    stop_name = sys.argv[5] if len(sys.argv) > 5 else "base"
    for u in universes:
        run_one("GUPIAO_018", u, start, end, tag, stop_name)
    print("[ALL DONE]", flush=True)
