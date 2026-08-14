# -*- coding: utf-8 -*-
"""GUPIAO_018 | 1D | REAL 移动止盈 | 2019-至今 全A非ST 回测 (v2 — 用户第2轮参数)

用户 2026-08-08 第 2 轮 5 项改动 (其他不变):
  1) 时间退出 12 -> 6 交易日 (time_stop.max_hold_days)
  2) 单票上限: 固定 2 万 -> 不超过【当前总权益 5%】(动态)
     · max_position_pct = 0.05 (新增, 基于 prev_equity)
     · max_buy_amount 抬到 10**9 取消 2 万卡死 (否则 5%=5万 永远撞 2万 上限)
  3) 总持股市值 > 总资产 70% -> 停开新仓 (持仓照常止损止盈)  [max_total_exposure=0.70]
  4) 出场是亏的, 全局连亏 3 次 -> 停开新仓 3 交易日        [loss_streak_halt={n:3,days:3}]
  5) 分析部分加敏感度分析 (另见 run_gp018_v2_sensitivity.py + 报告)

其余同 v1: 硬止损6% stop_first / 阶梯止盈关 / 移动止盈REAL 3.5%激活1%回撤 /
           100万 / 前复权 / 全A非ST(type50) / 1D / 20190101->20260808

选股参数未改 (只改出场/仓位门槛, 不影响公式信号), 故复用 v1 选股缓存省选股。
用法: python -X utf8 research/gp018/run_gp018_1d_real_2019_v2.py
"""
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
TAG_V1 = "gp018_1d_real_2019_2026"   # v1 缓存 (选股 parquet 复用)
TAG = "gp018_1d_real_2019_2026_v2"   # v2 输出
OUT_DIR = "research/gp018/results"

BT_CFG = {
    "initial_capital": 1000000.0,
    "commission": 0.0003,
    "slippage": 0.001,
    "period": "1d",
    "degrade_5m": False,
    # —— 要求 2/3/4: 单票5% + 70%仓位 + 连亏冷却 ——
    "max_total_exposure": 0.70,                  # 要求3: 持仓市值/总权益>=70% 停开新仓
    "loss_streak_halt": {"n": 3, "days": 3},     # 要求4: 全局连亏3次停开3交易日
    "position_sizing": {
        "max_positions": 999,
        "min_buy_amount": 2000.0,
        "max_buy_amount": 10 ** 9,               # 取消 2万 卡死, 让 5% 主导
        "max_position_pct": 0.05,                # 要求2: 单票<=当前总权益 5% (动态)
        "lot_size": 100,
        "min_lots": 1,
    },
}

STOP_CFG = {
    "priority": "stop_first",                        # 硬止损优先
    "cost_stop": {"enabled": True, "threshold": -0.06},   # 硬止损 6%
    "trailing_stop": {                               # 移动止盈 REAL 模式
        "enabled": True,
        "activation": 0.035,                         # 3.5% 激活
        "drawdown": 0.01,                            # 1% 回撤全卖
        "confirm": "real",                           # 条件单语义
    },
    "ladder_tp": {"enabled": False, "levels": []},   # 关闭阶梯止盈
    "time_stop": {"enabled": True, "max_hold_days": 6},   # 要求1: 12 -> 6 交易日
    "cond_time_stop": {"enabled": False},
    "first_day": {"enabled": False},
    "formula_sell": {"enabled": False},
}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    t0 = time.time()

    # ===== 选股: 复用 v1 缓存 (参数不改信号, 省选股 ~30s) =====
    sel_path = f"{OUT_DIR}/{TAG_V1}_selections.parquet"
    if not os.path.exists(sel_path):
        print(f"[FAIL] 缺 v1 选股缓存 {sel_path}, 先跑 run_gp018_1d_real_2019.py", flush=True)
        return
    selections = pd.read_parquet(sel_path)
    n = len(selections)
    print(f"[INFO] 复用 v1 选股缓存: {n} 信号  ({sel_path})", flush=True)

    # ===== 回测 =====
    t1 = time.time()
    engine = BacktestEngine(BT_CFG)
    result = engine.run(selections=selections, start_time=START, end_time=END,
                        stop_config=STOP_CFG)
    m = result.get("metrics") or {}
    print(f"[DONE] 回测耗时 {time.time()-t1:.0f}s  总耗时 {time.time()-t0:.0f}s", flush=True)

    # ===== 关键指标 (用户关心的四件套 + 收益/笔数/盈亏比) =====
    sep = "=" * 64
    print(sep, flush=True)
    print(f"累计收益 cum    = {m.get('cumulative_return',0)*100:.2f}%", flush=True)
    print(f"年化收益 ann    = {m.get('annualized_return',0)*100:.2f}%", flush=True)
    print(f"夏普  sharpe    = {m.get('sharpe_ratio',0):.3f}", flush=True)
    _calmar = m.get('calmar_ratio')
    print(f"Calmar          = {_calmar:.3f}" if isinstance(_calmar, (int, float)) else f"Calmar = {_calmar}", flush=True)
    print(f"最大回撤 dd     = {m.get('max_drawdown',0)*100:.2f}%", flush=True)
    print(f"胜率  win_rate  = {m.get('win_rate',0)*100:.1f}%", flush=True)
    print(f"交易笔数 trades = {m.get('total_trades')}", flush=True)
    print(f"盈亏比 pf       = {m.get('profit_factor',0):.2f}", flush=True)
    print(sep, flush=True)
    print("[ALL METRICS]", flush=True)
    print(json.dumps(m, ensure_ascii=False, default=str, indent=1), flush=True)

    # ===== 落盘 =====
    with open(f"{OUT_DIR}/{TAG}.json", "w", encoding="utf-8") as f:
        json.dump({
            "formula": FORMULA, "range": [START, END],
            "version": "v2",
            "changes": ["time_stop 12->6", "单票2万->5%动态", "70%仓位停开新仓", "连亏3次停3天"],
            "stop_config": STOP_CFG, "bt_cfg": BT_CFG,
            "metrics": m, "n_signals": int(n),
            "elapsed_sec": round(time.time() - t0),
        }, f, ensure_ascii=False, default=str, indent=1)
    trades = result.get("trades")
    if hasattr(trades, "to_parquet"):
        trades.to_parquet(f"{OUT_DIR}/{TAG}_trades.parquet")
    eq = result.get("equity_curve")
    if hasattr(eq, "to_parquet"):
        eq.to_parquet(f"{OUT_DIR}/{TAG}_equity.parquet")
    print(f"[OK] 落盘 {OUT_DIR}/{TAG}*  (metrics/trades/equity)", flush=True)


if __name__ == "__main__":
    main()
