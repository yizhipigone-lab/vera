"""避险腿遍历 —— 创业板50回撤时, 按本系统规则该轮动进哪只 ETF (2026-08-18)。

问题: 系统规则 (MA20方向 + 250日高点回撤三态) 触发避险时, 现写死买黄金ETF(518880)。
      还有没有比黄金更该买的避险腿?

规则 (复用 trade/rotation.compute_signal, 风险腿固定创业板50ETF 159949):
    满仓创业板50 (MA20向上 且 回撤>=-20%)
    半仓 创业板50+避险腿各半 (MA20向上 但 回撤<-20%)   ← 深度回撤
    满仓避险腿 (MA20向下)                               ← 趋势转熊
  信号用 t-1 及以前收盘, 应用到 t 日收益 (无未来函数)。

两路输出:
  A. 整体轮动 {创业板50, H} 全指标排名 (换避险腿后整套规则跑 7 年的结果)。
  B. 避险期分解: 只统计"满仓避险日"与"半仓避险日"里, 每只 H 实际赚了多少 ——
     这才是"回撤时买什么"的直接答案 (risk-on 日 100% 创业板50, 与避险腿无关)。

数据: TDX 前复权日线 (use_cache, 上一轮已拉满历史), 2019-07 起。
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
from trade.rotation import STATE_RATIOS, compute_signal

CYB = "159949.SZ"        # 创业板50ETF (风险腿, 固定)
GOLD = "518880.SH"       # 黄金ETF (现役避险腿, 基准)
START, END = "20190701", "20260818"
MA, HIGH, DD = 20, 250, 0.20

# 避险腿候选: (代码, 名称, 类别)
HEDGES = [
    ("518880.SH", "黄金ETF", "商品"),      # 现役基准
    ("518800.SH", "黄金基金", "商品"),
    ("511010.SH", "国债", "债券"),
    ("511260.SH", "十年国债", "债券"),
    ("511090.SH", "30年国债", "债券"),
    ("511220.SH", "城投债", "债券"),
    ("511990.SH", "华宝添益(货基)", "现金"),
    ("510880.SH", "红利", "红利价值"),
    ("512890.SH", "红利低波", "红利价值"),
    ("512800.SH", "银行", "价值"),
    ("510050.SH", "上证50", "宽基"),
    ("159985.SZ", "豆粕", "商品"),
    ("162411.SZ", "华宝油气", "商品"),
    ("513100.SH", "纳指", "海外"),
    ("513500.SH", "标普500", "海外"),
    ("513030.SH", "德国30", "海外"),
    ("159920.SZ", "恒生", "海外"),
]

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "research", "etf_hedge_sweep")


def annualize(total, n):
    return (1.0 + total) ** (252.0 / n) - 1.0 if n > 0 else 0.0


def m(ret, curve):
    n = len(ret)
    total = curve[-1] - 1.0
    dd = float((curve / np.maximum.accumulate(curve) - 1.0).min())
    sd = ret.std(ddof=1)
    sharpe = (ret.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0
    calmar = (annualize(total, n) / abs(dd)) if dd < 0 else 0.0
    return total, annualize(total, n), dd, sharpe, calmar, float((ret > 0).mean()), n


def rotation(close_cyb, close_h):
    """{创业板50, H} 三态轮动。返回 (日收益, 净值, 换手, 避险日权重序列)。"""
    df = pd.concat([close_cyb, close_h], axis=1, join="inner").dropna()
    if len(df) < HIGH + 5:
        return None
    cx = df.iloc[:, 0].values
    ch = df.iloc[:, 1].values
    rc = np.r_[0.0, cx[1:] / cx[:-1] - 1.0]
    rh = np.r_[0.0, ch[1:] / ch[:-1] - 1.0]
    n = len(df)
    curve = np.empty(n)
    w_hedge = np.zeros(n)   # 当日避险腿权重 (0 / 0.5 / 1.0)
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
        w_hedge[i] = wh
        e *= (1.0 + (wc * rc[i] + wh * rh[i]))
        curve[i] = e
        if st is not None and st != last:
            trades += 1
        last = st
    ret = np.r_[0.0, curve[1:] / curve[:-1] - 1.0]
    return ret, curve, trades, w_hedge


def main():
    codes = sorted(set([CYB, GOLD] + [h[0] for h in HEDGES]))
    names = {h[0]: h[1] for h in HEDGES}
    cats = {h[0]: h[2] for h in HEDGES}
    print(f"取数 {len(codes)} 只 ETF ...")
    kl = DataFetcher.get_kline(codes, START, END, period="1d",
                               dividend_type="front", use_cache=True)
    close = kl["Close"]
    cyb_s = close[CYB].dropna()
    print(f"创业板50 {CYB}: {cyb_s.index[0].date()} ~ {cyb_s.index[-1].date()}, {len(cyb_s)} 根\n")

    # 用黄金做一次基准, 同时拿到避险日权重序列 (信号只取决于创业板50, 与避险腿无关)
    base = rotation(cyb_s, close[GOLD].dropna())
    _, base_curve, base_trades, w_hedge = base
    full_mask = w_hedge >= 0.999   # 满仓避险日 (MA20向下)
    half_mask = (w_hedge > 0.499) & (w_hedge < 0.999)   # 半仓避险日 (深度回撤)
    n_full = int(full_mask.sum())
    n_half = int(half_mask.sum())
    print(f"避险期结构 (7年): 满仓避险日 {n_full} 天, 半仓避险日 {n_half} 天, "
          f"其余 {len(w_hedge)-n_full-n_half} 天满仓创业板50\n")

    rows = []
    for code, name, cat in HEDGES:
        s = close[code].dropna()
        r = rotation(cyb_s, s)
        if r is None:
            print(f"  [跳过] {name}({code})")
            continue
        ret, curve, trades, _ = r
        t, a, d, sh, ca, win, n = m(ret, curve)
        # 避险期分解 (用该避险腿自己的日收益)
        rh = s.reindex(cyb_s.index).pct_change().fillna(0.0).values
        # 与 rotation 内部对齐: 只取共同窗口
        dfw = pd.concat([cyb_s, s], axis=1, join="inner").dropna()
        rh = dfw.iloc[:, 1].pct_change().fillna(0.0).values
        full_ret = float(np.prod(1.0 + rh[full_mask[:len(rh)]]) - 1.0) if n_full else 0.0
        half_ret = float(np.prod(1.0 + rh[half_mask[:len(rh)]]) - 1.0) if n_half else 0.0
        off_ret = float(np.prod(1.0 + rh[(full_mask | half_mask)[:len(rh)]]) - 1.0)
        # 避险期夏普 (只算避险日)
        off_r = rh[(full_mask | half_mask)[:len(rh)]]
        off_sh = (off_r.mean() / off_r.std(ddof=1) * np.sqrt(252)) if off_r.std(ddof=1) > 0 else 0.0
        rows.append({
            "name": name, "cat": cat, "code": code,
            "total": t, "ann": a, "maxdd": d, "sharpe": sh, "calmar": ca,
            "win": win, "n": n, "trades": trades,
            "full_ret": full_ret, "half_ret": half_ret, "off_ret": off_ret,
            "off_sharpe": off_sh,
        })

    rdf = pd.DataFrame(rows)
    rdf["beat_gold_total"] = rdf["total"] > rdf.loc[rdf.code == GOLD, "total"].iloc[0]
    rdf["beat_gold_calmar"] = rdf["calmar"] > rdf.loc[rdf.code == GOLD, "calmar"].iloc[0]

    print("=== A. 整体轮动 {创业板50, 避险腿H} —— 换避险腿后整套规则跑7年 ===")
    a_df = rdf.sort_values("calmar", ascending=False).reset_index(drop=True)
    print(f"{'避险腿':<10}{'类别':<6}{'轮动累计':>9}{'夏普':>7}{'Calmar':>8}"
          f"{'回撤':>8}{'胜率':>6}{'换手':>5}  {'比黄金Calmar'}")
    g_cal = rdf.loc[rdf.code == GOLD, "calmar"].iloc[0]
    for _, r in a_df.iterrows():
        tag = "★" if r["calmar"] > g_cal else ""
        print(f"{r['name']:<10}{r['cat']:<6}{r['total']*100:>+8.1f}%{r['sharpe']:>7.2f}"
              f"{r['calmar']:>8.2f}{r['maxdd']*100:>7.1f}%{r['win']*100:>5.0f}%"
              f"{r['trades']:>5}  {tag}")

    print("\n=== B. 避险期分解: 创业板回撤/走弱时, 每只避险腿实际赚了多少 ===")
    b_df = rdf.sort_values("off_ret", ascending=False).reset_index(drop=True)
    print(f"{'避险腿':<10}{'满仓避险日赚':>12}{'半仓避险日赚':>12}{'避险期合计':>11}{'避险期夏普':>10}")
    for _, r in b_df.iterrows():
        print(f"{r['name']:<10}{r['full_ret']*100:>+11.1f}%{r['half_ret']*100:>+11.1f}%"
              f"{r['off_ret']*100:>+10.1f}%{r['off_sharpe']:>10.2f}")

    # 落盘
    os.makedirs(OUT_DIR, exist_ok=True)
    rdf.to_csv(os.path.join(OUT_DIR, "etf_hedge_sweep.csv"), index=False, encoding="utf-8-sig")
    today = datetime.now().strftime("%Y-%m-%d")
    md = os.path.join(OUT_DIR, f"{today}_ETF避险腿遍历_创业板回撤买什么_研究报告.md")
    _report(md, a_df, b_df, n_full, n_half, cyb_s)
    print(f"\nCSV: {os.path.join(OUT_DIR, 'etf_hedge_sweep.csv')}")
    print(f"报告: {md}")


def _report(path, a_df, b_df, n_full, n_half, cyb_s):
    g = a_df[a_df.code == GOLD].iloc[0]
    top_off = b_df.head(4)
    lines = []
    lines.append("# ETF 避险腿遍历 —— 创业板50回撤时该买什么 (2026-08-18)\n")
    lines.append("## 结论先行\n")
    lines.append(f"- 避险期结构：7 年里 **满仓避险日 {n_full} 天、半仓避险日 {n_half} 天**，"
                 f"其余满仓创业板50。")
    lines.append(f"- 现役避险腿黄金：整体轮动累计 {g['total']*100:+.1f}%、Calmar {g['calmar']:.2f}。")
    lines.append(f"- 回撤期合计赚最多的避险腿前几: " + "、".join(
        f"{r['name']}({r['off_ret']*100:+.1f}%)" for _, r in top_off.iterrows()))
    lines.append("\n## A. 整体轮动（换避险腿后整套规则 7 年，按 Calmar 排序）\n")
    lines.append("| 避险腿 | 类别 | 轮动累计 | 年化 | 夏普 | Calmar | 最大回撤 | 胜率 | 换手 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for _, r in a_df.iterrows():
        lines.append(f"| {r['name']} | {r['cat']} | {r['total']*100:+.1f}% | {r['ann']*100:+.1f}% | "
                     f"{r['sharpe']:.2f} | {r['calmar']:.2f} | {r['maxdd']*100:.1f}% | "
                     f"{r['win']*100:.0f}% | {r['trades']} |")
    lines.append("\n## B. 避险期分解（满仓避险日=MA20向下、半仓避险日=深度回撤）\n")
    lines.append("| 避险腿 | 满仓避险日赚 | 半仓避险日赚 | 避险期合计 | 避险期夏普 |")
    lines.append("|---|---|---|---|---|")
    for _, r in b_df.iterrows():
        lines.append(f"| {r['name']} | {r['full_ret']*100:+.1f}% | {r['half_ret']*100:+.1f}% | "
                     f"{r['off_ret']*100:+.1f}% | {r['off_sharpe']:.2f} |")
    lines.append("\n## 口径与免责\n")
    lines.append("- 风险腿固定创业板50ETF(159949)，避险腿逐一遍历，信号只取决于创业板50（与避险腿无关），无未来函数。")
    lines.append("- 数据 TDX 前复权日线 2019-07~2026-08-18；未计交易成本与滑点。")
    lines.append("- 回测绝对值不可全信，相对排序在同口径下可参考。")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    main()
