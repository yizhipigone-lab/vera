"""
航海突破 + C1288 训练集网格搜索 (2019-01-01 ~ 2023-12-31)
"""
import json, os, sys, time, itertools, copy
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from backtest.engine import BacktestEngine
from backtest.stop_config import load_stop_config
from selection.selector import StockSelector

os.makedirs("research/hh_c1288", exist_ok=True)

TRAIN_START, TRAIN_END = "20190101", "20231231"
BT_CFG = {
    "initial_capital": 1000000.0, "commission": 0.0003, "slippage": 0.001,
    "period": "1d", "degrade_5m": True,
    "position_sizing": {"max_positions": 999, "min_buy_amount": 2000.0,
                        "max_buy_amount": 20000.0, "lot_size": 100, "min_lots": 1},
}

BASE_STOP = load_stop_config()

# ── 航海突破 网格 ────────────────────────────────
# 成本止损变体 + 信号冷却
HH_VARIANTS = []
for cs in [-0.06, -0.08, -0.10, -0.12]:
    stop = copy.deepcopy(BASE_STOP)
    stop["cost_stop"]["threshold"] = cs
    HH_VARIANTS.append(("HH_cs%.0f" % (abs(cs)*100), stop, ""))

# ── C1288 网格 ────────────────────────────────────
# N 值 + 趋势过滤 + 位置限制
C1288_VARIANTS = []
for n_val in [3, 5, 7]:
    for max_pos in [50, 100, 200]:
        label = "C1288_n%d_m%d" % (n_val, max_pos)
        stop = copy.deepcopy(BASE_STOP)
        C1288_VARIANTS.append((label, stop, str(n_val), max_pos))

# 合并所有变体
ALL_RUNS = HH_VARIANTS + C1288_VARIANTS


def pull_signals(formula, formula_arg):
    """拉取训练集信号，缓存复用。"""
    tag = f"{formula}_arg{formula_arg}" if formula_arg else formula
    cache_path = f"research/hh_c1288/{tag}_train_selections.parquet"
    if os.path.exists(cache_path):
        return pd.read_parquet(cache_path)
    sel_cfg = {"formula_name": formula, "formula_arg": formula_arg,
               "universe": {"type": "50", "exclude_st": True},
               "period": "1d", "dividend_type": 1}
    s = StockSelector(sel_cfg).run(start_time=TRAIN_START, end_time=TRAIN_END)
    if s is not None and len(s):
        s.to_parquet(cache_path)
    return s


def run_one(label, formula, formula_arg, stop_config, selections, max_pos=None):
    t0 = time.time()
    bt_cfg = copy.deepcopy(BT_CFG)
    if max_pos:
        bt_cfg["position_sizing"]["max_positions"] = max_pos
    engine = BacktestEngine(bt_cfg)
    try:
        result = engine.run(selections=selections,
                            start_time=TRAIN_START, end_time=TRAIN_END,
                            stop_config=stop_config)
    except Exception as e:
        print(f"  [{label}] ERROR: {type(e).__name__}: {str(e)[:80]}", flush=True)
        return None
    m = result.get("metrics") or {}
    elapsed = time.time() - t0
    print(f"  [{label}] ann={m.get('annualized_return',0)*100:.1f}% "
          f"dd={m.get('max_drawdown',0)*100:.1f}% sharpe={m.get('sharpe_ratio',0):.2f} "
          f"wr={m.get('win_rate',0)*100:.1f}% trades={m.get('total_trades')} "
          f"t={elapsed:.0f}s", flush=True)
    return {"label": label, "formula": formula,
            "ann": m.get("annualized_return", 0), "dd": m.get("max_drawdown", 0),
            "sharpe": m.get("sharpe_ratio", 0), "wr": m.get("win_rate", 0),
            "trades": m.get("total_trades"), "cum": m.get("cumulative_return", 0),
            "elapsed": elapsed}


def main():
    # 预拉信号
    print("拉取 航海突破 训练集信号...", flush=True)
    hh_sig = pull_signals("航海突破", "")
    print(f"  信号={len(hh_sig) if hh_sig is not None else 0}", flush=True)

    print("拉取 C1288 训练集信号 (arg=3,5,7)...", flush=True)
    c1288_sigs = {}
    for n in [3, 5, 7]:
        sig = pull_signals("C1288", str(n))
        c1288_sigs[n] = sig
        print(f"  N={n}: {len(sig) if sig is not None else 0} signals", flush=True)

    results = []

    for run_info in ALL_RUNS:
        label, stop, *rest = run_info
        if label.startswith("HH"):
            formula = "航海突破"
            selections = hh_sig
            formula_arg = ""
            max_pos = None
        else:
            formula = "C1288"
            n_val = int(rest[0]) if rest else 3
            max_pos = rest[1] if len(rest) > 1 else None
            selections = c1288_sigs.get(n_val)
            formula_arg = str(n_val)

        if selections is None or not len(selections):
            print(f"  [{label}] 无信号，跳过", flush=True)
            continue

        r = run_one(label, formula, formula_arg, stop, selections, max_pos)
        if r:
            results.append(r)

    # 保存结果
    with open("research/hh_c1288/grid_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)

    # 打印排名
    print("\n=== 训练集排名 ===", flush=True)
    results.sort(key=lambda x: x["ann"], reverse=True)
    for i, r in enumerate(results):
        formula_tag = "HH" if r["formula"] == "航海突破" else "C8"
        print(f"{i+1}. [{formula_tag}] {r['label']}: ann={r['ann']*100:.1f}% "
              f"dd={r['dd']*100:.1f}% sharpe={r['sharpe']:.2f}", flush=True)


if __name__ == "__main__":
    main()
