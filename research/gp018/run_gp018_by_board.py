# -*- coding: utf-8 -*-
"""GUPIAO_018 1D REAL 分板块独立回测 + 统一资金池贡献口径。

两个口径:
  独立口径: 每板块各自 100 万、同套出场参数 -> 公平比"板块本身盈利能力" (真年化/回撤/夏普)
  贡献口径: 统一 100 万资金池的全市场 trades 按板块拆 -> 各板块实际赚了多少 (受资金抢占影响)
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from backtest.engine import BacktestEngine
from research.gp018.run_gp018_1d_real_2019 import (
    BT_CFG, STOP_CFG, START, END, TAG,
)
RES = "research/gp018/results"

BOARDS = ["主板", "创业板", "科创板", "北交所"]


def board_of(code):
    n = str(code).split(".")[0]
    p3 = n[:3]
    if p3 in ("600", "601", "603", "605", "000", "001", "002", "003"):
        return "主板"
    if p3 in ("300", "301", "302"):
        return "创业板"
    if p3 in ("688", "689"):
        return "科创板"
    if n[0] in ("4", "8") or p3 == "920":
        return "北交所"
    return "其他"


def main():
    sel = pd.read_parquet(f"{RES}/{TAG}_selections.parquet")
    sel["board"] = sel["stock_code"].apply(board_of)
    print(f"[INFO] 选股信号板块分布:\n{sel['board'].value_counts().to_string()}\n", flush=True)

    out = {}
    for b in BOARDS:
        sub = sel[sel["board"] == b]
        n_sig, n_stk = len(sub), sub["stock_code"].nunique()
        if n_sig == 0:
            out[b] = {"n_signals": 0, "n_stocks": 0}
            print(f"[{b}] 本池无信号, 跳过", flush=True)
            continue
        t0 = time.time()
        eng = BacktestEngine(BT_CFG)
        r = eng.run(
            selections=sub[["stock_code", "select_date", "formula_name"]],
            start_time=START, end_time=END, stop_config=STOP_CFG,
        )
        m = r.get("metrics") or {}
        trades = r.get("trades")
        n_tr = 0 if trades is None else len(trades)
        out[b] = {"n_signals": int(n_sig), "n_stocks": int(n_stk),
                  "n_trades": int(n_tr), "metrics": m}
        if hasattr(trades, "to_parquet"):
            trades.to_parquet(f"{RES}/{TAG}_{b}_trades.parquet")
        print(f"[{b}] 信号{n_sig} 股票{n_stk} 笔数{n_tr} | "
              f"累计{m.get('cumulative_return',0)*100:7.1f}% "
              f"年化{m.get('annualized_return',0)*100:6.2f}% "
              f"夏普{m.get('sharpe_ratio',0):5.2f} "
              f"Calmar{m.get('calmar_ratio',0):5.2f} "
              f"回撤{m.get('max_drawdown',0)*100:6.1f}% "
              f"胜率{m.get('win_rate',0)*100:5.1f}% "
              f"盈亏比{m.get('profit_loss_ratio',0):4.2f} "
              f"| {time.time()-t0:.0f}s", flush=True)

    # ===== 贡献口径: 统一资金池全市场 trades 按板块拆 =====
    print("\n" + "=" * 64, flush=True)
    print("[贡献口径 — 统一 100 万资金池里各板块实际贡献]", flush=True)
    tr = pd.read_parquet(f"{RES}/{TAG}_trades.parquet")
    tr["board"] = tr["stock_code"].apply(board_of)
    tot_pnl = tr["pnl"].sum()
    contrib = tr.groupby("board").agg(
        n_trades=("pnl", "size"),
        sum_pnl_wan=("pnl", lambda x: x.sum() / 1e4),
        avg_ret=("return", "mean"),
        win_rate=("return", lambda x: (x > 0).mean()),
        avg_hold=("hold_days", "mean"),
    )
    contrib["盈亏占比%"] = (contrib["sum_pnl_wan"] / (tot_pnl / 1e4) * 100).round(1)
    contrib["笔数占比%"] = (contrib["n_trades"] / len(tr) * 100).round(1)
    print(contrib.round(4).to_string(), flush=True)
    out["_contrib"] = contrib.reset_index().to_dict(orient="records")
    out["_total_pnl_wan"] = round(tot_pnl / 1e4, 1)

    with open(f"{RES}/{TAG}_by_board.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, default=str, indent=1)
    print(f"\n[OK] 落盘 {RES}/{TAG}_by_board.json + 各板块 _trades.parquet", flush=True)


if __name__ == "__main__":
    main()
