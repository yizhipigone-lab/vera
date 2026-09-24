# -*- coding: utf-8 -*-
"""GUPIAO_018 逐年收益(平衡+进取, 全区间) — 看行情穿越能力
equity_curve 是 DataFrame(2D), 用 iloc[:,0] 取权益列算逐年。
用法: python -X utf8 research/gp018/run_gp018_yearly.py
输出: research/gp018/results/gp018_yearly.json
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from backtest.engine import BacktestEngine

TAG = "gp018_1d_real_2012_2026"
OUT = "research/gp018/results"
START, END = "20120101", "20260808"


def make_cfg(hold=8, pos_pct=0.05, loss_n=2):
    bt = {"initial_capital": 1000000.0, "commission": 0.0003, "slippage": 0.001,
          "period": "1d", "degrade_5m": False, "max_total_exposure": 1.0,
          "loss_streak_halt": {"n": loss_n, "days": 3},
          "position_sizing": {"max_positions": 999, "min_buy_amount": 2000.0,
                              "max_buy_amount": 10 ** 9, "max_position_pct": pos_pct,
                              "lot_size": 100, "min_lots": 1}}
    sp = {"priority": "stop_first", "cost_stop": {"enabled": True, "threshold": -0.06},
          "trailing_stop": {"enabled": True, "activation": 0.035, "drawdown": 0.01, "confirm": "real"},
          "ladder_tp": {"enabled": False, "levels": []},
          "time_stop": {"enabled": True, "max_hold_days": hold},
          "cond_time_stop": {"enabled": False}, "first_day": {"enabled": False},
          "formula_sell": {"enabled": False}}
    return bt, sp


COMBOS = [
    ("平衡(3%/6d/loss2)", dict(hold=6, pos_pct=0.03, loss_n=2)),
    ("进取(5%/8d/loss3)", dict(hold=8, pos_pct=0.05, loss_n=3)),
]


def main():
    sel = pd.read_parquet(f"{OUT}/{TAG}_selections.parquet")
    rows = []
    for label, kw in COMBOS:
        bt, sp = make_cfg(**kw)
        t0 = time.time()
        res = BacktestEngine(bt).run(selections=sel, start_time=START, end_time=END, stop_config=sp)
        eq = res.get("equity_curve") if hasattr(res, "get") else getattr(res, "equity_curve", None)
        # equity_curve: DataFrame, index=RangeIndex(int), columns=[date, equity, drawdown]
        # → 用 date 列做 index, 取 equity 列
        cols_low = [str(c).lower() for c in eq.columns]
        if "date" in cols_low and "equity" in cols_low:
            s = eq.set_index("date")["equity"].astype(float)
        elif hasattr(eq, "iloc") and eq.ndim == 2:
            s = eq.iloc[:, 1].astype(float)  # 第0列是date, 第1列是equity
        else:
            s = pd.Series(eq).astype(float)
        s.index = pd.to_datetime(s.index)
        yr_last = s.groupby(s.index.year).last()
        yr_ret = yr_last.pct_change().dropna()
        by = {str(int(k)): round(v * 100, 1) for k, v in yr_ret.items()}
        m = res.get("metrics") or {}
        print(f"\n{label}  ({time.time()-t0:.0f}s)", flush=True)
        print(f"  年化{m.get('annualized_return',0)*100:.2f}%  回撤{m.get('max_drawdown',0)*100:.1f}%", flush=True)
        print("  逐年: " + "  ".join(f"{k}:{v}%" for k, v in by.items()), flush=True)
        rows.append({"label": label, **kw, "by_year": by,
                     "ann": round(m.get("annualized_return", 0) * 100, 2)})
    json.dump(rows, open(f"{OUT}/gp018_yearly.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n[OK] 落盘 {OUT}/gp018_yearly.json", flush=True)


if __name__ == "__main__":
    main()
