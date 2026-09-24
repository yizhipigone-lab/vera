"""GUPIAO_018 出场参数寻优 — 2026 窗口专场 (1D, stop_first, real 条件单语义)。

2026-08-07 用户要求: 2026-01-01~2026-08-01, 日线, 止损优先, real 口径。
网格: 硬止损{-10/-12/-15/关} × 阶梯{6·10各30% / 6·15各30% / 关}
      × 移动止盈{3.5%·1% / 6%·1% / 10%·1% / 10%·2%} × 时间12天固定 = 48 组。

用法: python -X utf8 research/gp018/opt_2026.py [worker_idx n_workers]
结果: research/gp018/exitopt/grid_2026.csv (断点续跑, 已完成组合自动跳过)

注意: 2026 仅 7 个月单行情段, 寻优结果过拟合风险高, 仅作参数敏感度参考。
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from backtest.engine import BacktestEngine

RESULTS = "research/gp018/exitopt"
CSV = os.path.join(RESULTS, "grid_2026.csv")
WINDOW = ("20260101", "20260801")

BT = {
    "initial_capital": 1000000.0,
    "commission": 0.0003, "slippage": 0.001,
    "period": "1d", "degrade_5m": True,
    "position_sizing": {"max_positions": 999, "min_buy_amount": 2000.0,
                        "max_buy_amount": 20000.0, "lot_size": 100, "min_lots": 1},
}


def mk_stop(cost, trail, ladder, days):
    return {
        "priority": "stop_first",
        "cost_stop": {"enabled": cost is not None, "threshold": cost if cost is not None else -0.12},
        "trailing_stop": ({"enabled": True, "activation": trail[0], "drawdown": trail[1],
                           "confirm": "real"}
                          if trail else {"enabled": False}),
        "ladder_tp": ({"enabled": True, "levels": [{"profit": p, "sell_ratio": r} for p, r in ladder]}
                      if ladder else {"enabled": False, "levels": []}),
        "time_stop": {"enabled": True, "max_hold_days": days},
        "cond_time_stop": {"enabled": False},
        "first_day": {"enabled": False},
        "formula_sell": {"enabled": False},
    }


COSTS = {"c10": -0.10, "c12": -0.12, "c15": -0.15, "coff": None}
TRAILS = {"t35_1": (0.035, 0.01), "t6_1": (0.06, 0.01),
          "t10_1": (0.10, 0.01), "t10_2": (0.10, 0.02)}
LADDERS = {
    "l610": [(0.06, 0.30), (0.10, 0.30)],
    "l615": [(0.06, 0.30), (0.15, 0.30)],
    "loff": None,
}


def combos():
    out = []
    for ck, cv in COSTS.items():
        for lk, lv in LADDERS.items():
            for tk, tv in TRAILS.items():
                out.append((f"o26_{ck}_{lk}_{tk}_d12", mk_stop(cv, tv, lv, 12)))
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


def main():
    widx = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    nw = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    sel = pd.read_parquet("research/gp018/results/server_gp018_2005_2026.parquet")
    sel["select_date"] = pd.to_datetime(sel["select_date"])
    ds = sel["select_date"].dt.strftime("%Y%m%d")
    sel = sel[(ds >= WINDOW[0]) & (ds <= WINDOW[1])]
    print(f"[INFO] 信号 {len(sel)} 条", flush=True)
    done = set(load_results()["combo"]) if os.path.exists(CSV) else set()
    all_combos = combos()
    mine = [c for i, c in enumerate(all_combos) if i % nw == widx and c[0] not in done]
    print(f"[INFO] worker {widx}/{nw}: {len(mine)}/{len(all_combos)} 组合待跑", flush=True)
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
            print(f"[DONE] {name} cum={row['cum']*100:.1f}% dd={row['dd']*100:.1f}% "
                  f"trades={row['trades']}", flush=True)
        except Exception as e:
            print(f"[FAIL] {name}: {type(e).__name__} {str(e)[:100]}", flush=True)
        del r


if __name__ == "__main__":
    main()
