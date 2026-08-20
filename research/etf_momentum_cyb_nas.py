"""创业板50 × 纳指 4周动量轮动回测 (2026-08-19 用户拍板规则)。

规则 (唯一真相):
  - 信号: 每周五收盘算, 动量 = 今日收盘 / 4周前周五收盘 - 1 (两只各自算)
  - 选腿: 动量高 且 >0 → 全仓持谁; 两只都 ≤0 → 空仓现金 (年化 2%)
  - 成交: 下周一开盘调仓
  - 成本: 换腿万10 (0.1%, 卖旧买新合计, 每次切换扣一次)
  - 频率: 周频, 预期约 14 次换腿/年

数据: TDX 前复权日线 (Open+Close), 159949(创业板50ETF) 与 513100(纳指ETF)。
窗口: 两只都有数据的公共区间 (159949 2016-08 起)。
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

CYB, NAS = "159949.SZ", "513100.SH"
START, END = "20160101", "20260819"
MOM_WEEKS = 4          # 4 周动量
COST = 0.001           # 万10 = 0.1% / 次换腿
CASH_ANN = 0.02        # 现金年化 2%


def annualize(total, n_years):
    return (1.0 + total) ** (1.0 / n_years) - 1.0 if n_years > 0 else 0.0


def metrics(weekly_ret: np.ndarray) -> dict:
    n = len(weekly_ret)
    curve = np.cumprod(1.0 + weekly_ret)
    total = curve[-1] - 1.0
    n_years = n * 5 / 252.0          # 每周约 5 交易日
    ann = annualize(total, n_years)
    dd = float((curve / np.maximum.accumulate(curve) - 1.0).min())
    sd = weekly_ret.std(ddof=1)
    sharpe = (weekly_ret.mean() / sd * np.sqrt(52)) if sd > 0 else 0.0
    calmar = (ann / abs(dd)) if dd < 0 else 0.0
    win = float((weekly_ret > 0).mean())
    return {"total": total, "ann": ann, "maxdd": dd, "sharpe": sharpe,
            "calmar": calmar, "win": win, "n_years": n_years, "curve": curve}


def main():
    kl = DataFetcher.get_kline([CYB, NAS], START, END, period="1d",
                               dividend_type="front", use_cache=True)
    close = kl["Close"]
    openp = kl["Open"]

    # 公共有效区间 (两只都非 NaN)
    df_close = close[[CYB, NAS]].dropna()
    df_open = openp[[CYB, NAS]].reindex(df_close.index)
    dates = df_close.index
    print(f"窗口 {dates[0].date()} ~ {dates[-1].date()}, {len(dates)} 交易日")

    fridays = [d for d in dates if d.weekday() == 4]
    mondays = [d for d in dates if d.weekday() == 0]

    # 每周五的两腿动量 (i>=MOM_WEEKS 才有信号)
    mom = {a: [] for a in (CYB, NAS)}
    for i, f in enumerate(fridays):
        if i >= MOM_WEEKS:
            f0 = fridays[i - MOM_WEEKS]
            for a in (CYB, NAS):
                mom[a].append(float(df_close.at[f, a] / df_close.at[f0, a] - 1.0))
        else:
            for a in (CYB, NAS):
                mom[a].append(np.nan)
    mom_f = pd.DataFrame(mom, index=fridays)

    # 逐周回测: 周从「周一开盘」持到「下周一开盘」
    # 目标 = 最近一个(≤本周一)周五的信号
    sig_fridays = list(mom_f.index)
    pos = None
    equity = 1.0
    switches = 0
    weekly = []
    for i in range(len(mondays) - 1):
        M = mondays[i]
        M_next = mondays[i + 1]
        # 找到 ≤M 的最近周五
        cand = [f for f in sig_fridays if f <= M]
        target = None
        if cand:
            row = mom_f.loc[cand[-1]]
            m_c, m_n = row[CYB], row[NAS]
            if not (np.isnan(m_c) or np.isnan(m_n)):
                if m_c > 0 or m_n > 0:
                    target = CYB if m_c >= m_n else NAS
                else:
                    target = "cash"
        if target is None:
            target = "cash"   # 数据不足 → 现金
        if target != pos:
            switches += 1
            equity *= (1.0 - COST)
            pos = target
        # 周收益: 周一开盘 → 下周一开盘
        if pos == "cash":
            r = (1.0 + CASH_ANN) ** (5.0 / 252.0) - 1.0
        else:
            r = float(df_open.at[M_next, pos] / df_open.at[M, pos] - 1.0)
        weekly.append(r)
        equity *= (1.0 + r)

    weekly = np.array(weekly)
    m = metrics(weekly)
    n_years = m["n_years"]

    # 同窗基准: 买入持有 (周一开盘买, 期末卖)
    def bh(code):
        c = df_close[code]
        return float(c.iloc[-1] / c.iloc[0] - 1.0)
    bh_cyb = bh(CYB)
    bh_nas = bh(NAS)

    print("\n=== 4周动量轮动 (创业板50 × 纳指) ===")
    print(f"累计 {m['total']*100:+.1f}%  年化 {m['ann']*100:+.1f}%  回撤 {m['maxdd']*100:.1f}%")
    print(f"夏普(周) {m['sharpe']:.2f}  Calmar {m['calmar']:.2f}  周胜率 {m['win']*100:.0f}%")
    print(f"换腿 {switches} 次, 平均 {switches/n_years:.1f} 次/年")
    print(f"\n=== 同窗买入持有 ===")
    print(f"创业板50: {bh_cyb*100:+.1f}%   纳指: {bh_nas*100:+.1f}%")


if __name__ == "__main__":
    main()
