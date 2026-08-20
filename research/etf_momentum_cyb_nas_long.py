"""创业板50 × 纳指 4周动量轮动 —— 长窗口版 (2026-08-20)。

规则同 etf_momentum_cyb_nas.py (周五收盘算4周动量, 周一开盘成交, 谁动量高且>0
全仓谁, 都≤0空仓现金年化2%, 换腿万10)。

两个窗口:
  A. ETF版: 创业板50ETF(159949, 2016-08起) × 纳指ETF(513100)  —— 真实可交易
  B. 指数代理版: 创业板50指数(399673, 2014-06起) × 纳指ETF(513100) —— 最长历史
     (159949 上市前的 2014-2016 用指数代替, 与 rotation_backtest 同口径)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np
import pandas as pd

from core.data_fetcher import DataFetcher

NAS = "513100.SH"
MOM_WEEKS = 4
COST = 0.001
CASH_ANN = 0.02


def run_momentum(df_close, df_open, name):
    dates = df_close.index
    fridays = [d for d in dates if d.weekday() == 4]
    mondays = [d for d in dates if d.weekday() == 0]
    cols = list(df_close.columns)  # [cyb, nas]
    cyb, nas = cols[0], cols[1]

    mom_rows = []
    for i, f in enumerate(fridays):
        if i >= MOM_WEEKS:
            f0 = fridays[i - MOM_WEEKS]
            mom_rows.append([df_close.at[f, cyb] / df_close.at[f0, cyb] - 1.0,
                             df_close.at[f, nas] / df_close.at[f0, nas] - 1.0])
        else:
            mom_rows.append([np.nan, np.nan])
    mom = pd.DataFrame(mom_rows, index=fridays, columns=[cyb, nas])

    pos = None
    equity = 1.0
    switches = 0
    weekly = []
    for i in range(len(mondays) - 1):
        M = mondays[i]
        M_next = mondays[i + 1]
        cand = [f for f in mom.index if f <= M]
        target = None
        if cand:
            m_c, m_n = mom.loc[cand[-1]]
            if not (np.isnan(m_c) or np.isnan(m_n)):
                if m_c > 0 or m_n > 0:
                    target = cyb if m_c >= m_n else nas
                else:
                    target = "cash"
        if target is None:
            target = "cash"
        if target != pos:
            switches += 1
            equity *= (1.0 - COST)
            pos = target
        if pos == "cash":
            r = (1.0 + CASH_ANN) ** (5.0 / 252.0) - 1.0
        else:
            r = float(df_open.at[M_next, pos] / df_open.at[M, pos] - 1.0)
        weekly.append(r)
        equity *= (1.0 + r)

    weekly = np.array(weekly)
    curve = np.cumprod(1.0 + weekly)
    total = curve[-1] - 1.0
    n_years = len(weekly) * 5 / 252.0
    ann = (1.0 + total) ** (1.0 / n_years) - 1.0
    dd = float((curve / np.maximum.accumulate(curve) - 1.0).min())
    sharpe = weekly.mean() / weekly.std(ddof=1) * np.sqrt(52)
    calmar = ann / abs(dd) if dd < 0 else 0.0
    win = float((weekly > 0).mean())
    print(f"\n=== {name} ===")
    print(f"窗口 {dates[0].date()} ~ {dates[-1].date()} ({n_years:.1f} 年)")
    print(f"累计 {total*100:+.1f}%  年化 {ann*100:+.1f}%  回撤 {dd*100:.1f}%")
    print(f"夏普(周) {sharpe:.2f}  Calmar {calmar:.2f}  周胜率 {win*100:.0f}%")
    print(f"换腿 {switches} 次 = {switches/n_years:.1f} 次/年")


def main():
    # 纳指 ETF (TDX 前复权, 全历史)
    kl = DataFetcher.get_kline([NAS, "159949.SZ"], "20130101", "20260819",
                               period="1d", dividend_type="front",
                               use_cache=True, force_refresh=True)
    close = kl["Close"]
    openp = kl["Open"]

    # A. ETF版: 159949 × 513100
    dfc = close[["159949.SZ", NAS]].dropna()
    dfo = openp[["159949.SZ", NAS]].reindex(dfc.index)
    run_momentum(dfc, dfo, "A. ETF版 创业板50ETF(159949) × 纳指ETF")

    # B. 指数代理版: 399673 × 513100 (2014-06 起)
    import akshare as ak
    idx = ak.stock_zh_index_daily_tx(symbol="sz399673")
    idx = idx.set_index("date")
    idx.index = pd.to_datetime(idx.index)
    idx_close = idx["close"]
    idx_open = idx["open"]
    nas_c = close[NAS].dropna()
    nas_o = openp[NAS].reindex(nas_c.index)
    dfc2 = pd.concat([idx_close.rename("399673"), nas_c], axis=1, join="inner").dropna()
    dfo2 = pd.concat([idx_open.rename("399673"), nas_o], axis=1).reindex(dfc2.index)
    run_momentum(dfc2, dfo2, "B. 指数代理版 创业板50指数(399673) × 纳指ETF")


if __name__ == "__main__":
    main()
