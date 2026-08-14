# -*- coding: utf-8 -*-
"""GUPIAO_018  2026 上半年(1.1-7.31)复现对照

目的: 用户前端 7-01~8-03 那次 -18.23%, 想用 1.1-7.31 整段复现验证。
关键发现: 前端单票约10万/只(max_buy_amount=100000), 不是报告的2万固定。
两版对照:
  A 复现版: 单票10万 + 6天退出 (前端真实口径, 除政策层外)
  B 报告基准: 单票2万 + 12天退出 (报告口径, 分散)
其余同: GUPIAO_018/全A非ST/1d/信号日T收盘/-6%/移动3.5%+1%REAL/100万/连亏关
政策层(P1/P2/P3/AVOID): 脚本不涉及, 两版都不开 — 用户前端复现时需手动关政策层才能对照

用法: python -X utf8 research/gp018/run_gp018_2026h1_repro.py
输出: research/gp018/results/gp018_2026h1_repro.json
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from backtest.engine import BacktestEngine
from selection.selector import StockSelector

OUT = "research/gp018/results"
START, END = "20260101", "20260731"


def make_cfg(max_buy=100000.0, hold=6):
    """单票上限 max_buy / 持有天数 hold 可调, 其余固定"""
    bt = {"initial_capital": 1000000.0, "commission": 0.0003, "slippage": 0.001,
          "period": "1d", "degrade_5m": False, "max_total_exposure": 1.0,
          "loss_streak_halt": {"n": 0, "days": 0},  # 关连亏冷却(报告口径)
          "position_sizing": {"max_positions": 999, "min_buy_amount": 2000.0,
                              "max_buy_amount": max_buy,  # 单票上限(10万=前端/2万=报告)
                              "max_position_pct": 1.0,   # 不用占比限制, 让 max_buy_amount 管
                              "lot_size": 100, "min_lots": 1}}
    sp = {"priority": "stop_first",
          "cost_stop": {"enabled": True, "threshold": -0.06},
          "trailing_stop": {"enabled": True, "activation": 0.035, "drawdown": 0.01, "confirm": "real"},
          "ladder_tp": {"enabled": False, "levels": []},
          "time_stop": {"enabled": True, "max_hold_days": hold},
          "cond_time_stop": {"enabled": False}, "first_day": {"enabled": False},
          "formula_sell": {"enabled": False}}
    return bt, sp


def select():
    sel_cfg = {"formula_name": "GUPIAO_018", "formula_arg": "",
               "universe": {"type": "hs_a", "exclude_st": True, "exclude_quit": True, "sectors": []},
               "period": "1d", "dividend_type": 1}
    return StockSelector(sel_cfg).run(start_time=START, end_time=END)


def run_one(bt, sp, sel):
    r = BacktestEngine(bt).run(selections=sel, start_time=START, end_time=END, stop_config=sp)
    m = r.get("metrics") or {}
    out = {"ann": round(m.get("annualized_return", 0) * 100, 2),
           "cum": round(m.get("cumulative_return", 0) * 100, 2),
           "sharpe": round(m.get("sharpe_ratio", 0), 3),
           "calmar": round(m.get("calmar_ratio", 0) if isinstance(m.get("calmar_ratio"), (int, float)) else 0, 3),
           "max_dd": round(m.get("max_drawdown", 0) * 100, 2),
           "win": round(m.get("win_rate", 0) * 100, 1),
           "trades": m.get("total_trades"),
           "total_pnl": round(m.get("total_pnl", 0), 4)}
    # 逐月末权益收益(权益曲线口径, 对应前端 cumulative_return)
    eq = r.get("equity_curve")
    if eq is not None and len(eq):
        try:
            cols_low = [str(c).lower() for c in eq.columns] if hasattr(eq, "columns") else []
            if "date" in cols_low and "equity" in cols_low:
                s = eq.set_index("date")["equity"].astype(float)
            elif hasattr(eq, "iloc") and eq.ndim == 2:
                s = eq.iloc[:, 1].astype(float)
                s.index = eq.iloc[:, 0]
            else:
                s = pd.Series(eq).astype(float)
            s.index = pd.to_datetime(s.index)
            mon_last = s.groupby(s.index.to_period("M")).last()
            mon_ret = mon_last.pct_change().dropna()
            # 首月相对期初
            first_eq = s.iloc[0]
            mon_ret.iloc[0] = mon_last.iloc[0] / 1_000_000 - 1  # 首月相对100万初始
            out["by_month"] = {str(k): round(v * 100, 1) for k, v in mon_ret.items()}
        except Exception as e:
            out["by_month_err"] = str(e)
    return out


COMBOS = [
    ("A_复现版(10万/6天)", dict(max_buy=100000.0, hold=6)),
    ("B_报告基准(2万/12天)", dict(max_buy=20000.0, hold=12)),
]


def main():
    os.makedirs(OUT, exist_ok=True)
    t_all = time.time()
    print(f"[INFO] GUPIAO_018  {START}-{END}  全A非ST  1d  信号日T收盘  100万", flush=True)
    print(f"[INFO] 两版对照: A=单票10万/6天(前端口径)  B=单票2万/12天(报告口径)", flush=True)
    print(f"[INFO] 政策层(P1/P2/P3/AVOID): 两版都不开 — 前端复现需手动关政策层", flush=True)
    sel = select()
    print(f"[INFO] 信号 {len(sel)} 条  (选股 {time.time()-t_all:.0f}s)\n", flush=True)
    rows = []
    for label, kw in COMBOS:
        bt, sp = make_cfg(**kw)
        t0 = time.time()
        m = run_one(bt, sp, sel)
        print(f"[{label}]", flush=True)
        print(f"  累计{m['cum']:7.2f}%  年化{m['ann']:6.2f}%  夏普{m['sharpe']:5.2f}  "
              f"Calmar{m['calmar']:5.2f}  回撤{m['max_dd']:6.2f}%  胜率{m['win']:4.1f}%  "
              f"笔{m['trades']}  总盈亏{m['total_pnl']}万  ({time.time()-t0:.0f}s)", flush=True)
        if m.get("by_month"):
            print("  逐月: " + "  ".join(f"{k}:{v}%" for k, v in m["by_month"].items()), flush=True)
        rows.append({"label": label, "max_buy": kw["max_buy"], "hold": kw["hold"], **m})
        print(flush=True)
    with open(f"{OUT}/gp018_2026h1_repro.json", "w", encoding="utf-8") as f:
        json.dump({"range": [START, END], "rows": rows,
                   "elapsed_min": round((time.time() - t_all) / 60, 1)},
                  f, ensure_ascii=False, default=str, indent=1)
    print(f"[OK] 落盘 {OUT}/gp018_2026h1_repro.json  (总 {time.time()-t_all:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
