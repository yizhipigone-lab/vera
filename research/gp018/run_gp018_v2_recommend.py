# -*- coding: utf-8 -*-
"""GUPIAO_018 v2 推荐3档组合实测 (基于逐项还原诊断结论)

诊断结论: 单票占比是回撤主杠杆(越低越稳), 70%仓位上限帮倒忙(关掉更好),
连亏冷却是压舱石(必须留, 2次略优于3), 6-8天退出(8天收益更高)。
本脚本把"好方向"组合成3档实测, 不靠推断。

  极稳(回撤优先): 2万固定 + 仓位不限 + 连亏2停3 + 6天
  平衡(夏普优先): 单票3%   + 仓位不限 + 连亏2停3 + 6天
  进取(年化优先): 单票5%   + 仓位不限 + 连亏3停3 + 8天

用法: python -X utf8 research/gp018/run_gp018_v2_recommend.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from backtest.engine import BacktestEngine
from research.gp018.run_gp018_v2_sensitivity import make_cfg, run_one, START, END, TAG_V1, OUT_DIR

# 3 档推荐组合
RECOMMEND = [
    ("极稳(回撤优先)", make_cfg(hold=6, pos_mode="fixed", exposure=1.0, loss_n=2)),
    ("平衡(夏普优先)", make_cfg(hold=6, pos_mode="pct", pos_pct=0.03, exposure=1.0, loss_n=2)),
    ("进取(年化优先)", make_cfg(hold=8, pos_mode="pct", pos_pct=0.05, exposure=1.0, loss_n=3)),
]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    selections = pd.read_parquet(f"{OUT_DIR}/{TAG_V1}_selections.parquet")
    print(f"[INFO] 复用选股缓存: {len(selections)} 信号\n", flush=True)

    out = []
    t_all = time.time()
    for label, (bt, sp) in RECOMMEND:
        t0 = time.time()
        m = run_one(bt, sp, selections)
        dt = time.time() - t0
        ps = bt["position_sizing"]
        pos_desc = "2万固定" if ps["max_buy_amount"] == 20000 else f"{ps['max_position_pct']*100:.0f}%"
        ls = bt["loss_streak_halt"]
        print(f"【{label}】 年化{m['ann']:6.2f}%  夏普{m['sharpe']:5.2f}  "
              f"Calmar{m['calmar']:5.2f}  回撤{m['max_dd']:6.2f}%  "
              f"胜率{m['win']:4.1f}%  笔{m['trades']}  ({dt:.0f}s)", flush=True)
        print(f"   单票={pos_desc}  仓位上限={bt['max_total_exposure']}  "
              f"连亏n={ls['n']}停{ls['days']}天  持有{sp['time_stop']['max_hold_days']}天", flush=True)
        out.append({"label": label, "bt": bt, "stop": sp, **m, "sec": round(dt)})

    with open(f"{OUT_DIR}/gp018_v2_recommend.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, default=str, indent=1)
    print(f"\n[OK] 落盘 {OUT_DIR}/gp018_v2_recommend.json  (总耗时 {(time.time()-t_all)/60:.1f}min)", flush=True)


if __name__ == "__main__":
    main()
