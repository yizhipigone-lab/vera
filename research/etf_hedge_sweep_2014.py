"""避险腿遍历 (2014 窗口) —— 谁比黄金的 +1887.4% 更高 (2026-08-18)。

复现 output/rotation_report_bond_2014.html 的口径:
    风险腿 = 创业板50指数 399673 (腾讯源, 2014-06-18 起)
    规则   = MA20方向 + 250日高点回撤三态 (复用 trade/rotation.compute_signal)
    窗口   = 2014-06-18 ~ 2026-08 (12 年)
    黄金避险腿 = 518880, 得 +1887.4% / 回撤 -29.0% (本脚本先复现校验, 再遍历替换)

遍历: 把避险腿换成能回到 2014 的候选 ETF/指数, 同一规则, 按总收益排名,
      找 > +1887.4% 的; 同时给 Calmar/夏普/回撤 看"性价比"。
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

CYB_IDX = "399673.SZ"      # 创业板50指数 (腾讯源)
GOLD = "518880.SH"
START, END = "20140101", "20260818"
MA, HIGH, DD = 20, 250, 0.20

# 避险腿候选 (2014 全窗) + 部分 (晚上市, 跑自身窗口)
HEDGES = [
    ("518880.SH", "黄金ETF", "商品", "full"),
    ("510880.SH", "红利", "红利价值", "full"),
    ("510050.SH", "上证50", "宽基", "full"),
    ("162411.SZ", "华宝油气", "商品", "full"),
    ("513100.SH", "纳指", "海外", "full"),
    ("513500.SH", "标普500", "海外", "full"),
    ("159920.SZ", "恒生", "海外", "full"),
    ("513030.SH", "德国30", "海外", "full"),
    ("511010.SH", "国债", "债券", "full"),
    ("511990.SH", "华宝添益(货基)", "现金", "full"),
    ("511220.SH", "城投债", "债券", "full"),
    # 晚上市 (跑自身窗口, 标注)
    ("512800.SH", "银行", "价值", "part"),
    ("511260.SH", "十年国债", "债券", "part"),
    ("512890.SH", "红利低波", "红利价值", "part"),
    ("159985.SZ", "豆粕", "商品", "part"),
    ("513050.SH", "中概互联", "海外", "part"),
    ("511090.SH", "30年国债", "债券", "part"),
    ("518800.SH", "黄金基金", "商品", "part"),
]

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "research", "etf_hedge_sweep_2014")


def annualize(total, n):
    return (1.0 + total) ** (252.0 / n) - 1.0 if n > 0 else 0.0


def rotation(close_cyb, close_h):
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


def m(ret, curve):
    n = len(ret)
    total = curve[-1] - 1.0
    dd = float((curve / np.maximum.accumulate(curve) - 1.0).min())
    sd = ret.std(ddof=1)
    sharpe = (ret.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0
    calmar = (annualize(total, n) / abs(dd)) if dd < 0 else 0.0
    return total, annualize(total, n), dd, sharpe, calmar, float((ret > 0).mean()), n


def main():
    # 风险腿 399673 (腾讯源)
    import akshare as ak
    cyb = ak.stock_zh_index_daily_tx(symbol="sz399673")
    cyb = cyb.set_index("date")["close"].rename("cyb")
    cyb.index = pd.to_datetime(cyb.index)
    print(f"风险腿 399673 (腾讯): {cyb.index[0].date()} ~ {cyb.index[-1].date()}, {len(cyb)} 根")

    codes = sorted(set([h[0] for h in HEDGES]))
    kl = DataFetcher.get_kline(codes, START, END, period="1d",
                               dividend_type="front", use_cache=True,
                               force_refresh=True)
    close = kl["Close"]

    # 先复现黄金基准
    gold_s = close[GOLD].dropna()
    g = rotation(cyb, gold_s)
    g_ret, g_curve, g_trades, g_start, g_end = g
    gt, ga, gd, gsh, gca, gwin, gn = m(g_ret, g_curve)
    print(f"\n[校验] 黄金避险腿: 窗口 {g_start.date()}~{g_end.date()} ({gn} 交易日)  "
          f"收益 {gt*100:+.1f}% 年化 {ga*100:+.1f}% 回撤 {gd*100:.1f}% 换手 {g_trades}")
    print(f"       (报告口径应为 +1887.4% / -29.0%)\n")

    rows = []
    for code, name, cat, kind in HEDGES:
        s = close[code].dropna()
        r = rotation(cyb, s)
        if r is None:
            print(f"  [跳过] {name}({code}) 数据不足")
            continue
        ret, curve, trades, s0, s1 = r
        t, a, d, sh, ca, win, n = m(ret, curve)
        rows.append({"name": name, "cat": cat, "code": code, "kind": kind,
                     "start": s0.date(), "end": s1.date(), "n": n,
                     "total": t, "ann": a, "maxdd": d, "sharpe": sh,
                     "calmar": ca, "win": win, "trades": trades})

    rdf = pd.DataFrame(rows)
    full = rdf[rdf.kind == "full"].sort_values("total", ascending=False).reset_index(drop=True)
    part = rdf[rdf.kind == "part"].sort_values("total", ascending=False).reset_index(drop=True)

    print(f"=== 避险腿遍历 (2014 全窗, 风险腿=399673, 同规则) 按总收益排序 ===")
    print(f"{'避险腿':<10}{'类别':<6}{'轮动累计':>10}{'年化':>8}{'Calmar':>8}"
          f"{'夏普':>7}{'回撤':>8}{'换手':>5}  {'vs黄金+1887.4%'}")
    for _, r in full.iterrows():
        tag = "★ 更高" if r["total"] > gt else ""
        print(f"{r['name']:<10}{r['cat']:<6}{r['total']*100:>+9.1f}%{r['ann']*100:>+7.1f}%"
              f"{r['calmar']:>8.2f}{r['sharpe']:>7.2f}{r['maxdd']*100:>7.1f}%"
              f"{r['trades']:>5}  {tag}")

    print(f"\n=== 晚上市避险腿 (跑自身窗口, 非同期, 仅供参考) ===")
    print(f"{'避险腿':<10}{'窗口起':<12}{'轮动累计':>10}{'年化':>8}{'Calmar':>8}{'夏普':>7}{'回撤':>8}")
    for _, r in part.iterrows():
        print(f"{r['name']:<10}{str(r['start']):<12}{r['total']*100:>+9.1f}%{r['ann']*100:>+7.1f}%"
              f"{r['calmar']:>8.2f}{r['sharpe']:>7.2f}{r['maxdd']*100:>7.1f}%")

    os.makedirs(OUT_DIR, exist_ok=True)
    rdf.to_csv(os.path.join(OUT_DIR, "etf_hedge_sweep_2014.csv"), index=False, encoding="utf-8-sig")
    today = datetime.now().strftime("%Y-%m-%d")
    md = os.path.join(OUT_DIR, f"{today}_ETF避险腿遍历_2014窗_比黄金1887高_研究报告.md")
    _report(md, full, part, gt, gd, gca, gsh)
    print(f"\nCSV: {os.path.join(OUT_DIR, 'etf_hedge_sweep_2014.csv')}")
    print(f"报告: {md}")


def _report(path, full, part, gt, gd, gca, gsh):
    lines = []
    lines.append("# ETF 避险腿遍历 (2014 窗口) —— 谁比黄金的 +1887.4% 更高 (2026-08-18)\n")
    lines.append("## 结论先行\n")
    lines.append(f"- 黄金避险腿基准: 收益 {gt*100:+.1f}%、回撤 {gd*100:.1f}%、Calmar {gca:.2f}、夏普 {gsh:.2f}。")
    win = full[full.total > gt]
    lines.append(f"- 2014 全窗、同规则下, **总收益跑赢黄金**的避险腿有 {len(win)} 只。")
    if len(win):
        lines.append("- 跑赢者: " + "、".join(
            f"{r['name']}({r['total']*100:+.1f}%/回撤{r['maxdd']*100:.1f}%)"
            for _, r in win.iterrows()))
    lines.append("\n## 2014 全窗避险腿排名（按总收益）\n")
    lines.append("| 避险腿 | 类别 | 轮动累计 | 年化 | Calmar | 夏普 | 最大回撤 | 换手 | 强于黄金 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for _, r in full.iterrows():
        tag = "★" if r["total"] > gt else ""
        lines.append(f"| {r['name']} | {r['cat']} | {r['total']*100:+.1f}% | {r['ann']*100:+.1f}% | "
                     f"{r['calmar']:.2f} | {r['sharpe']:.2f} | {r['maxdd']*100:.1f}% | {r['trades']} | {tag} |")
    lines.append("\n## 晚上市避险腿（自身窗口，非同期，仅参考）\n")
    lines.append("| 避险腿 | 窗口起 | 轮动累计 | 年化 | Calmar | 夏普 | 最大回撤 |")
    lines.append("|---|---|---|---|---|---|---|")
    for _, r in part.iterrows():
        lines.append(f"| {r['name']} | {r['start']} | {r['total']*100:+.1f}% | {r['ann']*100:+.1f}% | "
                     f"{r['calmar']:.2f} | {r['sharpe']:.2f} | {r['maxdd']*100:.1f}% |")
    lines.append("\n## 口径与免责\n")
    lines.append("- 风险腿=创业板50指数399673(腾讯源)，规则复用 `trade/rotation.py::compute_signal`，信号 t-1、应用 t，无未来函数。")
    lines.append("- 数据 TDX 前复权日线；未计交易成本与滑点（各腿换手次数一致，相对排序不受成本影响）。")
    lines.append("- 回测绝对值不可全信，相对排序同口径可参考；QDII(纳指/标普等)有溢价率与跟踪误差。")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    main()
