"""GUPIAO_018 出场参数寻优 (真5m, 分阶段剪枝 + 断点续跑 + CSV汇总)。

用法:
  python -X utf8 research/gp018/exit_opt.py stage1 <worker_idx> <n_workers>
  python -X utf8 research/gp018/exit_opt.py stage2 <worker_idx> <n_workers>   (依赖 stage1 结果)
  python -X utf8 research/gp018/exit_opt.py report
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from backtest.engine import BacktestEngine

RESULTS = "research/gp018/exitopt"
CSV = os.path.join(RESULTS, "grid.csv")
WINDOW = ("20210101", "20231231")   # 调参窗口

BT = {
    "initial_capital": 1000000.0,
    "commission": 0.0003, "slippage": 0.001,
    "period": "5m", "degrade_5m": True,
    "position_sizing": {"max_positions": 999, "min_buy_amount": 2000.0,
                        "max_buy_amount": 20000.0, "lot_size": 100, "min_lots": 1},
}


def mk_stop(cost, trail, ladder, days):
    return {
        "priority": "stop_first",
        "cost_stop": {"enabled": cost is not None, "threshold": cost if cost is not None else -0.06},
        "trailing_stop": ({"enabled": True, "activation": trail[0], "drawdown": trail[1]}
                          if trail else {"enabled": False}),
        "ladder_tp": ({"enabled": True, "levels": [{"profit": p, "sell_ratio": r} for p, r in ladder]}
                      if ladder else {"enabled": False, "levels": []}),
        "time_stop": {"enabled": True, "max_hold_days": days},
        "cond_time_stop": {"enabled": False},
        "first_day": {"enabled": False},
        "formula_sell": {"enabled": False},
    }


COSTS = {"c6": -0.06, "c8": -0.08, "c12": -0.12, "coff": None}
TRAILS = {"toff": None, "t35_1": (0.035, 0.01), "t6_2": (0.06, 0.02), "t8_3": (0.08, 0.03)}
LADDERS = {
    "loff": None,
    "l5_10": [(0.05, 0.50), (0.10, 0.50)],
    "l6_15": [(0.06, 0.30), (0.15, 0.30)],
    "l8": [(0.08, 1.0)],
}
TIMES = {"d12": 12, "d20": 20}


def stage1_combos():
    out = []
    for ck, cv in COSTS.items():
        for tk, tv in TRAILS.items():
            out.append((f"s1_{ck}_{tk}_loff_d12", mk_stop(cv, tv, None, 12)))
    return out


def stage2_combos(top3):
    out = []
    for ck, tk in top3:
        for lk, lv in LADDERS.items():
            for dk, dv in TIMES.items():
                out.append((f"s2_{ck}_{tk}_{lk}_{dk}", mk_stop(COSTS[ck], TRAILS[tk], lv, dv)))
    return out


def load_results():
    if os.path.exists(CSV):
        return pd.read_csv(CSV)
    return pd.DataFrame(columns=["combo", "cum", "ann", "dd", "sharpe", "trades", "wr", "pf", "elapsed"])


def append_result(row):
    df = load_results()
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True).drop_duplicates("combo", keep="last")
    os.makedirs(RESULTS, exist_ok=True)
    df.to_csv(CSV, index=False)


def run_combos(combos, widx, nw):
    sel = pd.read_parquet("research/gp018/results/server_gp018_2005_2026.parquet")
    sel["select_date"] = pd.to_datetime(sel["select_date"])
    ds = sel["select_date"].dt.strftime("%Y%m%d")
    sel = sel[(ds >= WINDOW[0]) & (ds <= WINDOW[1])]
    done = set(load_results()["combo"]) if os.path.exists(CSV) else set()
    mine = [c for i, c in enumerate(combos) if i % nw == widx and c[0] not in done]
    print(f"[INFO] worker {widx}/{nw}: {len(mine)} 组合待跑", flush=True)
    for name, stop in mine:
        t0 = time.time()
        try:
            r = BacktestEngine(BT).run(selections=sel, start_time=WINDOW[0], end_time=WINDOW[1],
                                       stop_config=stop)
            m = r.get("metrics") or {}
            row = {"combo": name, "cum": round(m.get("cumulative_return", 0), 4),
                   "ann": round(m.get("annualized_return", 0), 4),
                   "dd": round(m.get("max_drawdown", 0), 4),
                   "sharpe": round(m.get("sharpe_ratio", 0), 2),
                   "trades": m.get("total_trades"), "wr": round(m.get("win_rate", 0), 4),
                   "pf": round(m.get("profit_factor", 0), 2), "elapsed": round(time.time() - t0)}
            append_result(row)
            print(f"[DONE] {name} ann={row['ann']*100:.1f}% dd={row['dd']*100:.1f}% "
                  f"trades={row['trades']}", flush=True)
        except Exception as e:
            print(f"[FAIL] {name}: {type(e).__name__} {str(e)[:100]}", flush=True)
        del r


def report():
    df = load_results().sort_values("ann", ascending=False)
    print(df.to_string(index=False))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "report":
        report()
    elif cmd == "stage1":
        run_combos(stage1_combos(), int(sys.argv[2]), int(sys.argv[3]))
    elif cmd == "stage2":
        df = load_results()
        s1 = df[df.combo.str.startswith("s1_")].copy()
        s1 = s1.sort_values(["ann"], ascending=False)
        top3 = []
        for c in s1["combo"].head(3):
            parts = c.split("_")
            ck = parts[1]
            tk = "_".join(parts[2:-2])  # 名字里可能含下划线(如 t35_1), 掐头去尾取中段
            top3.append((ck, tk))
        print(f"[INFO] stage1 前3骨架: {top3}", flush=True)
        run_combos(stage2_combos(top3), int(sys.argv[2]), int(sys.argv[3]))
