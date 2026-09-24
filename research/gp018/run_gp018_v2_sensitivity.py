# -*- coding: utf-8 -*-
"""GUPIAO_018 v2 敏感度分析 — 逐项还原诊断 + 关键维度方向扫描

v2 (6天/5%单票/70%仓位/连亏3停3) 回测发现: 收益微涨但回撤 -12% -> -29%,
夏普 1.50 -> 0.94, Calmar 1.35 -> 0.58, 交易笔数 13055 -> 4586。
本脚本定位: 用户提的 4 项改动各自是帮忙还是帮倒忙。

方法: 以 v2 为基准, 每次【只把一项还原到 v1】其他保持 v2, 看该项的边际影响;
再对每维扫一个方向点看趋势。基准配置去重只跑 1 次。

配置清单 (★=v2基准, ⬅=还原到v1):
  时间退出  : hold=12⬅ / hold=6★ / hold=8 / hold=10
  单票上限  : 2万固定⬅(还原) / 5%★ / 8% / 3%
  总仓位上限: 1.0关⬅ / 0.7★ / 0.8 / 0.6
  连亏冷却  : 关⬅(n=0) / 3★ / 5 / 2

用法: python -X utf8 research/gp018/run_gp018_v2_sensitivity.py
输出: research/gp018/results/gp018_v2_sensitivity.{json,csv}
"""
import copy
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from backtest.engine import BacktestEngine

FORMULA = "GUPIAO_018"
START = "20190101"
END = "20260808"
TAG_V1 = "gp018_1d_real_2019_2026"
OUT_DIR = "research/gp018/results"

# v2 基准 (与 run_gp018_1d_real_2019_v2.py 一致)
BASE_BT = {
    "initial_capital": 1000000.0, "commission": 0.0003, "slippage": 0.001,
    "period": "1d", "degrade_5m": False,
    "max_total_exposure": 0.70,
    "loss_streak_halt": {"n": 3, "days": 3},
    "position_sizing": {"max_positions": 999, "min_buy_amount": 2000.0,
                        "max_buy_amount": 10 ** 9, "max_position_pct": 0.05,
                        "lot_size": 100, "min_lots": 1},
}
BASE_STOP = {
    "priority": "stop_first",
    "cost_stop": {"enabled": True, "threshold": -0.06},
    "trailing_stop": {"enabled": True, "activation": 0.035, "drawdown": 0.01, "confirm": "real"},
    "ladder_tp": {"enabled": False, "levels": []},
    "time_stop": {"enabled": True, "max_hold_days": 6},
    "cond_time_stop": {"enabled": False}, "first_day": {"enabled": False},
    "formula_sell": {"enabled": False},
}


def make_cfg(hold=6, pos_mode="pct", pos_pct=0.05, exposure=0.70, loss_n=3):
    """生成一份配置。pos_mode='fixed' 还原 v1 (2万固定无 pct); 'pct' 用动态占比。"""
    bt = copy.deepcopy(BASE_BT)
    sp = copy.deepcopy(BASE_STOP)
    sp["time_stop"]["max_hold_days"] = hold
    bt["max_total_exposure"] = exposure
    bt["loss_streak_halt"]["n"] = loss_n
    if pos_mode == "fixed":
        bt["position_sizing"]["max_buy_amount"] = 20000.0   # v1 固定 2 万
        bt["position_sizing"]["max_position_pct"] = 1.0      # 关闭动态占比
    else:
        bt["position_sizing"]["max_buy_amount"] = 10 ** 9
        bt["position_sizing"]["max_position_pct"] = pos_pct
    return bt, sp


# (标签, 维度, bt, stop)
def build_grid():
    rows = []
    # —— 基准 (v2) ——
    bt, sp = make_cfg()
    rows.append(("v2基准", "基准", bt, sp))
    # —— 时间退出 ——
    for h, tag in [(12, "hold=12⬅"), (8, "hold=8"), (10, "hold=10")]:
        bt, sp = make_cfg(hold=h)
        rows.append((tag, "时间退出", bt, sp))
    # —— 单票上限 ——
    bt, sp = make_cfg(pos_mode="fixed")
    rows.append(("2万固定⬅", "单票上限", bt, sp))
    for p, tag in [(0.08, "单票8%"), (0.03, "单票3%")]:
        bt, sp = make_cfg(pos_pct=p)
        rows.append((tag, "单票上限", bt, sp))
    # —— 总仓位上限 ——
    for e, tag in [(1.0, "仓位关⬅"), (0.8, "仓位80%"), (0.6, "仓位60%")]:
        bt, sp = make_cfg(exposure=e)
        rows.append((tag, "总仓位上限", bt, sp))
    # —— 连亏冷却 ——
    for n, tag in [(0, "连亏关⬅"), (5, "连亏5次"), (2, "连亏2次")]:
        bt, sp = make_cfg(loss_n=n)
        rows.append((tag, "连亏冷却", bt, sp))
    return rows


def run_one(bt_cfg, stop_cfg, selections):
    engine = BacktestEngine(bt_cfg)
    result = engine.run(selections=selections, start_time=START, end_time=END,
                        stop_config=stop_cfg)
    m = result.get("metrics") or {}
    return {
        "cum": round(m.get("cumulative_return", 0) * 100, 2),
        "ann": round(m.get("annualized_return", 0) * 100, 2),
        "sharpe": round(m.get("sharpe_ratio", 0), 3),
        "calmar": round(m.get("calmar_ratio", 0) if isinstance(m.get("calmar_ratio"), (int, float)) else 0, 3),
        "max_dd": round(m.get("max_drawdown", 0) * 100, 2),
        "win": round(m.get("win_rate", 0) * 100, 1),
        "trades": m.get("total_trades"),
        "pf": round(m.get("profit_factor", 0), 2),
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    sel_path = f"{OUT_DIR}/{TAG_V1}_selections.parquet"
    if not os.path.exists(sel_path):
        print(f"[FAIL] 缺选股缓存 {sel_path}", flush=True)
        return
    selections = pd.read_parquet(sel_path)
    print(f"[INFO] 复用选股缓存: {len(selections)} 信号", flush=True)

    grid = build_grid()
    print(f"[INFO] 共 {len(grid)} 个配置, 预计每配置 ~5min", flush=True)
    rows_out = []
    t_all = time.time()

    for idx, (label, dim, bt, sp) in enumerate(grid, 1):
        t0 = time.time()
        print(f"\n[{idx}/{len(grid)}] {dim} | {label}", flush=True)
        try:
            metrics = run_one(bt, sp, selections)
            ok = True
        except Exception as e:
            metrics = {"error": str(e)}
            ok = False
        dt = time.time() - t0
        if ok:
            print(f"   -> 年化{metrics['ann']:6.2f}%  夏普{metrics['sharpe']:5.2f}  "
                  f"Calmar{metrics['calmar']:5.2f}  回撤{metrics['max_dd']:6.2f}%  "
                  f"胜率{metrics['win']:4.1f}%  笔{metrics['trades']}  "
                  f"({dt:.0f}s)", flush=True)
        else:
            print(f"   -> ERROR {metrics['error']} ({dt:.0f}s)", flush=True)
        rows_out.append({"label": label, "dim": dim, **metrics, "sec": round(dt)})

    df = pd.DataFrame(rows_out)
    df.to_csv(f"{OUT_DIR}/gp018_v2_sensitivity.csv", index=False, encoding="utf-8-sig")
    with open(f"{OUT_DIR}/gp018_v2_sensitivity.json", "w", encoding="utf-8") as f:
        json.dump({"formula": FORMULA, "range": [START, END],
                   "base_bt": BASE_BT, "base_stop": BASE_STOP,
                   "rows": rows_out, "elapsed_sec": round(time.time() - t_all)},
                  f, ensure_ascii=False, default=str, indent=1)
    print(f"\n[OK] 落盘 {OUT_DIR}/gp018_v2_sensitivity.*  (总耗时 {(time.time()-t_all)/60:.1f}min)", flush=True)


if __name__ == "__main__":
    main()
