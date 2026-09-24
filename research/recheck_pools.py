# -*- coding: utf-8 -*-
"""recheck_pools.py
===================
同策略换池对比: 中证500(基准) vs 沪深300 / 中证1000 / 创业板 (2026-08-21)。

策略完全一致: MA金叉10/30 + 慢松快锁延时20d (make_stop(-0.08,0.04,0.01,20,None))
+ 5仓/单票1-10万/冷却20天 + close_t 口径 (T日收盘买入), 2006-01 ~ 2026-07。

用法: python -X utf8 research/recheck_pools.py
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

from backtest.engine import BacktestEngine
from core.data_fetcher import DataFetcher
from selection.selector import StockSelector
from utils.logger import get_logger
from auto_strategy_optimizer import (
    FACTORS, signals_to_selections, make_bt_cfg, make_stop, PERIOD,
)

logger = get_logger(__name__)

LONG_START, LONG_END = "20060101", "20260731"
MAX_POS, MIN_AMT, MAX_AMT, COOLDOWN = 5, 10000, 100000, 20

POOLS = [
    ("23", "沪深300"),
    ("24", "中证500"),   # 基准
    ("25", "中证1000"),
    ("51", "创业板"),
]


def run_pool(utype: str, name: str) -> dict:
    sel = StockSelector({"formula_name": "_", "universe": {"type": utype, "exclude_st": True},
                         "period": PERIOD, "dividend_type": 1})
    codes = sel.resolve_universe()
    logger.info("[%s] 池 %d 只", name, len(codes))

    kl = DataFetcher.get_kline(codes, LONG_START, LONG_END, period=PERIOD,
                               dividend_type="front", use_cache=True)
    if not kl or "Close" not in kl:
        logger.error("[%s] 取数失败", name)
        return {}
    close = kl["Close"]; high = kl.get("High"); low = kl.get("Low"); vol = kl.get("Volume")
    sig = FACTORS["ma_cross"](close, high=high, low=low, vol=vol, fast=10, slow=30)
    selections = signals_to_selections(sig, "MA金叉10/30")
    logger.info("[%s] 信号 %d 条", name, len(selections))

    cfg = make_bt_cfg(MAX_POS, MIN_AMT, MAX_AMT)
    cfg["sell_cooldown_days"] = COOLDOWN
    stop = make_stop(-0.08, 0.04, 0.01, 20, None)
    res = BacktestEngine(cfg).run(selections=selections, start_time=LONG_START,
                                  end_time=LONG_END, stop_config=stop)
    m = res.get("metrics") or {}
    ann = float(m.get("annualized_return", 0) or 0)
    dd = float(m.get("max_drawdown", 0) or 0)
    sh = float(m.get("sharpe_ratio", 0) or 0)
    wr = float(m.get("win_rate", 0) or 0)
    tr = int(m.get("total_trades", 0) or 0)
    cum = float(m.get("cumulative_return", 0) or 0)

    # 年度链 → 分段年化
    ec = res.get("equity_curve")
    ec = ec.copy()
    ec["date"] = pd.to_datetime(ec["date"])
    ec["year"] = ec["date"].dt.year
    by_year = {}
    for y, g in ec.groupby("year"):
        eq = g.set_index("date")["equity"].sort_index()
        by_year[y] = eq.iloc[-1] / eq.iloc[0] - 1
    segs = {}
    for (a, b), sname in [((2006, 2013), "段1_2006-12"), ((2013, 2020), "段2_2013-19"),
                          ((2020, 2027), "段3_2020-26")]:
        prod = 1.0
        for y in range(a, b):
            prod *= (1 + by_year.get(y, 0.0))
        segs[sname] = prod ** (1 / (b - a)) - 1
    neg_years = sum(1 for r in by_year.values() if r < 0)

    logger.info("[%s] 年化=%.2f%% 回撤=%.2f%% 夏普=%.2f 胜率=%.1f%% 交易=%d 累计=%.1f%% 亏年=%d 段1=%.2f%% 段2=%.2f%% 段3=%.2f%%",
                name, ann * 100, dd * 100, sh, wr * 100, tr, cum * 100, neg_years,
                segs["段1_2006-12"] * 100, segs["段2_2013-19"] * 100, segs["段3_2020-26"] * 100)
    return {"池": name, "年化": f"{ann*100:.2f}%", "回撤": f"{dd*100:.2f}%",
            "夏普": f"{sh:.2f}", "胜率": f"{wr*100:.1f}%", "交易": tr,
            "累计": f"{cum*100:+.1f}%", "亏损年": neg_years,
            "段1(06-12)": f"{segs['段1_2006-12']*100:.2f}%",
            "段2(13-19)": f"{segs['段2_2013-19']*100:.2f}%",
            "段3(20-26)": f"{segs['段3_2020-26']*100:.2f}%"}


def main():
    print("=" * 100)
    print("  同策略换池对比: MA金叉10/30 + 慢松快锁延时20d | close_t | 2006-01~2026-07")
    print("=" * 100)
    rows = []
    for utype, name in POOLS:
        t0 = time.time()
        r = run_pool(utype, name)
        if r:
            rows.append(r)
        logger.info("[%s] 完成 %.0fs", name, time.time() - t0)
    if rows:
        df = pd.DataFrame(rows)
        print("\n" + df.to_string(index=False))
        out = "research/auto_optimizer_runs/pool_compare.csv"
        df.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"\n已存: {out}")


if __name__ == "__main__":
    TURN_START = time.time()
    try:
        main()
    finally:
        logger.info("总耗时 %.0fs", time.time() - TURN_START)
