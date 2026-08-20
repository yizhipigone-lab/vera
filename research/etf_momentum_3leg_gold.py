"""黄金相关性 + 三腿动量轮动 (创业板50 × 纳指 × 黄金) (2026-08-20)。

验证: 2025年起创业板50与纳指同频(相关~0.59)。黄金是否仍低相关/负相关?
若黄金不同频, 三腿动量轮动能否治"同频时代"?
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
GOLD = "518880.SH"
COST = 0.001
CASH_DAILY = (1.02) ** (1.0 / 252.0) - 1.0
MOM_WEEKS = 4


def corr_table(ret, pairs, window=120):
    out = {}
    for a, b in pairs:
        r = ret[a].rolling(window).corr(ret[b])
        seg24 = r[(r.index.year == 2024)]
        seg25 = r[(r.index.year >= 2025)]
        out[(a, b)] = (seg24.mean(), seg25.mean(), r.iloc[-1])
    return out


def momentum_run(df_close, legs, stop_x=None, label=""):
    dates = df_close.index
    ret = df_close.pct_change().fillna(0.0)
    fridays = [d for d in dates if d.weekday() == 4]
    mondays = set(d for d in dates if d.weekday() == 0)
    mom = {}
    for a in legs:
        s = []
        for i, f in enumerate(fridays):
            if i >= MOM_WEEKS:
                f0 = fridays[i - MOM_WEEKS]
                s.append(df_close.at[f, a] / df_close.at[f0, a] - 1.0)
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
        if valid[best] > 0:
            return best
        return "cash"

    pos = "cash"
    entry_high = 0.0
    switches = 0
    curve = np.empty(len(dates))
    equity = 1.0
    curve[0] = 1.0
    for i in range(len(dates) - 1):
        t = dates[i]
        t_next = dates[i + 1]
        if pos == "cash":
            equity *= (1.0 + CASH_DAILY)
        else:
            equity *= (1.0 + ret.at[t_next, pos])
        curve[i + 1] = equity
        if pos != "cash":
            c = df_close.at[t, pos]
            entry_high = max(entry_high, c)
            if stop_x and c < entry_high * (1.0 - stop_x):
                pos = "cash"; switches += 1; equity *= (1.0 - COST); entry_high = 0.0
        if t in mondays:
            target = decide(t)
            if target != pos:
                pos = target; switches += 1; equity *= (1.0 - COST)
                entry_high = df_close.at[t, target] if target != "cash" else 0.0
    total = curve[-1] - 1.0
    n_years = len(dates) / 252.0
    ann = (1.0 + total) ** (1.0 / n_years) - 1.0
    dd = float((curve / np.maximum.accumulate(curve) - 1.0).min())
    calmar = ann / abs(dd) if dd < 0 else 0.0
    sharpe = 0.0
    dr = pd.Series(curve).pct_change().dropna()
    if dr.std() > 0:
        sharpe = dr.mean() / dr.std() * np.sqrt(252)
    print(f"{label:<28} 累计{total*100:>+8.1f}% 年化{ann*100:>+6.1f}% "
          f"回撤{dd*100:>7.1f}% Calmar{calmar:>6.2f} 夏普{sharpe:>5.2f} 换腿{switches:>4}")


def main():
    kl = DataFetcher.get_kline([NAS, GOLD, "159949.SZ"], "20130101", "20260819",
                               period="1d", dividend_type="front",
                               use_cache=True, force_refresh=True)
    close = kl["Close"]
    import akshare as ak
    idx = ak.stock_zh_index_daily_tx(symbol="sz399673")
    idx = idx.set_index("date")
    idx.index = pd.to_datetime(idx.index)
    nas = close[NAS].dropna()
    gold = close[GOLD].dropna()
    dfc = pd.concat([idx["close"].rename("cyb"), nas, gold], axis=1, join="inner").dropna()
    dfc.columns = ["cyb", "nas", "gold"]

    ret = dfc.pct_change().dropna()
    print("=== 120日滚动相关性 (2024 均值 / 2025至今 均值 / 最新) ===")
    for (a, b), (m24, m25, last) in corr_table(ret, [("cyb", "nas"), ("cyb", "gold"), ("nas", "gold")]).items():
        print(f"  {a:>4} vs {b:<4}  {m24:+.3f} / {m25:+.3f} / {last:+.3f}")

    print("\n=== 动量轮动对比 (2014-06 起, 周频+日频15%止损) ===")
    # 两腿 (基线)
    two = dfc[["cyb", "nas"]]
    momentum_run(two, ["cyb", "nas"], stop_x=None, label="两腿(无止损)")
    momentum_run(two, ["cyb", "nas"], stop_x=0.15, label="两腿(日频止损15%)")
    # 三腿 (加黄金)
    momentum_run(dfc, ["cyb", "nas", "gold"], stop_x=None, label="三腿含黄金(无止损)")
    momentum_run(dfc, ["cyb", "nas", "gold"], stop_x=0.15, label="三腿含黄金(日频止损15%)")


if __name__ == "__main__":
    main()
