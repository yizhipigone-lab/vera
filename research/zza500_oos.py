# -*- coding: utf-8 -*-
"""zza500_oos.py
================
中证A500 + MA金叉5/20 + 基准出场的样本外稳健验证 (2026-08-11)。

目的: 30.58% (全区间 2019-2026) 是稳赚还是靠某段行情撑出来的? 防过拟合。

三段独立回测 (各从 100万 初始资金):
  - 全区间:  20190101 ~ 20260731  (基线 30.58%, 已坐实)
  - 训练期:  20190101 ~ 20221230  (前 4 年)
  - 样本外:  20230103 ~ 20260731  (后 3.5 年, OOS)

年度分解: 全区间权益曲线按年切片, 看每年收益率分布 (抓"某年暴利撑平均")。

判定:
  - OOS 年化 >= 20% 且训练期也健康  → 稳健, 非过拟合
  - OOS 年化 < 10% 或某年暴利撑全局  → 过拟合风险, 诚实报告

用法: python -X utf8 research/zza500_oos.py
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
    PERIOD, CAPITAL, TARGET_ANN, MAX_DD,
)

logger = get_logger(__name__)

UTYPE = "28"
FULL_START, FULL_END = "20190101", "20260731"
TRAIN_START, TRAIN_END = "20190101", "20221230"
OOS_START, OOS_END = "20230103", "20260731"


def build_cfg() -> dict:
    cfg = make_bt_cfg(5, 10000, 100000)
    cfg["sell_cooldown_days"] = 20
    return cfg


def run_seg(selections, bt_cfg, stop, start, end, tag):
    """单段回测, 返回 (metrics, equity_curve)。"""
    eng = BacktestEngine(bt_cfg)
    res = eng.run(selections=selections, start_time=start, end_time=end, stop_config=stop)
    m = res.get("metrics") or {}
    ec = res.get("equity_curve")
    ann = float(m.get("annualized_return", 0) or 0)
    dd = float(m.get("max_drawdown", 1) or 1)
    sharpe = float(m.get("sharpe_ratio", 0) or 0)
    trades = int(m.get("total_trades", 0) or 0)
    wr = float(m.get("win_rate", 0) or 0)
    logger.info("[%-8s] %s~%s | 年化=%6.2f%% 回撤=%6.2f%% 夏普=%5.2f 交易=%-5d 胜率=%5.1f%%",
                tag, start, end, ann * 100, dd * 100, sharpe, trades, wr * 100)
    return m, ec


def yearly_returns(equity_curve) -> pd.DataFrame:
    """权益曲线 -> 逐年收益率表。"""
    if equity_curve is None or len(equity_curve) == 0:
        return pd.DataFrame()
    ec = equity_curve.copy()
    ec["date"] = pd.to_datetime(ec["date"])
    ec["year"] = ec["date"].dt.year
    year_end = ec.groupby("year")["equity"].last()
    prev = year_end.shift(1).fillna(CAPITAL)
    ret = (year_end / prev - 1)
    out = pd.DataFrame({"年末权益": year_end.round(0), "年度收益": (ret * 100).round(2).astype(str) + "%"})
    out.index.name = "年份"
    return out


def main():
    print("=" * 78)
    print("  中证A500 + MA金叉5/20 + 基准出场 | 样本外稳健验证")
    print(f"  全区间 {FULL_START}~{FULL_END} | 训练 {TRAIN_START}~{TRAIN_END} | OOS {OOS_START}~{OOS_END}")
    print(f"  达标门槛: 年化>={TARGET_ANN:.0%} 且 回撤<={MAX_DD:.0%}")
    print("=" * 78)

    sel = StockSelector({"formula_name": "_", "universe": {"type": UTYPE, "exclude_st": True},
                         "period": PERIOD, "dividend_type": 1})
    codes = sel.resolve_universe()
    logger.info("股票池 中证A500 (type=%s) -> %d 只", UTYPE, len(codes))

    kl = DataFetcher.get_kline(codes, FULL_START, FULL_END, period=PERIOD,
                               dividend_type="front", use_cache=True)
    if not kl or "Close" not in kl:
        logger.error("取数失败"); return
    close = kl["Close"]; high = kl.get("High"); low = kl.get("Low"); vol = kl.get("Volume")
    sig = FACTORS["ma_cross"](close, high=high, low=low, vol=vol, fast=5, slow=20)
    selections = signals_to_selections(sig, "MA金叉5/20")
    logger.info("全区间信号 %d 条", len(selections))

    bt_cfg = build_cfg()
    stop = make_stop(-0.06, 0.035, 0.01, 12, None)  # 基准出场 (达标组合)

    t0 = time.time()
    m_full, ec_full = run_seg(selections, bt_cfg, stop, FULL_START, FULL_END, "全区间")
    m_train, _ = run_seg(selections, bt_cfg, stop, TRAIN_START, TRAIN_END, "训练期")
    m_oos, _ = run_seg(selections, bt_cfg, stop, OOS_START, OOS_END, "OOS")

    # 汇总
    print("\n" + "=" * 78)
    print("  三段对比 (各从 100万 起跑, 独立回测)")
    print("=" * 78)
    rows = []
    for tag, m in [("全区间", m_full), ("训练期", m_train), ("OOS", m_oos)]:
        ann = float(m.get("annualized_return", 0) or 0)
        dd = float(m.get("max_drawdown", 1) or 1)
        sharpe = float(m.get("sharpe_ratio", 0) or 0)
        trades = int(m.get("total_trades", 0) or 0)
        wr = float(m.get("win_rate", 0) or 0)
        ok = ann >= TARGET_ANN and abs(dd) <= MAX_DD and trades > 0
        rows.append({"段": tag, "年化": f"{ann*100:.2f}%", "回撤": f"{dd*100:.2f}%",
                     "夏普": f"{sharpe:.2f}", "交易": trades, "胜率": f"{wr*100:.1f}%",
                     "达标": "✓" if ok else ""})
    df = pd.DataFrame(rows)
    print(df.to_string(index=False))

    # 年度分解
    print("\n" + "=" * 78)
    print("  全区间年度收益分解 (抓'某年暴利撑平均')")
    print("=" * 78)
    yr = yearly_returns(ec_full)
    if len(yr):
        print(yr.to_string())
        # 判断分布是否均匀
        rets = yr["年度收益"].str.rstrip("%").astype(float)
        n_pos = (rets > 0).sum()
        n_big = (rets > 50).sum()  # 暴利年 (>50%)
        best, worst = rets.max(), rets.min()
        logger.info("正收益年 %d/%d, 暴利年(>50%%) %d, 最好 %.1f%%, 最差 %.1f%%",
                    n_pos, len(rets), n_big, best, worst)
        if n_big >= 1 and worst < 0:
            print(f"\n⚠️ 警示: 有 {n_big} 个暴利年(>50%)且存在亏损年({worst:.1f}%), 平均值可能被个别大年拉高")
        else:
            print(f"\n✅ 分布较均匀: 正收益 {n_pos}/{len(rets)} 年, 无单年暴利拉偏")
    print("=" * 78)

    # 判定结论
    oos_ann = float(m_oos.get("annualized_return", 0) or 0)
    train_ann = float(m_train.get("annualized_return", 0) or 0)
    print("\n判定:")
    print(f"  训练期 {train_ann*100:.2f}%  |  OOS {oos_ann*100:.2f}%")
    if oos_ann >= 0.20 and train_ann >= 0.15:
        print("  ✅ 稳健: OOS>=20% 且训练期>=15%, 非过拟合, 30.58% 可信")
    elif oos_ann >= 0.15:
        print("  ⚠️ 基本稳健: OOS 15-20%, 全区间 30.58% 略偏高但 OOS 未崩, 可接受")
    else:
        print("  ❌ 过拟合风险: OOS < 15%, 全区间 30.58% 疑似被某段行情撑高, 实盘预期需大幅下调")
    logger.info("验证完成, 总耗时 %.0fs", time.time() - t0)


if __name__ == "__main__":
    TURN_START = time.time()
    try:
        main()
    finally:
        logger.info("总耗时 %.0fs", time.time() - TURN_START)
