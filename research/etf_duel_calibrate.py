"""校准: 用旧 etf_hedge_sweep_2014.rotation() 原版函数(无成本)跑本脚本数据,
对比旧报告 +1923.1%/-29.0%, 隔离数据差异 vs 实现差异。"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

import numpy as np
import pandas as pd
from trade.rotation import STATE_RATIOS, compute_signal

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "etf_duel_ma20_vs_momentum", "data")
MA, HIGH, DD = 20, 250, 0.20

def rotation(close_cyb, close_h):
    """旧研究原版 (etf_hedge_sweep_2014.py 逐行复制, 无成本)。"""
    df = pd.concat([close_cyb, close_h], axis=1, join="inner").dropna()
    if len(df) < HIGH + 5:
        return None
    cx = df.iloc[:, 0].values
    ch = df.iloc[:, 1].values
    rc = np.r_[0.0, cx[1:] / cx[:-1] - 1.0]
    rh = np.r_[0.0, ch[1:] / ch[:-1] - 1.0]
    n = len(df)
    curve = np.empty(n)
    e = 1.0
    trades = 0
    last = None
    for i in range(n):
        if i < 1:
            wc, wh, st = 1.0, 0.0, None
        else:
            hist = cx[:i]
            if len(hist) >= HIGH:
                st = compute_signal(list(hist), MA, HIGH, DD)["state"]
                wc, wh = STATE_RATIOS.get(st, (1.0, 0.0))
            else:
                st, wc, wh = None, 1.0, 0.0
        e *= (1.0 + (wc * rc[i] + wh * rh[i]))
        curve[i] = e
        if st is not None and st != last:
            trades += 1
        last = st
    ret = np.r_[0.0, curve[1:] / curve[:-1] - 1.0]
    return ret, curve, trades, df.index[0], df.index[-1]

def stats(ret, curve):
    total = curve[-1] - 1.0
    dd = float((curve / np.maximum.accumulate(curve) - 1.0).min())
    sd = ret.std(ddof=1)
    sharpe = ret.mean() / sd * np.sqrt(252) if sd > 0 else 0.0
    ny = len(ret) / 252.0
    ann = (1.0 + total) ** (1.0 / ny) - 1.0
    return total, ann, dd, sharpe, ann / abs(dd)

cyb = pd.read_csv(os.path.join(DATA_DIR, "idx399673.csv"), index_col=0, parse_dates=True)["close"]
gold = pd.read_csv(os.path.join(DATA_DIR, "etf518880.csv"), index_col=0, parse_dates=True)["close"]
common = cyb.index.intersection(gold.index)
common = common[common >= pd.Timestamp("2014-06-18")]

r = rotation(cyb.loc[common], gold.loc[common])
if r is None:
    print("数据不足")
else:
    ret, curve, trades, s0, s1 = r
    t, a, d, sh, ca = stats(ret, curve)
    print(f"[我的数据×旧函数·无成本] {s0.date()}~{s1.date()} ({len(ret)}根)")
    print(f"  累计 {t*100:+.1f}%  年化 {a*100:+.1f}%  回撤 {d*100:.1f}%  Calmar {ca:.2f}  换手 {trades}")
    print("  [旧报告口径应为: +1923.1% / -29.0% / Calmar 1.01 (TDX数据, 无成本)]")
