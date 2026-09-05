"""主腿换成纳指 vs 创业板50 —— 2014 窗口同规则对照 (2026-08-18)。

问题: 现有系统主腿=创业板50(399673/159949)+黄金避险, 报告 +1887.4%。
      若改 主腿=纳指(513100) + 黄金避险, 是否更好?

对照 (同一窗口 2014-06-18~2026-08, 同一 MA20+250日回撤三态规则, 信号挂主腿):
  A. 创业板50指数(399673) 主 + 黄金(518880) 避险   (现系统, 报告口径)
  B. 纳指(513100) 主 + 黄金(518880) 避险          (用户提案)
  C. 创业板50指数 主 + 纳指 避险                   (上一轮发现的最高)
  D. 纳指主 + 各避险腿 H 扫描                        (纳指为主时, 谁当避险最好)

数据: 399673 腾讯源; ETF 走 TDX 前复权。统一截到 2014-06-18 起 (399673 数据起点)。
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
START = "20140618"   # 统一窗口起点 (399673 数据起点)
END = "20260818"

GOLD = "518880.SH"
NASDAQ = "513100.SH"

HEDGES = [
    ("518880.SH", "黄金", "商品"),
    ("511010.SH", "国债", "债券"),
    ("510880.SH", "红利", "红利价值"),
    ("510050.SH", "上证50", "宽基"),
    ("162411.SZ", "华宝油气", "商品"),
    ("511990.SH", "华宝添益(货基)", "现金"),
    ("159920.SZ", "恒生", "海外"),
    ("513500.SH", "标普500", "海外"),
    ("513030.SH", "德国30", "海外"),
    ("511220.SH", "城投债", "债券"),
]

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "research", "etf_main_nasdaq_2014")


def annualize(total, n):
    return (1.0 + total) ** (252.0 / n) - 1.0 if n > 0 else 0.0


def rotation(close_main, close_hedge):
    df = pd.concat([close_main, close_hedge], axis=1, join="inner").dropna()
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
            wm, wh, st = 1.0, 0.0, None
        else:
            hist = cx[:i]
            if len(hist) >= HIGH:
                st = compute_signal(list(hist), MA, HIGH, DD)["state"]
                wm, wh = STATE_RATIOS.get(st, (1.0, 0.0))
            else:
                st, wm, wh = None, 1.0, 0.0
        e *= (1.0 + (wm * rc[i] + wh * rh[i]))
        curve[i] = e
        if st is not None and st != last:
            trades += 1
        last = st
    ret = np.r_[0.0, curve[1:] / curve[:-1] - 1.0]
    return ret, curve, trades, df.index[0], df.index[-1]


def metrics(ret, curve):
    n = len(ret)
    total = curve[-1] - 1.0
    dd = float((curve / np.maximum.accumulate(curve) - 1.0).min())
    sd = ret.std(ddof=1)
    sharpe = (ret.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0
    calmar = (annualize(total, n) / abs(dd)) if dd < 0 else 0.0
    return total, annualize(total, n), dd, sharpe, calmar, float((ret > 0).mean()), n


def show(tag, r):
    ret, curve, trades, s0, s1 = r
    t, a, d, sh, ca, win, n = metrics(ret, curve)
    print(f"  {tag:<34} {t*100:>+9.1f}% 年化{a*100:>+7.1f}% 回撤{d*100:>7.1f}% "
          f"夏普{sh:>6.2f} Calmar{ca:>6.2f} 换手{trades}")
    return t, a, d, sh, ca


def main():
    import akshare as ak
    cyb = ak.stock_zh_index_daily_tx(symbol="sz399673")
    cyb = cyb.set_index("date")["close"]
    cyb.index = pd.to_datetime(cyb.index)
    cyb = cyb[cyb.index >= START]

    codes = sorted(set([NASDAQ, GOLD] + [h[0] for h in HEDGES]))
    kl = DataFetcher.get_kline(codes, "20140101", END, period="1d",
                               dividend_type="front", use_cache=True)
    close = kl["Close"]

    def series(code):
        s = close[code].dropna()
        s.index = pd.to_datetime(s.index)
        return s[s.index >= START]

    nas = series(NASDAQ)
    gold = series(GOLD)
    print(f"窗口 {START} ~ {cyb.index[-1].date()}, 纳指{nas.index[0].date()}起, "
          f"创业板50指数{cyb.index[0].date()}起\n")

    print("=== 三方案对照 (同一窗口, 同一规则, 信号挂主腿) ===")
    A = rotation(cyb, gold)
    B = rotation(nas, gold)
    C = rotation(cyb, nas)
    show("A. 创业板50主 + 黄金避险(现系统)", A)
    show("B. 纳指主 + 黄金避险(用户提案)", B)
    show("C. 创业板50主 + 纳指避险(上轮最高)", C)

    print("\n=== D. 纳指为主时, 避险腿扫描 ===")
    rows = []
    for code, name, cat in HEDGES:
        r = rotation(nas, series(code))
        if r is None:
            continue
        ret, curve, trades, s0, s1 = r
        t, a, d, sh, ca, win, n = metrics(ret, curve)
        rows.append((name, t, a, d, sh, ca))
    rows.sort(key=lambda x: -x[1])
    for name, t, a, d, sh, ca in rows:
        print(f"    纳指主+{name:<10} {t*100:>+9.1f}% 年化{a*100:>+7.1f}% "
              f"回撤{d*100:>7.1f}% 夏普{sh:>6.2f} Calmar{ca:>6.2f}")

    # 落盘
    os.makedirs(OUT_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    md = os.path.join(OUT_DIR, f"{today}_主腿换纳指_2014窗_研究报告.md")
    a_t, a_a, a_d, a_sh, a_ca = metrics(*A[:2])[:5]
    b_t, b_a, b_d, b_sh, b_ca = metrics(*B[:2])[:5]
    c_t, c_a, c_d, c_sh, c_ca = metrics(*C[:2])[:5]
    with open(md, "w", encoding="utf-8") as f:
        f.write(f"""# 主腿换成纳指 vs 创业板50 —— 2014 窗口同规则对照 (2026-08-18)

## 结论

| 方案 | 主腿 | 避险腿 | 累计 | 年化 | 最大回撤 | 夏普 | Calmar |
|---|---|---|---|---|---|---|---|
| A 现系统 | 创业板50指数 | 黄金 | {a_t*100:+.1f}% | {a_a*100:+.1f}% | {a_d*100:.1f}% | {a_sh:.2f} | {a_ca:.2f} |
| B 用户提案 | 纳指 | 黄金 | {b_t*100:+.1f}% | {b_a*100:+.1f}% | {b_d*100:.1f}% | {b_sh:.2f} | {b_ca:.2f} |
| C 上轮最高 | 创业板50指数 | 纳指 | {c_t*100:+.1f}% | {c_a*100:+.1f}% | {c_d*100:.1f}% | {c_sh:.2f} | {c_ca:.2f} |

- 规则: MA20方向 + 250日高点回撤三态, 信号挂主腿, t-1 信号应用到 t。
- 数据: 399673 腾讯源; ETF 走 TDX 前复权; 统一 2014-06-18 起。
- 未计交易成本/滑点; QDII(纳指) 有汇率/溢价/跟踪误差。
""")
    print(f"\n报告: {md}")


if __name__ == "__main__":
    main()
