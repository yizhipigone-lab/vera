# -*- coding: utf-8 -*-
"""multi_factor_breakout.py
==========================
中证500 + MA金叉10/30 + 大盘择时过滤 → 冲年化 > 18.88% (20年真窗口 2006-2026)。

诊断 (2026-08-11):
  - 中证500 MA金叉10/30 是三池最优: 基准延时20d = 16.99%/-20.94%/夏普1.42 (诚实黄金组合)
  - 单因子天花板 17.68% (慢松快锁延时20d), 差 1.20% 达不到 18.88%
  - 最大出血点: 段2(2013-2019含15股灾+18熊市) 回撤 -47.46% (基准) / -48.84% (慢松快锁)
  - 段2是正收益(13-18%)但回撤深 → 切掉熊市的亏损交易, 年化有望上 18.88%

突破策略: 大盘择时过滤 (最低过拟合风险, 直击段2)
  - 上证指数当日收盘 > MA250 → bull, 允许开新仓
  - 上证 <= MA250 → bear, 禁止开新仓 (已有持仓仍按原止损出场, 不在底部砍仓)
  - 只作用于开仓层(selections 过滤), 不动出场逻辑

用法: python -X utf8 research/multi_factor_breakout.py
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
MAX_POS, MIN_AMT, MAX_AMT, COOLDOWN = 5, 10000, 100000, 20

SEGMENTS = [
    ("全区间", "20060101", "20260731"),
    ("段1(07牛08灾)", "20060101", "20121231"),
    ("段2(15牛灾)", "20130101", "20191231"),
    ("段3(21核心资产)", "20200101", "20260731"),
]


def build_bt_cfg() -> dict:
    cfg = make_bt_cfg(MAX_POS, MIN_AMT, MAX_AMT)
    cfg["sell_cooldown_days"] = COOLDOWN
    return cfg


def build_regime(idx_df: pd.DataFrame, ma: int, with_slope: bool = False) -> pd.Series:
    """上证 close > MA(ma) → bull。with_slope=True 再要求 MA 上行(20日斜率>0)。"""
    close = idx_df["close"]
    ma_line = close.rolling(ma).mean()
    bull = close > ma_line
    if with_slope:
        slope = ma_line.diff(20)
        bull = bull & (slope > 0)
    s = pd.Series(bull.values, index=close.index.strftime("%Y%m%d"))
    return s


def filter_by_regime(selections: pd.DataFrame, regime_map: pd.Series, tag: str) -> pd.DataFrame:
    if selections.empty:
        return selections
    mask = selections["select_date"].map(regime_map).fillna(False).astype(bool)
    kept = selections[mask]
    logger.info("[%s] 过滤前=%d → 过滤后=%d (砍 %d, %.1f%%)",
                tag, len(selections), len(kept), len(selections) - len(kept),
                100 * (1 - len(kept) / max(1, len(selections))))
    return kept


def run_bt(selections, bt_cfg, stop, start, end, tag):
    if selections is None or len(selections) == 0:
        logger.info("[%-22s] 信号空, 跳过", tag); return None
    res = BacktestEngine(bt_cfg).run(selections=selections, start_time=start,
                                      end_time=end, stop_config=stop)
    m = res.get("metrics") or {}
    ann = float(m.get("annualized_return", 0) or 0)
    dd = float(m.get("max_drawdown", 1) or 1)
    sharpe = float(m.get("sharpe_ratio", 0) or 0)
    trades = int(m.get("total_trades", 0) or 0)
    wr = float(m.get("win_rate", 0) or 0)
    ok = ann >= TARGET_ANN
    mark = "[年化达标>18.88%]" if ok else ""
    logger.info("[%-22s] %s~%s | 年化=%6.2f%% 回撤=%6.2f%% 夏普=%5.2f 交易=%-6d 胜率=%5.1f%% %s",
                tag, start, end, ann * 100, dd * 100, sharpe, trades, wr * 100, mark)
    return res, {"年化": ann, "回撤": dd, "夏普": sharpe, "交易": trades, "胜率": wr}


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
    print("=" * 96)
    print(f"  中证500 + MA金叉10/30 + 大盘择时过滤 | {LONG_START}~{LONG_END} | 目标年化>{TARGET_ANN:.2%}")
    print("=" * 96)

    # 1. 股票池 + MA金叉信号
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

    # 2. 上证指数 + 大盘 regime (从 2004 拿, 保 MA250 在 2006 初已有效)
    idx = DataFetcher.get_index_data("shanghai", "20040101", LONG_END, period="1d")
    if idx is None or len(idx) == 0 or "close" not in idx:
        logger.error("上证指数取数失败"); return
    logger.info("上证指数 %d 日 (%s ~ %s)", len(idx), idx.index[0].date(), idx.index[-1].date())
    reg_ma120 = build_regime(idx, 120)
    reg_ma250 = build_regime(idx, 250)
    reg_ma250_slope = build_regime(idx, 250, with_slope=True)

    bt_cfg = build_bt_cfg()
    # 出场: 基准延时20d (夏普1.42最优诚实组合)
    stop_base20 = make_stop(-0.06, 0.035, 0.01, 20, None)
    # 出场: 慢松快锁延时20d (年化最高)
    stop_lock20 = make_stop(-0.08, 0.04, 0.01, 20, None)

    # ===== A. 对照组(无过滤) vs MA250过滤, 全区间+分段 =====
    print("\n" + "=" * 96)
    print("  A. 对照(无过滤) vs MA250择时过滤 | 出场=基准延时20d")
    print("=" * 96)
    sel_none = selections
    sel_250 = filter_by_regime(selections, reg_ma250, "MA250过滤")
    logger.info("--- 对照组: 无过滤 ---")
    run_bt(sel_none, bt_cfg, stop_base20, LONG_START, LONG_END, "对照-全区间")
    logger.info("--- MA250择时过滤 ---")
    for tag, s, e in SEGMENTS:
        run_bt(sel_250, bt_cfg, stop_base20, s, e, f"MA250-{tag}")
    # MA250过滤的年度分解
    res_250 = BacktestEngine(bt_cfg).run(selections=sel_250, start_time=LONG_START,
                                          end_time=LONG_END, stop_config=stop_base20)
    ec = res_250.get("equity_curve")
    if ec is not None and len(ec):
        print("\n  MA250过滤 年度收益分解:")
        yr = yearly_returns(ec)
        if len(yr):
            print(yr.to_string())
            rets = yr["年度收益"].str.rstrip("%").astype(float)
            logger.info("正收益年 %d/%d, 最好 %.1f%%, 最差 %.1f%%, 年度均值 %.1f%%",
                        (rets > 0).sum(), len(rets), rets.max(), rets.min(), rets.mean())

    # ===== B. 过滤强度 × 出场 网格 (全区间, 找最优) =====
    print("\n" + "=" * 96)
    print("  B. 过滤强度 × 出场 网格 (全区间, 冲 18.88%)")
    print("=" * 96)
    grid = [
        ("无过滤", sel_none, stop_base20, "无过滤+基准延时20d"),
        ("MA120", filter_by_regime(selections, reg_ma120, "MA120"), stop_base20, "MA120+基准延时20d"),
        ("MA250", sel_250, stop_base20, "MA250+基准延时20d"),
        ("MA250+斜率", filter_by_regime(selections, reg_ma250_slope, "MA250+斜率"), stop_base20, "MA250+斜率+基准延时20d"),
        ("无过滤", sel_none, stop_lock20, "无过滤+慢松快锁延时20d"),
        ("MA250", sel_250, stop_lock20, "MA250+慢松快锁延时20d"),
        ("MA250+斜率", filter_by_regime(selections, reg_ma250_slope, "MA250+斜率2"), stop_lock20, "MA250+斜率+慢松快锁延时20d"),
    ]
    rows = []
    for _, s, st, name in grid:
        if s is None or len(s) == 0:
            continue
        res = BacktestEngine(bt_cfg).run(selections=s, start_time=LONG_START,
                                          end_time=LONG_END, stop_config=st)
        m = res.get("metrics") or {}
        ann = float(m.get("annualized_return", 0) or 0)
        dd = float(m.get("max_drawdown", 1) or 1)
        sharpe = float(m.get("sharpe_ratio", 0) or 0)
        trades = int(m.get("total_trades", 0) or 0)
        wr = float(m.get("win_rate", 0) or 0)
        ok_ann = ann >= TARGET_ANN
        ok_dd = abs(dd) <= MAX_DD
        mark = "[双达标]" if (ok_ann and ok_dd) else ("[年化达标]" if ok_ann else "")
        logger.info("%-34s | 信号=%-6d 年化=%6.2f%% 回撤=%6.2f%% 夏普=%5.2f 交易=%-6d 胜率=%5.1f%% %s",
                    name, len(s), ann * 100, dd * 100, sharpe, trades, wr * 100, mark)
        rows.append({"组合": name, "信号": len(s), "年化": f"{ann*100:.2f}%",
                     "回撤": f"{dd*100:.2f}%", "夏普": f"{sharpe:.2f}",
                     "交易": trades, "胜率": f"{wr*100:.1f}%",
                     "年化达标": "✓" if ok_ann else "", "双达标": "✓" if (ok_ann and ok_dd) else ""})

    print("\n" + "=" * 96)
    print("  多因子突破结果 (按年化降序)")
    print("=" * 96)
    if rows:
        df = pd.DataFrame(rows)
        df["_s"] = df["年化"].str.rstrip("%").astype(float)
        df = df.sort_values("_s", ascending=False).drop(columns="_s")
        print(df.to_string(index=False))
        n_ann = (df["年化达标"] == "✓").sum()
        n_both = (df["双达标"] == "✓").sum()
        print(f"\n年化达标(>18.88%): {n_ann}/{len(df)}   双达标(年化+回撤<=30%): {n_both}/{len(df)}")
        if len(df):
            best = df.iloc[0]
            print(f"最优: {best['组合']} -> 年化{best['年化']} 回撤{best['回撤']} 夏普{best['夏普']}")
    print("=" * 96)
    logger.info("多因子突破完成")


if __name__ == "__main__":
    TURN_START = time.time()
    try:
        main()
    finally:
        logger.info("总耗时 %.0fs", time.time() - TURN_START)
