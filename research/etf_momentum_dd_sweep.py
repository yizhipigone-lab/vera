"""创业板50 × 纳指 4周动量 —— 降回撤方法全扫 (2026-08-20)。

基线(指数代理版 2014起): +1773% / 回撤 -45.7% / Calmar 0.64, 亏钱年是 2016(-26%)、2022(-14.8%)。
逐类试降回撤:
  1. 移动止损: 持仓腿从「持仓期最高收盘」回撤 X% → 空仓 (X = 10/15/20/25%)
  2. 短均线过滤: 选中腿收盘 < N日均线 → 空仓 (N = 50/100; 200已证无用)
  3. 动量阈值: 动量须 > T 才持有 (T = 0/3%/5%)
  4. 更短动量窗口: 2周 (4周基线)
统一口径: 周五收盘算信号、周一开盘成交、换腿万10、现金年化2%。
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
CASH_ANN = 0.02


def run(df_close, df_open, mom_weeks=4, stop_x=None, ma_window=None, mom_thresh=0.0):
    dates = df_close.index
    fridays = [d for d in dates if d.weekday() == 4]
    mondays = [d for d in dates if d.weekday() == 0]
    cyb, nas = df_close.columns[0], df_close.columns[1]

    mom_rows = []
    for i, f in enumerate(fridays):
        if i >= mom_weeks:
            f0 = fridays[i - mom_weeks]
            mom_rows.append([df_close.at[f, cyb] / df_close.at[f0, cyb] - 1.0,
                             df_close.at[f, nas] / df_close.at[f0, nas] - 1.0])
        else:
            mom_rows.append([np.nan, np.nan])
    mom = pd.DataFrame(mom_rows, index=fridays, columns=[cyb, nas])
    ma = {a: df_close[a].rolling(ma_window).mean() for a in (cyb, nas)} if ma_window else {}

    pos = None
    entry_high = {}
    switches = 0
    weekly = {}
    for i in range(len(mondays) - 1):
        M = mondays[i]
        M_next = mondays[i + 1]
        cand = [f for f in mom.index if f <= M]
        f = cand[-1] if cand else None
        # 移动止损: 持仓腿从持仓期最高收盘回撤 X% → 空仓
        stop_off = False
        if pos not in (None, "cash") and stop_x and f is not None:
            c = df_close.at[f, pos]
            entry_high[pos] = max(entry_high.get(pos, c), c)
            if c < entry_high[pos] * (1.0 - stop_x):
                stop_off = True
        target = None
        if f is not None:
            m_c, m_n = mom.loc[f]
            if not (np.isnan(m_c) or np.isnan(m_n)):
                hi = max(m_c, m_n)
                if hi > mom_thresh:
                    pick = cyb if m_c >= m_n else nas
                    if ma_window:
                        maval = ma[pick].reindex(mom.index).loc[f]
                        target = "cash" if (not np.isnan(maval) and df_close.at[f, pick] < maval) else pick
                    else:
                        target = pick
                else:
                    target = "cash"
            else:
                target = "cash"
        if target is None:
            target = "cash"
        if stop_off:
            target = "cash"
        if target != pos:
            switches += 1
            pos = target
            entry_high = {pos: df_close.at[f, pos]} if (pos != "cash" and f is not None) else {}
        # 周收益 (周一开盘 → 下周一开盘)
        if pos == "cash":
            r = (1.0 + CASH_ANN) ** (5.0 / 252.0) - 1.0
        else:
            r = float(df_open.at[M_next, pos] / df_open.at[M, pos] - 1.0)
        weekly[M] = r
    w = pd.Series(weekly)
    curve = np.cumprod(1.0 + w.values)
    total = curve[-1] - 1.0
    ny = len(w) * 5 / 252.0
    ann = (1.0 + total) ** (1.0 / ny) - 1.0
    dd = float((curve / np.maximum.accumulate(curve) - 1.0).min())
    calmar = ann / abs(dd) if dd < 0 else 0.0
    sharpe = w.mean() / w.std(ddof=1) * np.sqrt(52)
    return dict(total=total, ann=ann, dd=dd, calmar=calmar, sharpe=sharpe,
                switches=switches), w


def main():
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

    variants = [
        ("基线 4周动量", dict()),
        ("移动止损 10%", dict(stop_x=0.10)),
        ("移动止损 15%", dict(stop_x=0.15)),
        ("移动止损 20%", dict(stop_x=0.20)),
        ("移动止损 25%", dict(stop_x=0.25)),
        ("50日线过滤", dict(ma_window=50)),
        ("100日线过滤", dict(ma_window=100)),
        ("动量阈值 3%", dict(mom_thresh=0.03)),
        ("动量阈值 5%", dict(mom_thresh=0.05)),
        ("2周动量", dict(mom_weeks=2)),
        ("2周动量+止损15%", dict(mom_weeks=2, stop_x=0.15)),
        ("止损15%+阈值3%", dict(stop_x=0.15, mom_thresh=0.03)),
    ]
    rows = []
    for name, kw in variants:
        m, w = run(dfc, dfo, **kw)
        rows.append((name, m["total"], m["ann"], m["dd"], m["calmar"], m["sharpe"], m["switches"]))
        print(f"{name:<16} 累计{m['total']*100:>+8.1f}% 年化{m['ann']*100:>+6.1f}% "
              f"回撤{m['dd']*100:>7.1f}% Calmar{m['calmar']:>6.2f} 夏普{m['sharpe']:>5.2f} "
              f"换腿{m['switches']:>4}")

    print("\n=== 按回撤排序(越小越好) ===")
    for name, t, a, d, ca, sh, sw in sorted(rows, key=lambda x: x[3]):
        print(f"{name:<16} 回撤{d*100:>7.1f}%  累计{t*100:>+8.1f}%  Calmar{ca:>6.2f}")


if __name__ == "__main__":
    main()
