"""最近一年 vs 近3年滚动 —— 三方案谁最近在赢 (2026-08-18)。

复用三方案 (创业板主 + 黄金/纳指/各半避险), 算:
  1. 最近 12 个月 (252 交易日) 收益
  2. 最近 3 年逐月滚动 1 年收益, 看谁是近期赢家
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
from trade.legacy_three_state import STATE_RATIOS, compute_signal

MA, HIGH, DD = 20, 250, 0.20
START = "20140618"
END = "20260818"
GOLD, NAS = "518880.SH", "513100.SH"


def run_rotation(cx, rh):
    n = len(cx)
    rc = np.r_[0.0, cx[1:] / cx[:-1] - 1.0]
    curve = np.empty(n)
    e = 1.0
    for i in range(n):
        if i < 1:
            wm, wh = 1.0, 0.0
        else:
            hist = cx[:i]
            if len(hist) >= HIGH:
                st = compute_signal(list(hist), MA, HIGH, DD)["state"]
                wm, wh = STATE_RATIOS.get(st, (1.0, 0.0))
            else:
                wm, wh = 1.0, 0.0
        e *= (1.0 + (wm * rc[i] + wh * rh[i]))
        curve[i] = e
    return pd.Series(curve)


def main():
    import akshare as ak
    cyb = ak.stock_zh_index_daily_tx(symbol="sz399673")
    cyb = cyb.set_index("date")["close"]
    cyb.index = pd.to_datetime(cyb.index)

    kl = DataFetcher.get_kline([GOLD, NAS], "20140101", END, period="1d",
                               dividend_type="front", use_cache=True)
    close = kl["Close"]
    gold = close[GOLD].dropna(); gold.index = pd.to_datetime(gold.index)
    nas = close[NAS].dropna(); nas.index = pd.to_datetime(nas.index)

    df = pd.concat([cyb, gold, nas], axis=1, join="inner").dropna()
    df = df[df.index >= START]
    dates = df.index
    cx = df.iloc[:, 0].values
    rg = np.r_[0.0, df.iloc[:, 1].values[1:] / df.iloc[:, 1].values[:-1] - 1.0]
    rn = np.r_[0.0, df.iloc[:, 2].values[1:] / df.iloc[:, 2].values[:-1] - 1.0]

    curves = {
        "黄金避险": run_rotation(cx, rg),
        "纳指避险": run_rotation(cx, rn),
        "黄金+纳指各半": run_rotation(cx, 0.5 * rg + 0.5 * rn),
    }
    for k in curves:
        curves[k].index = dates

    # 1) 最近 12 个月 (252 交易日)
    print("=== 最近 12 个月 (252 交易日) 收益 ===")
    for name, c in curves.items():
        r1y = c.iloc[-1] / c.iloc[-253] - 1.0
        # 最近半年 (126 交易日) 也一并给
        r6m = c.iloc[-1] / c.iloc[-127] - 1.0
        print(f"  {name:<14} 近1年 {r1y*100:+.1f}%   近半年 {r6m*100:+.1f}%")

    # 2) 最近 3 年逐月滚动 1 年收益
    print("\n=== 最近 3 年逐月「滚动1年收益」(谁最近在赢) ===")
    mends = pd.date_range(end=dates[-1], periods=36, freq="ME")
    print(f"{'月':<10}" + "".join(f"{k:>14}" for k in curves) + f"{'当月赢家':>10}")
    for me in mends:
        if me < dates[0] + pd.Timedelta(days=400):
            continue
        pos = dates.searchsorted(me, side="right") - 1
        if pos < 252:
            continue
        row = []
        for k, c in curves.items():
            v = c.iloc[pos] / c.iloc[pos - 252] - 1.0
            row.append(v)
        win = max(curves.keys(), key=lambda k: curves[k].iloc[pos] / curves[k].iloc[pos - 252] - 1.0)
        line = f"{me.strftime('%Y-%m'):<10}" + "".join(f"{v*100:>+13.1f}%" for v in row) + f"{win:>10}"
        print(line)


if __name__ == "__main__":
    main()
