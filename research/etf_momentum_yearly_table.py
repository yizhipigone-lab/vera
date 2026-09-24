"""两腿+日频止损15% —— 逐年金额表 (2026-08-20)。

策略: 创业板50指数(399673) × 纳指ETF(513100), 4周动量(周五收盘算),
下周一调仓, 谁动量高且>0全仓谁, 都≤0空仓现金2%, 换腿万10,
日频移动止损: 持仓腿从持仓期最高收盘回撤15%当天撤(现金)。
初始本金 100 万, 按日历年输出 年初金额/年末金额/当年收益/当年收益率。
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
COST = 0.001
CASH_DAILY = (1.02) ** (1.0 / 252.0) - 1.0
MOM_WEEKS = 4
STOP_X = 0.15
INIT = 1_000_000.0


def main():
    kl = DataFetcher.get_kline([NAS, "159949.SZ"], "20130101", "20260819",
                               period="1d", dividend_type="front",
                               use_cache=True, force_refresh=True)
    close = kl["Close"]
    import akshare as ak
    idx = ak.stock_zh_index_daily_tx(symbol="sz399673")
    idx = idx.set_index("date")
    idx.index = pd.to_datetime(idx.index)
    nas = close[NAS].dropna()
    dfc = pd.concat([idx["close"].rename("cyb"), nas], axis=1, join="inner").dropna()

    dates = dfc.index
    cyb, nas = dfc.columns[0], dfc.columns[1]
    ret = dfc.pct_change().fillna(0.0)
    fridays = [d for d in dates if d.weekday() == 4]
    mondays = set(d for d in dates if d.weekday() == 0)

    mom = {}
    for a in (cyb, nas):
        s = []
        for i, f in enumerate(fridays):
            if i >= MOM_WEEKS:
                f0 = fridays[i - MOM_WEEKS]
                s.append(dfc.at[f, a] / dfc.at[f0, a] - 1.0)
            else:
                s.append(np.nan)
        mom[a] = pd.Series(s, index=fridays)
    mom_df = pd.DataFrame(mom)

    def decide(t):
        cand = [f for f in mom_df.index if f <= t]
        if not cand:
            return "cash"
        row = mom_df.loc[cand[-1]]
        valid = row[row.notna()]
        if valid.empty:
            return "cash"
        best = valid.idxmax()
        return best if valid[best] > 0 else "cash"

    pos = "cash"
    entry_high = 0.0
    equity = INIT
    eq_series = pd.Series(index=dates, dtype=float)
    eq_series.iloc[0] = INIT
    for i in range(len(dates) - 1):
        t = dates[i]
        t_next = dates[i + 1]
        if pos == "cash":
            equity *= (1.0 + CASH_DAILY)
        else:
            equity *= (1.0 + ret.at[t_next, pos])
        eq_series.iloc[i + 1] = equity
        if pos != "cash":
            c = dfc.at[t, pos]
            entry_high = max(entry_high, c)
            if c < entry_high * (1.0 - STOP_X):
                pos = "cash"
                equity *= (1.0 - COST)
                entry_high = 0.0
        if t in mondays:
            target = decide(t)
            if target != pos:
                pos = target
                equity *= (1.0 - COST)
                entry_high = dfc.at[t, target] if target != "cash" else 0.0

    eq_series = eq_series.dropna()
    years = sorted(set(eq_series.index.year))
    print(f"{'年份':<6}{'年初金额':>14}{'年末金额':>14}{'当年收益':>13}{'当年收益率':>11}")
    print("-" * 60)
    prev_end = INIT
    for y in years:
        seg = eq_series[eq_series.index.year == y]
        start_val = seg.iloc[0]
        end_val = seg.iloc[-1]
        profit = end_val - start_val
        ret_y = end_val / start_val - 1.0
        print(f"{y:<6}{start_val:>14,.0f}{end_val:>14,.0f}{profit:>+13,.0f}{ret_y*100:>+10.1f}%")
    total = eq_series.iloc[-1] / INIT - 1.0
    print("-" * 60)
    print(f"累计: {total*100:+.1f}%  (100万 → {eq_series.iloc[-1]:,.0f} 元)")


if __name__ == "__main__":
    main()
