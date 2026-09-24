"""创业板50 × 纳指 4周动量轮动 —— 逐年拆解 + 200日均线顶部保护 (2026-08-20)。

问题: 长窗口指数代理版回撤 -45.7%, 凶手是 2015 股灾。本脚本:
  1. 逐年拆收益, 看 2015 到底怎么亏的;
  2. 加「200日均线过滤」顶部保护: 动量选中某腿后, 若该腿收盘 < 自己 200 日均线,
     则强制空仓现金 (跌破趋势就撤, 不给熊市接刀)。看回撤能否压下来。
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
MA_WIN = 200


def run_momentum(df_close, df_open, ma_filter):
    """返回 (周收益 Series[日期], 换腿次数, 持仓时间线 dict)。"""
    dates = df_close.index
    fridays = [d for d in dates if d.weekday() == 4]
    mondays = [d for d in dates if d.weekday() == 0]
    cyb, nas = df_close.columns[0], df_close.columns[1]

    mom_rows = []
    for i, f in enumerate(fridays):
        if i >= MOM_WEEKS:
            f0 = fridays[i - MOM_WEEKS]
            mom_rows.append([df_close.at[f, cyb] / df_close.at[f0, cyb] - 1.0,
                             df_close.at[f, nas] / df_close.at[f0, nas] - 1.0])
        else:
            mom_rows.append([np.nan, np.nan])
    mom = pd.DataFrame(mom_rows, index=fridays, columns=[cyb, nas])

    ma = {a: df_close[a].rolling(MA_WIN).mean() for a in (cyb, nas)}

    pos = None
    equity = 1.0
    switches = 0
    weekly = {}
    for i in range(len(mondays) - 1):
        M = mondays[i]
        M_next = mondays[i + 1]
        cand = [f for f in mom.index if f <= M]
        target = None
        if cand:
            m_c, m_n = mom.loc[cand[-1]]
            if not (np.isnan(m_c) or np.isnan(m_n)):
                if m_c > 0 or m_n > 0:
                    pick = cyb if m_c >= m_n else nas
                    if ma_filter:
                        # 顶部保护: 选中腿跌破自己 200 日线 → 空仓
                        maval = ma[pick].reindex(mom.index).loc[cand[-1]]
                        if not np.isnan(maval) and df_close.at[cand[-1], pick] < maval:
                            target = "cash"
                        else:
                            target = pick
                    else:
                        target = pick
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
        weekly[M] = r
        equity *= (1.0 + r)
    return pd.Series(weekly), switches


def report(name, weekly):
    curve = np.cumprod(1.0 + weekly.values)
    total = curve[-1] - 1.0
    n_years = len(weekly) * 5 / 252.0
    ann = (1.0 + total) ** (1.0 / n_years) - 1.0
    dd = float((curve / np.maximum.accumulate(curve) - 1.0).min())
    sharpe = weekly.mean() / weekly.std(ddof=1) * np.sqrt(52)
    calmar = ann / abs(dd) if dd < 0 else 0.0
    print(f"\n=== {name} ===")
    print(f"累计 {total*100:+.1f}%  年化 {ann*100:+.1f}%  回撤 {dd*100:.1f}%  "
          f"夏普 {sharpe:.2f}  Calmar {calmar:.2f}  周胜率 {(weekly>0).mean()*100:.0f}%")


def yearly(name, weekly):
    """按日历年拆收益。"""
    eq = (1.0 + weekly).cumprod()
    ye = eq.resample("YE").last()
    yr = ye.pct_change() * 100
    yr.iloc[0] = (ye.iloc[0] - 1.0) * 100
    print(f"\n--- {name} 逐年收益 ---")
    for y, v in yr.items():
        print(f"  {y.year}: {v:+.1f}%")


def main():
    # 指数代理版 (2014-06 起) —— 用 399673 覆盖最久历史, 看 2015
    kl = DataFetcher.get_kline([NAS, "159949.SZ"], "20130101", "20260819",
                               period="1d", dividend_type="front",
                               use_cache=True, force_refresh=True)
    close = kl["Close"]
    openp = kl["Open"]

    import akshare as ak
    idx = ak.stock_zh_index_daily_tx(symbol="sz399673")
    idx = idx.set_index("date")
    idx.index = pd.to_datetime(idx.index)
    nas_c = close[NAS].dropna()
    nas_o = openp[NAS].reindex(nas_c.index)
    dfc = pd.concat([idx["close"].rename("399673"), nas_c], axis=1, join="inner").dropna()
    dfo = pd.concat([idx["open"].rename("399673"), nas_o], axis=1).reindex(dfc.index)

    w_base, s_base = run_momentum(dfc, dfo, ma_filter=False)
    w_filt, s_filt = run_momentum(dfc, dfo, ma_filter=True)

    report("基准(无保护) 创业板50指数×纳指", w_base)
    yearly("基准", w_base)
    report("加200日线过滤 创业板50指数×纳指", w_filt)
    yearly("加过滤", w_filt)
    print(f"\n换腿: 基准 {s_base} 次 vs 过滤 {s_filt} 次")


if __name__ == "__main__":
    main()
