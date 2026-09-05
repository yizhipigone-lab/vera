# -*- coding: utf-8 -*-
"""recheck_open_t1.py
=====================
中证500 + MA金叉10/30 + 慢松快锁延时20d 的 **open_t1 口径**复验 (2026-08-21)。

口径: 信号日 T 收盘算信号 → T+1 首个可交易 bar 开盘价买入;
T+1 一字涨停 (OHLC 四价合一涨停) 拒买; T 日收盘涨停过滤自动关闭。

与 close_t (T 日收盘买入) 分开跑、分开标注, 不混排比较 (业务铁律)。

用法: python -X utf8 research/recheck_open_t1.py
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
    FACTORS, signals_to_selections, make_bt_cfg, make_stop, PERIOD,
)

logger = get_logger(__name__)

LONG_START, LONG_END = "20060101", "20260731"
UTYPE = "24"            # 中证500
MAX_POS, MIN_AMT, MAX_AMT, COOLDOWN = 5, 10000, 100000, 20


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


def main():
    print("=" * 100)
    print(f"  open_t1 口径: 中证500 + MA金叉10/30 + 慢松快锁延时20d | {LONG_START}~{LONG_END}")
    print("  口径 = T+1 首个可交易 bar 开盘价买入; T+1 一字涨停拒买; T日涨停过滤关闭")
    print("  (与 close_t 口径分开标注, 不混排)")
    print("=" * 100)

    sel = StockSelector({"formula_name": "_", "universe": {"type": UTYPE, "exclude_st": True},
                         "period": PERIOD, "dividend_type": 1})
    codes = sel.resolve_universe()
    logger.info("股票池 中证500 -> %d 只", len(codes))

    t0 = time.time()
    kl = DataFetcher.get_kline(codes, LONG_START, LONG_END, period=PERIOD,
                               dividend_type="front", use_cache=True)
    if not kl or "Close" not in kl:
        logger.error("取数失败"); return
    close = kl["Close"]; high = kl.get("High"); low = kl.get("Low"); vol = kl.get("Volume")
    logger.info("OHLCV 就绪 %d 股 × %d 日 (%.0fs)", close.shape[1], close.shape[0], time.time() - t0)

    sig = FACTORS["ma_cross"](close, high=high, low=low, vol=vol, fast=10, slow=30)
    selections = signals_to_selections(sig, "MA金叉10/30")
    logger.info("MA金叉10/30 信号 %d 条", len(selections))

    cfg = make_bt_cfg(MAX_POS, MIN_AMT, MAX_AMT)
    cfg["sell_cooldown_days"] = COOLDOWN
    cfg["entry_price_mode"] = "open_t1"        # ← 唯一口径差异
    stop = make_stop(-0.08, 0.04, 0.01, 20, None)

    res = BacktestEngine(cfg).run(selections=selections, start_time=LONG_START,
                                  end_time=LONG_END, stop_config=stop)
    m = res.get("metrics") or {}
    info = res.get("entry_mode_info") or {}
    print("\n[口径标注] entry_mode_info =", info)
    print(f"[open_t1] 累计={float(m.get('cumulative_return', 0))*100:+.2f}% "
          f"年化={float(m.get('annualized_return', 0))*100:.2f}% "
          f"回撤={float(m.get('max_drawdown', 0))*100:.2f}% "
          f"夏普={float(m.get('sharpe_ratio', 0)):.2f} "
          f"胜率={float(m.get('win_rate', 0))*100:.1f}% "
          f"交易={int(m.get('total_trades', 0))}")

    sa = strategy_annual(res.get("equity_curve"), res.get("trades"))
    print("\n年度明细 (open_t1):")
    print(sa.to_string(index=False))

    # 分段年化 (与档案 §3.1 同口径)
    rets = dict(zip(sa["年份"], sa["_ret"]))
    segs = [(range(2006, 2013), "段1 2006-2012"),
            (range(2013, 2020), "段2 2013-2019"),
            (range(2020, 2027), "段3 2020-2026H1")]
    print("\n分段年化 (open_t1):")
    for yrs, name in segs:
        prod = 1.0
        for y in yrs:
            prod *= (1 + rets.get(y, 0.0))
        n = len(yrs)
        print(f"  {name}: 累计 {(prod-1)*100:+.1f}% 年化 {(prod**(1/n)-1)*100:.2f}%")

    print(f"\n总耗时 %.0fs" % (time.time() - t0))


if __name__ == "__main__":
    TURN_START = time.time()
    try:
        main()
    finally:
        logger.info("总耗时 %.0fs", time.time() - TURN_START)
