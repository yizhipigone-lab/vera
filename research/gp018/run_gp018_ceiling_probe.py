# -*- coding: utf-8 -*-
"""GUPIAO_018 参数寻优 第0步: 年化天花板探测

用户目标年化≥30%, IS=2012-2018, OOS=2019-2026 (用户拍板一刀切)。
寻优策略=先探天花板: 先跑 8 个激进组合的 IS+OOS, 看年化最高能到哪:
  · 远低于 30% → 如实报客观天花板 (终止逻辑②), 省 20h 贝叶斯
  · 接近 30%  → 进贝叶斯精修 (run_gp018_bayes_opt.py)

7 变量全覆盖(本探测先扫主杠杆的激进谱, 定位天花板区):
  单票占比 / 仓位上限 / 连亏次数 / 持有天数 / 移动激活 / 移动回撤 / 硬止损
诊断结论: 单票占比=回撤主杠杆(越高年化越高但回撤爆), 连亏=压舱石, 仓位上限帮倒忙。
故探测偏激进(高占比/长持有/松连亏)找上限, 含 2 个对照(平衡/进取)。

口径: 100万 / 佣金0.03% / 滑点0.1% / 1d / 信号日T收盘买入(回测铁律)
用法: python -X utf8 research/gp018/run_gp018_ceiling_probe.py
输出: research/gp018/results/gp018_ceiling_probe.json
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
IS_START, IS_END = "20120101", "20181231"
OOS_START, OOS_END = "20190101", "20260808"
TARGET_ANN = 30.0


def make_cfg(hold=8, pos_pct=0.05, exposure=1.0, loss_n=3,
             trail_act=0.035, trail_dd=0.01, cost=-0.06):
    """7 变量完整配置生成。exposure=1.0 表示不限仓位(诊断: 不限优于 70%)。"""
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


# 探测组合: 激进谱找年化上限 + 2 对照
PROBES = [
    ("平衡(对照)",     dict(hold=6,  pos_pct=0.03, loss_n=2)),
    ("进取原型(对照)", dict(hold=8,  pos_pct=0.05, loss_n=3)),
    ("高占比8%",       dict(hold=8,  pos_pct=0.08, loss_n=3)),
    ("高占比12%",      dict(hold=10, pos_pct=0.12, loss_n=5)),
    ("极限15%",        dict(hold=12, pos_pct=0.15, loss_n=0)),
    ("长持12天",       dict(hold=12, pos_pct=0.05, loss_n=3)),
    ("松止8%+5%2%",    dict(hold=10, pos_pct=0.08, loss_n=5, trail_act=0.05, trail_dd=0.02, cost=-0.08)),
    ("紧移10%关连亏",  dict(hold=8,  pos_pct=0.10, loss_n=0, trail_act=0.03, trail_dd=0.01)),
]


def run_one(bt, sp, sel, start, end):
    eng = BacktestEngine(bt)
    r = eng.run(selections=sel, start_time=start, end_time=end, stop_config=sp)
    m = r.get("metrics") or {}
    return {
        "ann": round(m.get("annualized_return", 0) * 100, 2),
        "sharpe": round(m.get("sharpe_ratio", 0), 3),
        "calmar": round(m.get("calmar_ratio", 0) if isinstance(m.get("calmar_ratio"), (int, float)) else 0, 3),
        "max_dd": round(m.get("max_drawdown", 0) * 100, 2),
        "win": round(m.get("win_rate", 0) * 100, 1),
        "trades": m.get("total_trades"),
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    sel_path = f"{OUT_DIR}/{TAG}_selections.parquet"
    if not os.path.exists(sel_path):
        print(f"[FAIL] 缺 2012 选股缓存 {sel_path}, 先跑 run_gp018_select_2012.py", flush=True)
        return
    sel = pd.read_parquet(sel_path)
    print(f"[INFO] 信号 {len(sel)}  探测 {len(PROBES)} 组合 × (IS+OOS)  目标年化≥{TARGET_ANN}%", flush=True)

    rows = []
    t_all = time.time()
    for i, (label, kw) in enumerate(PROBES, 1):
        bt, sp = make_cfg(**kw)
        t0 = time.time()
        print(f"\n[{i}/{len(PROBES)}] {label}  {kw}", flush=True)
        try:
            m_is = run_one(bt, sp, sel, IS_START, IS_END)
            m_oos = run_one(bt, sp, sel, OOS_START, OOS_END)
        except Exception as e:
            print(f"   ERROR {e}", flush=True)
            continue
        dt = time.time() - t0
        decay = round(m_is["ann"] - m_oos["ann"], 2)
        print(f"   IS  年化{m_is['ann']:6.2f}%  夏普{m_is['sharpe']:5.2f}  Calmar{m_is['calmar']:5.2f}  "
              f"回撤{m_is['max_dd']:6.2f}%  笔{m_is['trades']}", flush=True)
        print(f"   OOS 年化{m_oos['ann']:6.2f}%  夏普{m_oos['sharpe']:5.2f}  Calmar{m_oos['calmar']:5.2f}  "
              f"回撤{m_oos['max_dd']:6.2f}%  笔{m_oos['trades']}  衰减{decay:+.2f}pp  ({dt:.0f}s)", flush=True)
        rows.append({"label": label, **kw, "is": m_is, "oos": m_oos,
                     "decay_pp": decay, "sec": round(dt)})

    with open(f"{OUT_DIR}/gp018_ceiling_probe.json", "w", encoding="utf-8") as f:
        json.dump({"is_range": [IS_START, IS_END], "oos_range": [OOS_START, OOS_END],
                   "target_ann": TARGET_ANN, "rows": rows,
                   "elapsed_min": round((time.time() - t_all) / 60, 1)},
                  f, ensure_ascii=False, default=str, indent=1)

    # 天花板判定
    print(f"\n{'='*64}", flush=True)
    if rows:
        best_is = max(rows, key=lambda r: r["is"]["ann"])
        best_oos = max(rows, key=lambda r: r["oos"]["ann"])
        print(f"[天花板] IS最高年化={best_is['is']['ann']}% ({best_is['label']})", flush=True)
        print(f"         OOS最高年化={best_oos['oos']['ann']}% ({best_oos['label']})", flush=True)
        if best_is["is"]["ann"] >= TARGET_ANN:
            print(f"[判定] IS 达标{TARGET_ANN}%! 进贝叶斯精修抗过拟合", flush=True)
        else:
            print(f"[判定] IS 最高{best_is['is']['ann']}% 未达{TARGET_ANN}%目标", flush=True)
    print(f"[OK] 落盘 {OUT_DIR}/gp018_ceiling_probe.json  (总耗时 {(time.time()-t_all)/60:.1f}min)", flush=True)


if __name__ == "__main__":
    main()
