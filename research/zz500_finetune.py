# -*- coding: utf-8 -*-
"""zz500_finetune.py
===================
中证500 + MA金叉10/30 (三池最优因子+池) 的分段稳定性验证 + 出场精修冲 18.88%。

三池结论 (2026-08-11):
  - 中证500 是最优池: 天花板17.34% (MA金叉10/30慢松快锁), 黄金组合=基准16.45%/-19.83%/夏普1.36
  - 三池穷证 18.88% 不可达 (54组合0达标), 中证500最接近, 差1.54%
  - 本脚本: 先分段验证底子实不实 (防A500段1虚高重演), 底子OK则精修冲18.88%

用法: python -X utf8 research/zz500_finetune.py
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
    FACTORS, signals_to_selections, make_bt_cfg, make_stop,
    PERIOD, MAX_DD, CAPITAL,
)

logger = get_logger(__name__)

LONG_START, LONG_END = "20060101", "20260731"
TARGET_ANN = 0.1888
UTYPE = "24"
RUN_DIR = "research/auto_optimizer_runs"
MAX_POS, MIN_AMT, MAX_AMT, COOLDOWN = 5, 10000, 100000, 20

SEGMENTS = [
    ("全区间", "20060101", "20260731"),
    ("段1(07牛08灾)", "20060101", "20121231"),
    ("段2(15牛灾)", "20130101", "20191231"),
    ("段3(21核心资产)", "20200101", "20260731"),
]

# 出场精修网格 (基于 基准-6/3.5/1/12 [最稳] 和 慢松快锁-8/4/1/15 [最高] 附近)
EXIT_GRID = [
    (-0.06, 0.035, 0.01, 12, None, "基准(-6/3.5/1/12)"),
    (-0.08, 0.04, 0.01, 15, None, "慢松快锁(-8/4/1/15)"),
    (-0.08, 0.06, 0.02, 15, None, "慢松(-8/6/2/15)"),
    (-0.06, 0.035, 0.01, 20, None, "基准延时20d"),
    (-0.08, 0.04, 0.01, 20, None, "慢松快锁延时20d"),
    (-0.06, 0.035, 0.01, 12, [(0.05, 0.5), (0.10, 0.5)], "基准+阶梯5/10"),
    (-0.08, 0.04, 0.01, 15, [(0.06, 0.4), (0.12, 0.6)], "快锁+阶梯6/12"),
    (-0.07, 0.04, 0.01, 15, None, "中止损快锁(-7/4/1/15)"),
    (-0.06, 0.05, 0.015, 15, None, "基准放宽移动(5/1.5)"),
    (-0.05, 0.03, 0.008, 12, None, "快紧(-5/3/0.8/12)"),
]


def build_bt_cfg() -> dict:
    cfg = make_bt_cfg(MAX_POS, MIN_AMT, MAX_AMT)
    cfg["sell_cooldown_days"] = COOLDOWN
    return cfg


def run_seg(selections, bt_cfg, stop, start, end, tag):
    eng = BacktestEngine(bt_cfg)
    res = eng.run(selections=selections, start_time=start, end_time=end, stop_config=stop)
    m = res.get("metrics") or {}
    ann = float(m.get("annualized_return", 0) or 0)
    dd = float(m.get("max_drawdown", 1) or 1)
    sharpe = float(m.get("sharpe_ratio", 0) or 0)
    trades = int(m.get("total_trades", 0) or 0)
    wr = float(m.get("win_rate", 0) or 0)
    logger.info("[%-16s] %s~%s | 年化=%6.2f%% 回撤=%6.2f%% 夏普=%5.2f 交易=%-6d 胜率=%5.1f%%",
                tag, start, end, ann * 100, dd * 100, sharpe, trades, wr * 100)
    return res


def yearly_returns(equity_curve) -> pd.DataFrame:
    if equity_curve is None or len(equity_curve) == 0:
        return pd.DataFrame()
    ec = equity_curve.copy()
    ec["date"] = pd.to_datetime(ec["date"])
    ec["year"] = ec["date"].dt.year
    year_end = ec.groupby("year")["equity"].last()
    prev = year_end.shift(1).fillna(CAPITAL)
    ret = (year_end / prev - 1)
    return pd.DataFrame({"年末权益": year_end.round(0),
                          "年度收益": (ret * 100).round(2).astype(str) + "%"})


def main():
    print("=" * 92)
    print(f"  中证500 + MA金叉10/30 分段稳定性 + 出场精修 | {LONG_START}~{LONG_END} | 门槛年化>{TARGET_ANN:.2%}")
    print("=" * 92)

    sel = StockSelector({"formula_name": "_", "universe": {"type": UTYPE, "exclude_st": True},
                         "period": PERIOD, "dividend_type": 1})
    codes = sel.resolve_universe()
    logger.info("股票池 中证500 -> %d 只", len(codes))
    kl = DataFetcher.get_kline(codes, LONG_START, LONG_END, period=PERIOD,
                               dividend_type="front", use_cache=True)
    if not kl or "Close" not in kl:
        logger.error("取数失败"); return
    close = kl["Close"]; high = kl.get("High"); low = kl.get("Low"); vol = kl.get("Volume")
    sig = FACTORS["ma_cross"](close, high=high, low=low, vol=vol, fast=10, slow=30)
    selections = signals_to_selections(sig, "MA金叉10/30")
    logger.info("MA金叉10/30 信号 %d 条", len(selections))

    bt_cfg = build_bt_cfg()

    # A. 分段稳定性 (基准=最稳 + 慢松快锁=最高)
    print("\n" + "=" * 92)
    print("  A1. 分段: MA金叉10/30 + 基准(-6/3.5/1/12) [黄金组合]")
    print("=" * 92)
    stop_base = make_stop(-0.06, 0.035, 0.01, 12, None)
    res_full = None
    for tag, s, e in SEGMENTS:
        r = run_seg(selections, bt_cfg, stop_base, s, e, tag)
        if tag == "全区间":
            res_full = r
    if res_full is not None:
        print("\n  年度收益分解:")
        yr = yearly_returns(res_full.get("equity_curve"))
        if len(yr):
            print(yr.to_string())
            rets = yr["年度收益"].str.rstrip("%").astype(float)
            logger.info("正收益年 %d/%d, 最好 %.1f%%, 最差 %.1f%%, 年度均值 %.1f%%",
                        (rets > 0).sum(), len(rets), rets.max(), rets.min(), rets.mean())

    print("\n" + "=" * 92)
    print("  A2. 分段: MA金叉10/30 + 慢松快锁(-8/4/1/15) [年化最高]")
    print("=" * 92)
    stop_lock = make_stop(-0.08, 0.04, 0.01, 15, None)
    for tag, s, e in SEGMENTS:
        run_seg(selections, bt_cfg, stop_lock, s, e, tag)

    # B. 出场精修
    print("\n" + "=" * 92)
    print("  B. 出场精修网格: MA金叉10/30 × 10 变体 (冲 18.88%)")
    print("=" * 92)
    rows = []
    for cost, tact, tdd, tdays, ladder, ename in EXIT_GRID:
        try:
            stop = make_stop(cost, tact, tdd, tdays, ladder)
            res = BacktestEngine(bt_cfg).run(selections=selections,
                                              start_time=LONG_START, end_time=LONG_END,
                                              stop_config=stop)
            m = res.get("metrics") or {}
            ann = float(m.get("annualized_return", 0) or 0)
            dd = float(m.get("max_drawdown", 1) or 1)
            sharpe = float(m.get("sharpe_ratio", 0) or 0)
            trades = int(m.get("total_trades", 0) or 0)
            wr = float(m.get("win_rate", 0) or 0)
            ok_ann = ann >= TARGET_ANN
            ok_dd = abs(dd) <= MAX_DD
            mark = "[双达标]" if (ok_ann and ok_dd) else ("[年化达标]" if ok_ann else "")
            logger.info("%-30s | 年化=%6.2f%% 回撤=%6.2f%% 夏普=%5.2f 交易=%-6d 胜率=%5.1f%% %s",
                        ename, ann * 100, dd * 100, sharpe, trades, wr * 100, mark)
            rows.append({"出场": ename, "年化": f"{ann*100:.2f}%", "回撤": f"{dd*100:.2f}%",
                         "夏普": f"{sharpe:.2f}", "交易": trades, "胜率": f"{wr*100:.1f}%",
                         "年化达标": "✓" if ok_ann else "", "双达标": "✓" if (ok_ann and ok_dd) else ""})
        except Exception as ex:
            logger.warning("[%s] 异常: %s", ename, ex)

    print("\n" + "=" * 92)
    print("  出场精修结果 (按年化降序)")
    print("=" * 92)
    if rows:
        df = pd.DataFrame(rows)
        df["_s"] = df["年化"].str.rstrip("%").astype(float)
        df = df.sort_values("_s", ascending=False).drop(columns="_s")
        print(df.to_string(index=False))
        n_ann = (df["年化达标"] == "✓").sum()
        n_both = (df["双达标"] == "✓").sum()
        print(f"\n年化达标(>18.88%): {n_ann}/{len(df)}   双达标: {n_both}/{len(df)}")
        if len(df):
            best = df.iloc[0]
            print(f"最优: {best['出场']} -> 年化{best['年化']} 回撤{best['回撤']} 夏普{best['夏普']}")
    print("=" * 92)
    logger.info("精修完成")


if __name__ == "__main__":
    TURN_START = time.time()
    try:
        main()
    finally:
        logger.info("总耗时 %.0fs", time.time() - TURN_START)
