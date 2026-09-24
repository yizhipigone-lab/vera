# -*- coding: utf-8 -*-
"""GUPIAO_018 | 1D | REAL 移动止盈 | 2019-至今 全A非ST 回测

参数 (用户 2026-08-08 拍板, 去睡觉前交代):
  - 公式 GUPIAO_018, 直传通达信接口
  - 区间 20190101 -> 20260808
  - 全A非ST (TDX type 50)
  - 1D 精度, 前复权 (dividend_type=1)
  - 优先级: stop_first (硬止损优先)
  - 硬止损 6% (cost_stop threshold=-0.06)
  - 阶梯止盈: 关
  - 移动止盈: REAL 模式, 3.5% 激活, 1% 回撤全卖
  - 时间止损: 12 交易日
  - 初始资金 100 万, 单票上限 2 万 (max_buy_amount), 佣金 0.03% + 滑点 0.1%

REAL 模式核对 (trailing.py:_check_real, 2026-08-05 用户拍板): 1D/5M 同一套, 条件单语义 —
  创峰值 bar 不判 / 跳空按开盘 / 触线按线价。1D 下非空操作, 代价 = 峰值日多观察一天。

用法: python -X utf8 research/gp018/run_gp018_1d_real_2019.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from backtest.engine import BacktestEngine
from selection.selector import StockSelector

FORMULA = "GUPIAO_018"
START = "20190101"
END = "20260808"
UNIVERSE_TYPE = "50"  # 全A
TAG = "gp018_1d_real_2019_2026"
OUT_DIR = "research/gp018/results"

SEL_CFG = {
    "formula_name": FORMULA,
    "formula_arg": "",
    "universe": {"type": UNIVERSE_TYPE, "exclude_st": True},
    "period": "1d",
    "dividend_type": 1,
}

BT_CFG = {
    "initial_capital": 1000000.0,
    "commission": 0.0003,
    "slippage": 0.001,
    "period": "1d",
    "degrade_5m": False,
    "position_sizing": {
        "max_positions": 999,
        "min_buy_amount": 2000.0,
        "max_buy_amount": 20000.0,
        "lot_size": 100,
        "min_lots": 1,
    },
}

STOP_CFG = {
    "priority": "stop_first",                      # 硬止损优先
    "cost_stop": {"enabled": True, "threshold": -0.06},   # 硬止损 6%
    "trailing_stop": {                             # 移动止盈 REAL 模式
        "enabled": True,
        "activation": 0.035,                        # 3.5% 激活
        "drawdown": 0.01,                           # 1% 回撤全卖
        "confirm": "real",                          # 条件单语义
    },
    "ladder_tp": {"enabled": False, "levels": []},  # 关闭阶梯止盈
    "time_stop": {"enabled": True, "max_hold_days": 12},  # 12 交易日无条件退出
    "cond_time_stop": {"enabled": False},
    "first_day": {"enabled": False},
    "formula_sell": {"enabled": False},
}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    t0 = time.time()

    # ===== 选股 =====
    print(f"[INFO] 选股 {FORMULA} universe=全A(type {UNIVERSE_TYPE}) 非ST  {START}->{END}", flush=True)
    selections = StockSelector(SEL_CFG).run(start_time=START, end_time=END)
    n = 0 if selections is None else len(selections)
    print(f"[INFO] 信号数 = {n}  (选股耗时 {time.time()-t0:.0f}s)", flush=True)
    if not n:
        print("[FAIL] 无信号, 终止", flush=True)
        return
    selections.to_parquet(f"{OUT_DIR}/{TAG}_selections.parquet")

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
    # dump 全部 metrics 字段 (不猜字段名, 看实际有什么)
    print("[ALL METRICS]", flush=True)
    print(json.dumps(m, ensure_ascii=False, default=str, indent=1), flush=True)

    # ===== 落盘 =====
    with open(f"{OUT_DIR}/{TAG}.json", "w", encoding="utf-8") as f:
        json.dump({
            "formula": FORMULA, "universe": UNIVERSE_TYPE, "range": [START, END],
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
    print(f"[OK] 落盘 {OUT_DIR}/{TAG}*  (metrics/trades/equity/selections)", flush=True)


if __name__ == "__main__":
    main()
