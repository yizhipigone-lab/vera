"""三方案分年度对比 —— 创业板主 + 黄金/纳指/黄金纳指各半 避险 (2014 窗口, 2026-08-18)。

三方案 (同一规则 MA20+250日回撤三态, 信号挂创业板50指数399673, 2014-06 起):
  A. 避险腿 = 黄金 (现系统)
  B. 避险腿 = 纳指
  C. 避险腿 = 黄金+纳指各半 (每日 50/50 再平衡)
逐年看谁赢, 并拆每年"避险期"里黄金 vs 纳指各自表现, 找纳指避险的翻车年份。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

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

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "research", "etf_hedge_yearly_2014")


def annualize(total, n):
    return (1.0 + total) ** (252.0 / n) - 1.0 if n > 0 else 0.0


def run_rotation(cx, rh):
    """cx=主腿收盘数组, rh=避险腿日简单收益数组(rh[0]=0)。返回(日收益, 净值, 换手, 避险权重)。"""
    n = len(cx)
    rc = np.r_[0.0, cx[1:] / cx[:-1] - 1.0]
    curve = np.empty(n)
    w_hedge = np.zeros(n)
    e = 1.0
    trades = 0
    last = None
    for i in range(n):
        if i < 1:
            wm, wh, st = 1.0, 0.0, None
        else:
            hist = cx[:i]
            if len(hist) >= HIGH:
                st = compute_signal(list(hist), MA, HIGH, DD)["state"]
                wm, wh = STATE_RATIOS.get(st, (1.0, 0.0))
            else:
                st, wm, wh = None, 1.0, 0.0
        w_hedge[i] = wh
        e *= (1.0 + (wm * rc[i] + wh * rh[i]))
        curve[i] = e
        if st is not None and st != last:
            trades += 1
        last = st
    ret = np.r_[0.0, curve[1:] / curve[:-1] - 1.0]
    return ret, curve, trades, w_hedge


def m(ret, curve):
    n = len(ret)
    total = curve[-1] - 1.0
    dd = float((curve / np.maximum.accumulate(curve) - 1.0).min())
    sd = ret.std(ddof=1)
    sharpe = (ret.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0
    calmar = (annualize(total, n) / abs(dd)) if dd < 0 else 0.0
    return total, annualize(total, n), dd, sharpe, calmar


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
    rh_half = 0.5 * rg + 0.5 * rn   # 黄金+纳指各半, 每日再平衡

    schemes = {
        "黄金避险": run_rotation(cx, rg),
        "纳指避险": run_rotation(cx, rn),
        "黄金+纳指各半": run_rotation(cx, rh_half),
    }

    print(f"窗口 {dates[0].date()} ~ {dates[-1].date()}, {len(dates)} 交易日\n")
    print("=== 三方案总览 ===")
    print(f"{'方案':<14}{'累计':>10}{'年化':>8}{'回撤':>8}{'夏普':>7}{'Calmar':>8}")
    for name, (ret, curve, trades, wh) in schemes.items():
        t, a, d, sh, ca = m(ret, curve)
        print(f"{name:<14}{t*100:>+9.1f}%{a*100:>+7.1f}%{d*100:>7.1f}%{sh:>7.2f}{ca:>8.2f}")

    # 逐年
    print("\n=== 分年度收益 (每年为当年末净值/上年末净值-1) ===")
    yearly = {}
    for name, (ret, curve, trades, wh) in schemes.items():
        s = pd.Series(curve, index=dates)
        ye = s.resample("YE").last()
        y = ye.pct_change() * 100
        y.iloc[0] = (ye.iloc[0] - 1.0) * 100   # 首年(2014半年)直接 净值-1
        yearly[name] = y
    ydf = pd.DataFrame(yearly)
    ydf.index = ydf.index.year
    # 2014 是半年 (6月才起), 标注
    ydf = ydf.round(1)
    # 年度赢家
    winners = ydf.idxmax(axis=1)
    print(f"{'年':>6}" + "".join(f"{c:>16}" for c in ydf.columns) + f"{'年度赢家':>16}")
    for yr, row in ydf.iterrows():
        w = winners.loc[yr]
        print(f"{yr:>6}" + "".join(f"{row[c]:>+15.1f}%" for c in ydf.columns) + f"{w:>16}")

    # 避险期分解: 每年满仓避险日里, 黄金 vs 纳指 各自收益
    print("\n=== 每年「满仓避险日」(创业板MA20向下) 里 黄金 vs 纳指 收益 ===")
    w_hedge = schemes["黄金避险"][3]
    full_mask = w_hedge >= 0.999
    year_of = dates.year
    print(f"{'年':>6}{'满仓避险天数':>10}{'黄金收益':>10}{'纳指收益':>10}{'谁强':>8}")
    for yr in sorted(set(year_of)):
        ymask = (year_of == yr) & full_mask
        ndays = int(ymask.sum())
        if ndays == 0:
            continue
        g = float(np.prod(1.0 + rg[ymask]) - 1.0) * 100
        n = float(np.prod(1.0 + rn[ymask]) - 1.0) * 100
        win = "黄金" if g > n else ("纳指" if n > g else "平")
        print(f"{yr:>6}{ndays:>10}{g:>+9.1f}%{n:>+9.1f}%{win:>8}")

    # 落盘
    os.makedirs(OUT_DIR, exist_ok=True)
    ydf.to_csv(os.path.join(OUT_DIR, "etf_hedge_yearly_2014.csv"), encoding="utf-8-sig")
    today = datetime.now().strftime("%Y-%m-%d")
    md = os.path.join(OUT_DIR, f"{today}_三方案分年度对比_研究报告.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write("# 三方案分年度对比 (创业板主 + 黄金/纳指/各半避险, 2014窗)\n\n")
        f.write("## 总览\n\n")
        f.write("| 方案 | 累计 | 年化 | 最大回撤 | 夏普 | Calmar |\n|---|---|---|---|---|---|\n")
        for name, (ret, curve, trades, wh) in schemes.items():
            t, a, d, sh, ca = m(ret, curve)
            f.write(f"| {name} | {t*100:+.1f}% | {a*100:+.1f}% | {d*100:.1f}% | {sh:.2f} | {ca:.2f} |\n")
        f.write("\n## 分年度收益\n\n")
        f.write(ydf.to_markdown())
        f.write("\n\n注: 2014 为半年(6月18日起)。黄金+纳指各半 = 避险期每日50/50再平衡。未计成本。\n")
    print(f"\n报告: {md}")


if __name__ == "__main__":
    main()
