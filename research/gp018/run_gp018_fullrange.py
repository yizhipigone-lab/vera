# -*- coding: utf-8 -*-
"""GUPIAO_018 全区间(2012-2026)年化复核 — 给终止逻辑②的客观天花板视角

探测结论(已验证数据无缺): IS(2012-2018)最高年化13.78% << 30%目标; IS/OOS是防过拟合框架,
但用户原话"2012至今年化30%"字面是全区间。本脚本跑最优候选的【全区间连续资金曲线】
(非IS/OOS简单加权), 给出策略客观天花板 + 逐年收益(看2015股灾/2018熊市/2020-21牛市)。
数据已热(探测刚缓存2012-2026的1d K线)。

口径: 100万 / 佣金0.03% / 滑点0.1% / 1d / 信号日T收盘买入(回测铁律)
用法: python -X utf8 research/gp018/run_gp018_fullrange.py
输出: research/gp018/results/gp018_fullrange.json
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from backtest.engine import BacktestEngine

TAG = "gp018_1d_real_2012_2026"
OUT_DIR = "research/gp018/results"
START, END = "20120101", "20260808"


def make_cfg(hold=8, pos_pct=0.05, exposure=1.0, loss_n=3,
             trail_act=0.035, trail_dd=0.01, cost=-0.06):
    bt = {
        "initial_capital": 1000000.0, "commission": 0.0003, "slippage": 0.001,
        "period": "1d", "degrade_5m": False,
        "max_total_exposure": exposure,
        "loss_streak_halt": {"n": loss_n, "days": 3} if loss_n > 0 else {"n": 0, "days": 0},
        "position_sizing": {"max_positions": 999, "min_buy_amount": 2000.0,
                            "max_buy_amount": 10 ** 9, "max_position_pct": pos_pct,
                            "lot_size": 100, "min_lots": 1},
    }
    sp = {
        "priority": "stop_first",
        "cost_stop": {"enabled": True, "threshold": cost},
        "trailing_stop": {"enabled": True, "activation": trail_act,
                          "drawdown": trail_dd, "confirm": "real"},
        "ladder_tp": {"enabled": False, "levels": []},
        "time_stop": {"enabled": True, "max_hold_days": hold},
        "cond_time_stop": {"enabled": False},
        "first_day": {"enabled": False},
        "formula_sell": {"enabled": False},
    }
    return bt, sp


# 4 个候选: 覆盖平衡~进取谱 (OOS最优平衡3% + IS最优进取5% + 两个中间档)
COMBOS = [
    ("平衡(3%/6d/loss2)",     dict(hold=6, pos_pct=0.03, loss_n=2)),
    ("进取(5%/8d/loss3)",     dict(hold=8, pos_pct=0.05, loss_n=3)),
    ("平衡长持(3%/8d/loss2)", dict(hold=8, pos_pct=0.03, loss_n=2)),
    ("偏进取(4%/7d/loss2)",   dict(hold=7, pos_pct=0.04, loss_n=2)),
]


def run_one(bt, sp, sel):
    eng = BacktestEngine(bt)
    r = eng.run(selections=sel, start_time=START, end_time=END, stop_config=sp)
    m = r.get("metrics") or {}
    out = {
        "ann": round(m.get("annualized_return", 0) * 100, 2),
        "cum": round(m.get("cumulative_return", 0) * 100, 2),
        "sharpe": round(m.get("sharpe_ratio", 0), 3),
        "calmar": round(m.get("calmar_ratio", 0) if isinstance(m.get("calmar_ratio"), (int, float)) else 0, 3),
        "max_dd": round(m.get("max_drawdown", 0) * 100, 2),
        "win": round(m.get("win_rate", 0) * 100, 1),
        "trades": m.get("total_trades"),
    }
    # 逐年收益: 从 equity_curve 取每年末权益算同比 (不依赖 resample 别名)
    eq = r.get("equity_curve")
    if eq is not None and len(eq):
        try:
            s = pd.Series(eq).astype(float)
            s.index = pd.to_datetime(s.index)
            yr_last = s.groupby(s.index.year).last()      # 每自然年最末权益
            yr_ret = yr_last.pct_change().dropna()        # 逐年收益率
            out["by_year"] = {str(int(k)): round(v * 100, 1) for k, v in yr_ret.items()}
        except Exception as e:
            out["by_year_err"] = str(e)
    return out


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    sel = pd.read_parquet(f"{OUT_DIR}/{TAG}_selections.parquet")
    print(f"[INFO] 信号 {len(sel)}  全区间 {START}-{END}  {len(COMBOS)}组合", flush=True)
    rows = []
    t_all = time.time()
    for i, (label, kw) in enumerate(COMBOS, 1):
        bt, sp = make_cfg(**kw)
        t0 = time.time()
        print(f"\n[{i}/{len(COMBOS)}] {label}  {kw}", flush=True)
        try:
            m = run_one(bt, sp, sel)
        except Exception as e:
            print(f"   ERROR {e}", flush=True)
            continue
        print(f"   年化{m['ann']:6.2f}%  累计{m['cum']:8.1f}%  夏普{m['sharpe']:5.2f}  "
              f"Calmar{m['calmar']:5.2f}  回撤{m['max_dd']:6.2f}%  胜率{m['win']:4.1f}%  "
              f"笔{m['trades']}  ({time.time()-t0:.0f}s)", flush=True)
        if m.get("by_year"):
            print("   逐年: " + "  ".join(f"{k}:{v}%" for k, v in m["by_year"].items()), flush=True)
        rows.append({"label": label, **kw, **m, "sec": round(time.time() - t0)})
    with open(f"{OUT_DIR}/gp018_fullrange.json", "w", encoding="utf-8") as f:
        json.dump({"range": [START, END], "rows": rows,
                   "elapsed_min": round((time.time() - t_all) / 60, 1)},
                  f, ensure_ascii=False, default=str, indent=1)
    print(f"\n[OK] 落盘 {OUT_DIR}/gp018_fullrange.json  (总耗时 {(time.time()-t_all)/60:.1f}min)", flush=True)


if __name__ == "__main__":
    main()
