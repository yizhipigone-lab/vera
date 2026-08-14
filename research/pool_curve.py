# -*- coding: utf-8 -*-
"""
pool_curve.py
=============
池—收益曲线诊断 (2026-08-11): 同一因子(MA金叉5/20) + 同一参数, 跑多个指数池,
验证"大盘蓝筹最优, 小盘失效"是否单调, 找池的甜区。

参数对齐 pond_optimizer 诊断 (单票10万+5仓+冷却20), 与本次 g4主板/g6全A 严格可比。

用法: python -X utf8 research/pool_curve.py
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

# (池名, universe type) — 含主板/全A对照
POOLS = [
    ("沪深300", "23"),
    ("中证A500", "28"),
    ("中证500", "24"),
    ("中证1000", "25"),
    ("主板", "mb"),   # 特殊: 全A剔除创业/科创
    ("全A", "5"),
]


def resolve_pool(name: str, utype: str) -> list:
    if utype == "mb":
        u = set(DataFetcher.get_stock_universe("5"))
        uni50 = set(DataFetcher.get_stock_universe("50"))
        uni51 = set(DataFetcher.get_stock_universe("51"))
        uni52 = set(DataFetcher.get_stock_universe("52"))
        codes = list((uni50 - uni51 - uni52) & u)
    else:
        sel = StockSelector({"formula_name": "_", "universe": {"type": utype, "exclude_st": True},
                             "period": PERIOD, "dividend_type": 1})
        codes = sel.resolve_universe()
    return codes


def main():
    print("=" * 78)
    print(f"  池—收益曲线诊断 | MA金叉5/20 | 区间 {START}~{END}")
    print(f"  参数: 单票10万+5仓+冷却20天+硬止损-6%+移动止盈3.5%/1%(条件单)+时间12天")
    print(f"  达标门槛: 年化 >= {TARGET_ANN:.0%}  且  最大回撤 <= {MAX_DD:.0%}")
    print("=" * 78)

    bt_cfg = make_bt_cfg(5, 10000, 100000)
    bt_cfg["sell_cooldown_days"] = 20
    stop = make_stop(-0.06, 0.035, 0.01, 12, None)

    rows = []
    for name, utype in POOLS:
        t0 = time.time()
        try:
            codes = resolve_pool(name, utype)
            logger.info("[%s] 池规模 %d 只", name, len(codes))
            kl = DataFetcher.get_kline(codes, START, END, period=PERIOD,
                                       dividend_type="front", use_cache=True)
            if not kl or "Close" not in kl:
                logger.warning("[%s] 取数失败, 跳过", name)
                continue
            close = kl["Close"]; high = kl.get("High"); low = kl.get("Low"); vol = kl.get("Volume")
            sig = FACTORS["ma_cross"](close, high=high, low=low, vol=vol, fast=5, slow=20)
            sel = signals_to_selections(sig, "MA金叉5/20")
            m = run_backtest(sel, bt_cfg, stop)
            ann = float(m.get("annualized_return", 0) or 0)
            dd = float(m.get("max_drawdown", 1) or 1)
            sharpe = float(m.get("sharpe_ratio", 0) or 0)
            trades = int(m.get("total_trades", 0) or 0)
            wr = float(m.get("win_rate", 0) or 0)
            ok = ann >= TARGET_ANN and abs(dd) <= MAX_DD and trades > 0
            elapsed = time.time() - t0
            logger.info("[%s] %-7s | 信号=%-6d 年化=%6.2f%% 回撤=%6.2f%% 夏普=%5.2f 交易=%-5d 胜率=%5.1f%% %s 耗时=%.0fs",
                        name, name, len(sel), ann * 100, dd * 100, sharpe, trades, wr * 100,
                        "[达标]" if ok else "", elapsed)
            rows.append({"池": name, "规模": len(codes), "信号": len(sel),
                         "年化": f"{ann*100:.2f}%", "回撤": f"{dd*100:.2f}%",
                         "夏普": f"{sharpe:.2f}", "交易": trades, "胜率": f"{wr*100:.1f}%",
                         "达标": "✓" if ok else ""})
        except Exception as e:
            logger.warning("[%s] 异常: %s", name, e)

    print("\n" + "=" * 78)
    print("  池—收益曲线 (MA金叉5/20, 同参数)")
    print("=" * 78)
    if rows:
        df = pd.DataFrame(rows)
        print(df.to_string(index=False))
    print("=" * 78)
    logger.info("诊断完成")


if __name__ == "__main__":
    TURN_START = time.time()
    try:
        main()
    finally:
        logger.info("总耗时 %.0fs", time.time() - TURN_START)
