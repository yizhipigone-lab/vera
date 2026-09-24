# -*- coding: utf-8 -*-
"""GUPIAO_018 换池子天花板对比 (2026-08-08 用户任务: 换池子找30%天花板)

池子: 全A(hs_a基准) / 创业板 / 沪深300 / 中证500 / 中证1000
口径: 平衡档(单票3% / 6天退出 / 连亏2次停3天 / 仓位不限) — 与参数寻优报告同口径
段:   IS(2012-2018) / OOS(2019-2026) / 全区间(2012-2026)
买入: 信号日 T 收盘价(回测铁律, 非 T+1 开盘)

注:
- 科创板(2019.7开)/北交所(2021.11开)上市晚, 无法做 2012-2018 IS/OOS, 已排除
- 中证1000 从 2014 起, IS 段实际代表 2014-2018
- 沪深300 含高价股(茅台等), 单票3%(3万)买不起一手会被 engine 跳过, 成交笔数可能偏低
- L2 按日信号缓存自动按池子hash隔离, 全A已缓存, 其余池子首次计算后续复用

用法: python -X utf8 research/gp018/run_gp018_by_universe.py
输出: research/gp018/results/gp018_by_universe.json
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
IS_START, IS_END = "20120101", "20181231"
OOS_START, OOS_END = "20190101", "20260808"
FULL_START, FULL_END = "20120101", "20260808"

# 5 池子: 全A基准 + 用户选的4个
UNIVERSES = [
    ("全A基准(hs_a)", "hs_a"),
    ("创业板", "chuangyeban"),
    ("沪深300", "hs300"),
    ("中证500", "zz500"),
    ("中证1000", "zz1000"),
]


def make_cfg():
    """平衡档 — 与寻优报告同口径 (run_gp018_fullrange.py 的平衡档)"""
    bt = {"initial_capital": 1000000.0, "commission": 0.0003, "slippage": 0.001,
          "period": "1d", "degrade_5m": False, "max_total_exposure": 1.0,
          "loss_streak_halt": {"n": 2, "days": 3},
          "position_sizing": {"max_positions": 999, "min_buy_amount": 2000.0,
                              "max_buy_amount": 10 ** 9, "max_position_pct": 0.03,
                              "lot_size": 100, "min_lots": 1}}
    sp = {"priority": "stop_first",
          "cost_stop": {"enabled": True, "threshold": -0.06},
          "trailing_stop": {"enabled": True, "activation": 0.035, "drawdown": 0.01, "confirm": "real"},
          "ladder_tp": {"enabled": False, "levels": []},
          "time_stop": {"enabled": True, "max_hold_days": 6},
          "cond_time_stop": {"enabled": False},
          "first_day": {"enabled": False},
          "formula_sell": {"enabled": False}}
    return bt, sp


def select(utype):
    """按池子选股 2012-2026 (selector 内部 L2 缓存按池子hash隔离)"""
    sel_cfg = {"formula_name": "GUPIAO_018", "formula_arg": "",
               "universe": {"type": utype, "exclude_st": True, "exclude_quit": True, "sectors": []},
               "period": "1d", "dividend_type": 1}
    return StockSelector(sel_cfg).run(start_time=FULL_START, end_time=FULL_END)


def run_seg(bt, sp, sel, start, end):
    """单段回测, 返回主指标 dict"""
    r = BacktestEngine(bt).run(selections=sel, start_time=start, end_time=end, stop_config=sp)
    m = r.get("metrics") or {}
    calmar = m.get("calmar_ratio", 0)
    return {"ann": round(m.get("annualized_return", 0) * 100, 2),
            "sharpe": round(m.get("sharpe_ratio", 0), 3),
            "calmar": round(calmar if isinstance(calmar, (int, float)) else 0, 3),
            "max_dd": round(m.get("max_drawdown", 0) * 100, 2),
            "win": round(m.get("win_rate", 0) * 100, 1),
            "trades": m.get("total_trades")}


def main():
    os.makedirs(OUT, exist_ok=True)
    bt, sp = make_cfg()
    rows, t_all = [], time.time()
    print(f"[INFO] GUPIAO_018 换池子对比  平衡档(3%/6d/loss2)  {len(UNIVERSES)}池子 × (IS+OOS+全段)", flush=True)
    for label, utype in UNIVERSES:
        t0 = time.time()
        print(f"\n{'=' * 60}\n[{label}] universe={utype}", flush=True)
        try:
            sel = select(utype)
        except Exception as e:
            print(f"  选股失败: {type(e).__name__}: {e}", flush=True)
            rows.append({"label": label, "utype": utype, "error": str(e)[:300]})
            continue
        n_sig = int(len(sel)) if sel is not None else 0
        print(f"  信号 {n_sig} 条  (选股 {time.time() - t0:.0f}s)", flush=True)
        if n_sig == 0:
            rows.append({"label": label, "utype": utype, "n_signals": 0})
            continue
        try:
            is_m = run_seg(bt, sp, sel, IS_START, IS_END)
            oos_m = run_seg(bt, sp, sel, OOS_START, OOS_END)
            full_m = run_seg(bt, sp, sel, FULL_START, FULL_END)
        except Exception as e:
            print(f"  回测失败: {type(e).__name__}: {e}", flush=True)
            rows.append({"label": label, "utype": utype, "n_signals": n_sig, "error": str(e)[:300]})
            continue
        print(f"  IS  年化{is_m['ann']:6.2f}%  夏普{is_m['sharpe']:5.2f}  Calmar{is_m['calmar']:5.2f}  "
              f"回撤{is_m['max_dd']:6.1f}%  胜率{is_m['win']:4.1f}%  笔{is_m['trades']}", flush=True)
        print(f"  OOS 年化{oos_m['ann']:6.2f}%  夏普{oos_m['sharpe']:5.2f}  Calmar{oos_m['calmar']:5.2f}  "
              f"回撤{oos_m['max_dd']:6.1f}%  胜率{oos_m['win']:4.1f}%  笔{oos_m['trades']}", flush=True)
        print(f"  全段年化{full_m['ann']:6.2f}%  夏普{full_m['sharpe']:5.2f}  Calmar{full_m['calmar']:5.2f}  "
              f"回撤{full_m['max_dd']:6.1f}%  胜率{full_m['win']:4.1f}%  笔{full_m['trades']}  "
              f"({time.time() - t0:.0f}s)", flush=True)
        rows.append({"label": label, "utype": utype, "n_signals": n_sig,
                     "is": is_m, "oos": oos_m, "full": full_m, "sec": round(time.time() - t0)})

    with open(f"{OUT}/gp018_by_universe.json", "w", encoding="utf-8") as f:
        json.dump({"is_range": [IS_START, IS_END], "oos_range": [OOS_START, OOS_END],
                   "full_range": [FULL_START, FULL_END], "cfg": "平衡(3%/6d/loss2)",
                   "rows": rows, "elapsed_min": round((time.time() - t_all) / 60, 1)},
                  f, ensure_ascii=False, default=str, indent=1)
    print(f"\n[OK] 落盘 {OUT}/gp018_by_universe.json  (总 {(time.time() - t_all) / 60:.1f}min)", flush=True)


if __name__ == "__main__":
    main()
