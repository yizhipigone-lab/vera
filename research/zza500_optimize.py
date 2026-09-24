# -*- coding: utf-8 -*-
"""zza500_optimize.py
=================
中证A500 (type=28) 池上多因子×多出场寻优 (2026-08-11)。

背景: pool_curve.py 诊断发现中盘是甜区 (倒U型峰值), 中证A500+MA金叉5/20 达标 30.58%。
本脚本验证: ① 基准组合能重现 30.58% (坐实非偶然); ② 多因子×多出场是否还有更稳/更高的达标组合。

参数对齐 pool_curve 达标口径: 单票10万 + 5仓 + 卖出冷却20天 (与 pond_axis 一致)。
口径: 日线 / 止损优先 / 信号日T收盘买入 / 移动止盈条件单语义 (confirm=real)。

用法: python -X utf8 research/zza500_optimize.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import pandas as pd

from core.data_fetcher import DataFetcher
from selection.selector import StockSelector
from utils.logger import get_logger
from auto_strategy_optimizer import (
    FACTORS, signals_to_selections, run_backtest, make_bt_cfg, make_stop,
    START, END, PERIOD, TARGET_ANN, MAX_DD,
)

logger = get_logger(__name__)

UTYPE = "28"            # 中证A500
POOL_NAME = "中证A500"
RUN_DIR = "research/auto_optimizer_runs"

# 仓位预设 (达标口径, 与 pool_curve / pond_axis 一致)
MAX_POS = 5
MIN_AMT = 10000
MAX_AMT = 100000
COOLDOWN = 20

# 因子网格: (因子key, 参数dict, 显示名)
FACTOR_GRID = [
    ("ma_cross", {"fast": 5, "slow": 20}, "MA金叉5/20"),       # 基准 (应重现30.58%)
    ("ma_cross", {"fast": 10, "slow": 30}, "MA金叉10/30"),     # 慢趋势
    ("breakout", {"window": 20}, "突破20日"),                   # 唐奇安突破
    ("momentum", {"window": 20}, "动量20日"),                   # 动量
    ("volume_breakout", {"window": 20, "vol_ma": 5}, "放量突破"),# 量价齐升
    ("rsi_rebound", {"window": 14, "low": 30}, "RSI超卖反弹"),   # 反转
]

# 出场网格: (cost_stop, trail_act, trail_dd, time_days, ladder, 显示名)
EXIT_GRID = [
    (-0.06, 0.035, 0.01, 12, None, "基准(-6%/3.5%/1%/12d)"),    # A: pool_curve 达标基准
    (-0.08, 0.06, 0.02, 15, None, "慢松(-8%/6%/2%/15d)"),       # B: 更大空间
    (-0.05, 0.03, 0.005, 8, None, "快紧(-5%/3%/0.5%/8d)"),      # C: 快进快出
]


def build_bt_cfg() -> dict:
    cfg = make_bt_cfg(MAX_POS, MIN_AMT, MAX_AMT)
    cfg["sell_cooldown_days"] = COOLDOWN
    return cfg


def main():
    print("=" * 86)
    print(f"  中证A500 多因子×多出场寻优 | 区间 {START}~{END} | 日线")
    print(f"  参数: 单票{MAX_AMT//10000}万 + {MAX_POS}仓 + 冷却{COOLDOWN}天 | 达标: 年化>={TARGET_ANN:.0%} 且 回撤<={MAX_DD:.0%}")
    print("=" * 86)

    # 1. 池 + 取数 (只算一次, 复用本地K线缓存)
    sel = StockSelector({"formula_name": "_", "universe": {"type": UTYPE, "exclude_st": True},
                         "period": PERIOD, "dividend_type": 1})
    codes = sel.resolve_universe()
    logger.info("股票池 %s (type=%s) -> %d 只", POOL_NAME, UTYPE, len(codes))
    t0 = time.time()
    kl = DataFetcher.get_kline(codes, START, END, period=PERIOD,
                               dividend_type="front", use_cache=True)
    if not kl or "Close" not in kl:
        logger.error("取数失败, 退出")
        return
    close = kl["Close"]; high = kl.get("High"); low = kl.get("Low"); vol = kl.get("Volume")
    logger.info("OHLCV 就绪: %d 股 × %d 日, 取数 %.0fs", close.shape[1], close.shape[0], time.time() - t0)

    bt_cfg = build_bt_cfg()

    # 2. 网格寻优
    rows = []
    t_start = time.time()
    for fkey, fparams, fname in FACTOR_GRID:
        # 该因子的信号只算一次, 多出场复用 (selections 相同 -> engine matrix_cache 命中)
        try:
            sig = FACTORS[fkey](close, high=high, low=low, vol=vol, **fparams)
            selections = signals_to_selections(sig, fname)
            n_sig = len(selections)
        except Exception as e:
            logger.warning("[%s] 因子计算失败: %s", fname, e)
            continue
        if n_sig == 0:
            logger.warning("[%s] 信号为空, 跳过", fname)
            continue

        for cost, tact, tdd, tdays, ladder, ename in EXIT_GRID:
            tag = f"{fname} | {ename}"
            try:
                stop = make_stop(cost, tact, tdd, tdays, ladder)
                m = run_backtest(selections, bt_cfg, stop)
                ann = float(m.get("annualized_return", 0) or 0)
                dd = float(m.get("max_drawdown", 1) or 1)
                sharpe = float(m.get("sharpe_ratio", 0) or 0)
                trades = int(m.get("total_trades", 0) or 0)
                wr = float(m.get("win_rate", 0) or 0)
                ok = ann >= TARGET_ANN and abs(dd) <= MAX_DD and trades > 0
                logger.info("%-46s | 信号=%-6d 年化=%6.2f%% 回撤=%6.2f%% 夏普=%5.2f 交易=%-5d 胜率=%5.1f%% %s",
                            tag, n_sig, ann * 100, dd * 100, sharpe, trades, wr * 100,
                            "[达标]" if ok else "")
                rows.append({"因子": fname, "出场": ename, "信号": n_sig,
                             "年化": f"{ann*100:.2f}%", "回撤": f"{dd*100:.2f}%",
                             "夏普": f"{sharpe:.2f}", "交易": trades,
                             "胜率": f"{wr*100:.1f}%", "达标": "✓" if ok else ""})
            except Exception as e:
                logger.warning("[%s] 回测异常: %s", tag, e)

    # 3. 汇总
    print("\n" + "=" * 86)
    print("  中证A500 寻优结果 (按年化降序)")
    print("=" * 86)
    if rows:
        df = pd.DataFrame(rows)
        df["_sort"] = df["年化"].str.rstrip("%").astype(float)
        df = df.sort_values("_sort", ascending=False).drop(columns="_sort")
        print(df.to_string(index=False))
        os.makedirs(RUN_DIR, exist_ok=True)
        out = os.path.join(RUN_DIR, "zza500_results.csv")
        df.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"\n[CSV] {out}")
        n_ok = (df["达标"] == "✓").sum()
        best = df.iloc[0]
        print(f"\n达标组合: {n_ok} / {len(df)}")
        print(f"最优: {best['因子']} + {best['出场']} -> 年化{best['年化']} 回撤{best['回撤']} 夏普{best['夏普']}")
    print("=" * 86)
    logger.info("寻优完成, 总耗时 %.0fs", time.time() - t_start)


if __name__ == "__main__":
    TURN_START = time.time()
    try:
        main()
    finally:
        logger.info("总耗时 %.0fs", time.time() - TURN_START)
