# -*- coding: utf-8 -*-
"""annual_review.py
==================
中证500 + MA金叉10/30 + 慢松快锁延时20d (诚实基线激进档 17.68%) 的年度复盘。

输出:
  1. 策略各年: 年初权益/年末权益/年度收益/年内最大回撤/交易笔数/胜率
  2. 上证指数同期: 年度收益/MA250牛市占比/年末牛熊状态
  3. 对照清单 (策略 vs 大盘, 看哪些年策略跑赢/跑输, 牛熊特征)

用法: python -X utf8 research/annual_review.py
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

import numpy as np
import pandas as pd

from backtest.engine import BacktestEngine
from core.data_fetcher import DataFetcher
from selection.selector import StockSelector
from utils.logger import get_logger
from auto_strategy_optimizer import (
    FACTORS, signals_to_selections, make_bt_cfg, make_stop,
    PERIOD, CAPITAL,
)

logger = get_logger(__name__)

LONG_START, LONG_END = "20060101", "20260731"
UTYPE = "24"
MAX_POS, MIN_AMT, MAX_AMT, COOLDOWN = 5, 10000, 100000, 20


def build_bt_cfg() -> dict:
    cfg = make_bt_cfg(MAX_POS, MIN_AMT, MAX_AMT)
    cfg["sell_cooldown_days"] = COOLDOWN
    return cfg


def strategy_annual(equity_curve, trades):
    """策略按年: 收益/年内最大回撤/交易数/胜率。"""
    ec = equity_curve.copy()
    ec["date"] = pd.to_datetime(ec["date"])
    ec["year"] = ec["date"].dt.year
    tr = trades.copy() if trades is not None and len(trades) else pd.DataFrame()
    if len(tr):
        tr["exit_date"] = pd.to_datetime(tr["exit_date"])
        tr["year"] = tr["exit_date"].dt.year

    rows = []
    for year, g in ec.groupby("year"):
        eq = g.set_index("date")["equity"].sort_index()
        year_start = eq.iloc[0]
        year_end = eq.iloc[-1]
        ret = year_end / year_start - 1
        peak = eq.cummax()
        dd = (eq - peak) / peak
        max_dd = dd.min()
        n_tr = 0
        win_rate = np.nan
        if len(tr):
            ty = tr[tr["year"] == year]
            n_tr = len(ty)
            if "pnl" in ty.columns and n_tr:
                win_rate = (ty["pnl"] > 0).mean()
        rows.append({"年份": year, "年初权益": round(year_start),
                     "年末权益": round(year_end),
                     "年度收益": f"{ret*100:+.2f}%",
                     "年内最大回撤": f"{max_dd*100:.2f}%",
                     "_ret": ret, "_dd": max_dd,
                     "交易笔数": n_tr,
                     "胜率": f"{win_rate*100:.1f}%" if not np.isnan(win_rate) else "-"})
    return pd.DataFrame(rows)


def index_annual(idx_df):
    """上证按年: 收益/MA250牛市占比/年末状态。"""
    df = idx_df.copy()
    df.index = pd.to_datetime(df.index)
    # 注: 这是「收盘价 > MA250」的简化占比代理 (列名即口径), 不是牛熊判定;
    # 牛熊口径唯一真相源 = core/index_regime.py (MA250+20日相对斜率+样本门槛)。
    df["ma250"] = df["close"].rolling(250).mean()
    df["bull"] = df["close"] > df["ma250"]
    df["year"] = df.index.year
    rows = []
    for year, g in df.groupby("year"):
        ret = g["close"].iloc[-1] / g["close"].iloc[0] - 1
        bull_ratio = g["bull"].mean()
        last_bull = g["bull"].iloc[-1]
        rows.append({"年份": year, "上证年度收益": f"{ret*100:+.2f}%",
                     "_ret": ret,
                     "MA250牛市占比": f"{bull_ratio*100:.0f}%",
                     "年末状态": "牛" if last_bull else "熊"})
    return pd.DataFrame(rows)


def main():
    print("=" * 100)
    print(f"  年度复盘: 中证500 + MA金叉10/30 + 慢松快锁延时20d | {LONG_START}~{LONG_END}")
    print("=" * 100)

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
    stop = make_stop(-0.08, 0.04, 0.01, 20, None)   # 慢松快锁延时20d
    res = BacktestEngine(bt_cfg).run(selections=selections, start_time=LONG_START,
                                      end_time=LONG_END, stop_config=stop)
    m = res.get("metrics") or {}
    logger.info("全区间: 年化=%.2f%% 回撤=%.2f%% 夏普=%.2f 胜率=%.1f%% 交易=%d",
                float(m.get("annualized_return", 0))*100,
                float(m.get("max_drawdown", 0))*100,
                float(m.get("sharpe_ratio", 0)),
                float(m.get("win_rate", 0))*100,
                int(m.get("total_trades", 0)))

    ec = res.get("equity_curve")
    trades = res.get("trades")
    sa = strategy_annual(ec, trades)

    idx = DataFetcher.get_index_data("shanghai", "20040101", LONG_END, period="1d")
    ia = index_annual(idx)

    merged = pd.merge(sa, ia, on="年份", how="left")
    merged["策略-大盘"] = merged.apply(
        lambda r: f"{(r['_ret_x']-r['_ret_y'])*100:+.1f}%", axis=1)
    show = merged[["年份", "年度收益", "年内最大回撤", "交易笔数", "胜率",
                    "上证年度收益", "MA250牛市占比", "年末状态", "策略-大盘"]]

    print("\n" + "=" * 100)
    print("  策略 vs 大盘 年度对照 (按年份升序)")
    print("=" * 100)
    print(show.to_string(index=False))

    print("\n" + "=" * 100)
    print("  统计摘要")
    print("=" * 100)
    rets = merged["_ret_x"]
    idx_rets = merged["_ret_y"]
    print(f"策略: 正收益年 {(rets>0).sum()}/{len(rets)}, 最好 {rets.max()*100:+.1f}%, "
          f"最差 {rets.min()*100:+.1f}%, 年度均值 {rets.mean()*100:+.2f}%, 中位数 {rets.median()*100:+.2f}%")
    print(f"大盘: 正收益年 {(idx_rets>0).sum()}/{len(idx_rets)}, 最好 {idx_rets.max()*100:+.1f}%, "
          f"最差 {idx_rets.min()*100:+.1f}%, 年度均值 {idx_rets.mean()*100:+.2f}%, 中位数 {idx_rets.median()*100:+.2f}%")
    win_years = (rets > idx_rets).sum()
    print(f"策略跑赢大盘: {win_years}/{len(rets)} 年")
    # 大跌年(大盘<-20%)策略表现
    bad = merged[idx_rets < -0.20]
    if len(bad):
        print(f"\n大盘大跌年(<-20%) 策略表现:")
        for _, r in bad.iterrows():
            print(f"  {r['年份']}: 大盘 {r['上证年度收益']} → 策略 {r['年度收益']} (年内回撤 {r['年内最大回撤']})")

    os.makedirs("research/auto_optimizer_runs", exist_ok=True)
    out = "research/auto_optimizer_runs/annual_review.csv"
    show.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n清单已存: {out}")


if __name__ == "__main__":
    TURN_START = time.time()
    try:
        main()
    finally:
        logger.info("总耗时 %.0fs", time.time() - TURN_START)
