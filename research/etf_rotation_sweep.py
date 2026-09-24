"""ETF 轮动遍历 —— 实证找出"比买入持有黄金ETF更强"的轮动腿 (2026-08-18)。

问: 除创业板50外, 还有没有更好的可轮动 ETF?
答: 用两路规则把候选 ETF 全部遍历一遍, 与黄金ETF(518880) 买入持有同窗对比。

规则 A (风险腿扫描, 复用生产规则 trade/rotation.compute_signal):
    候选 X 为风险腿, 黄金为避险腿, MA20 方向 + 250日高点回撤三态:
      MA20 向上 且 回撤 >= -20% → 满仓 X
      MA20 向上 但 回撤 <  -20% → X + 黄金各半
      MA20 向下                  → 满仓黄金
    信号用 t-1 及以前收盘算, 应用到 t 日收益 (无未来函数)。

规则 B (动量轮动, 全池):
    全池 (含黄金) 每 20 个交易日挑 20日动量最强 top-1 / top-2, 持有 20 日。

数据: TDX 前复权日线 (DataFetcher.get_kline, use_cache=True), 2019-07 起。
输出: 控制台排名表 + CSV + 报告 markdown。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 控制台编码: Windows GBK 遇到 ↔/★ 会崩, 强制 UTF-8
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np
import pandas as pd

from core.data_fetcher import DataFetcher
from trade.legacy_three_state import STATE_RATIOS, compute_signal

GOLD = "518880.SH"
START = "20190701"
END = "20260818"
MA, HIGH, DD = 20, 250, 0.20
MOM_WIN = 20        # 动量窗口 (交易日)
REBAL = 20          # 动量轮动再平衡间隔 (交易日)
RF = 0.0            # 夏普无风险利率 (与项目惯例一致, 0)

# 候选池: (代码, 名称, 类别)
UNIVERSE = [
    # 宽基
    ("510050.SH", "上证50", "宽基"),
    ("510180.SH", "上证180", "宽基"),
    ("510300.SH", "沪深300", "宽基"),
    ("510500.SH", "中证500", "宽基"),
    ("512100.SH", "中证1000", "宽基"),
    ("588000.SH", "科创50", "宽基"),
    ("159915.SZ", "创业板", "宽基"),
    ("159949.SZ", "创业板50", "宽基"),  # 现役风险腿
    # 红利 / 价值
    ("510880.SH", "红利", "红利价值"),
    ("512890.SH", "红利低波", "红利价值"),
    ("512800.SH", "银行", "价值"),
    # 行业 / 主题
    ("512480.SH", "半导体", "行业"),
    ("512760.SH", "芯片", "行业"),
    ("512690.SH", "酒", "行业"),
    ("515790.SH", "光伏", "行业"),
    ("515030.SH", "新能源车", "行业"),
    ("512170.SH", "医疗", "行业"),
    ("512290.SH", "生物医药", "行业"),
    ("512010.SH", "医药", "行业"),
    ("512660.SH", "军工", "行业"),
    ("512880.SH", "证券", "行业"),
    ("512400.SH", "有色金属", "行业"),
    ("515220.SH", "煤炭", "行业"),
    ("512980.SH", "传媒", "行业"),
    ("512720.SH", "计算机", "行业"),
    ("515000.SH", "科技龙头", "行业"),
    ("515170.SH", "食品饮料", "行业"),
    ("512200.SH", "房地产", "行业"),
    ("515880.SH", "通信", "行业"),
    ("515050.SH", "5G", "行业"),
    ("159869.SZ", "游戏", "行业"),
    ("159928.SZ", "消费", "行业"),
    ("515210.SH", "钢铁", "行业"),
    # 商品
    ("518880.SH", "黄金", "商品"),     # 基准, 兼动量池候选
    ("159985.SZ", "豆粕", "商品"),
    ("162411.SZ", "华宝油气", "商品"),
    # 债券
    ("511010.SH", "国债", "债券"),
    ("511260.SH", "十年国债", "债券"),
    ("511090.SH", "30年国债", "债券"),
    # 海外 / QDII
    ("513100.SH", "纳指", "海外"),
    ("513500.SH", "标普500", "海外"),
    ("513030.SH", "德国30", "海外"),
    ("159920.SZ", "恒生", "海外"),
    ("513050.SH", "中概互联", "海外"),
    ("513180.SH", "恒生科技", "海外"),
    ("513880.SH", "日经", "海外"),
]

GOLD_NAME = "黄金ETF"
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "research", "etf_rotation_sweep")


def annualize(total, n):
    return (1.0 + total) ** (252.0 / n) - 1.0 if n > 0 else 0.0


def metrics(ret: np.ndarray, curve: np.ndarray) -> dict:
    """从日收益序列 + 净值曲线算全口径指标。ret/curve 等长。"""
    n = len(ret)
    total = curve[-1] - 1.0
    ann = annualize(total, n)
    dd = float((curve / np.maximum.accumulate(curve) - 1.0).min())
    ret = np.asarray(ret, dtype=float)
    mu = ret.mean()
    sd = ret.std(ddof=1)
    sharpe = (mu / sd * np.sqrt(252)) if sd > 0 else 0.0
    calmar = (ann / abs(dd)) if dd < 0 else 0.0
    win = float((ret > 0).mean())
    return {"total": total, "ann": ann, "maxdd": dd, "sharpe": sharpe,
            "calmar": calmar, "win": win, "n": n}


def rule_a_rotation(close_x: pd.Series, close_g: pd.Series):
    """生产三态规则: 风险腿 X ↔ 黄金。返回 (日收益数组, 净值数组, 换档次数)。"""
    df = pd.concat([close_x, close_g], axis=1, join="inner").dropna()
    if len(df) < HIGH + 5:
        return None
    cx = df.iloc[:, 0].values
    cg = df.iloc[:, 1].values
    rx = np.r_[0.0, cx[1:] / cx[:-1] - 1.0]
    rg = np.r_[0.0, cg[1:] / cg[:-1] - 1.0]
    n = len(df)
    curve = np.empty(n)
    e = 1.0
    trades = 0
    last = None
    for i in range(n):
        if i < 1:
            wx, wg, st = 1.0, 0.0, None
        else:
            hist = cx[:i]
            if len(hist) >= HIGH:
                st = compute_signal(list(hist), MA, HIGH, DD)["state"]
                wx, wg = STATE_RATIOS.get(st, (1.0, 0.0))
            else:
                st, wx, wg = None, 1.0, 0.0
        e *= (1.0 + (wx * rx[i] + wg * rg[i]))
        curve[i] = e
        if st is not None and st != last:
            trades += 1
        last = st
    ret = np.r_[0.0, curve[1:] / curve[:-1] - 1.0]
    return ret, curve, trades


def rule_b_momentum(closes: pd.DataFrame, topk: int):
    """全池动量轮动: 每 REBAL 交易日挑 20日动量最强 top-k, 持有到下个调仓日。"""
    ret_panel = closes.pct_change()
    mom = closes / closes.shift(MOM_WIN) - 1.0
    n, m = ret_panel.shape
    idx = closes.index
    weights = pd.DataFrame(0.0, index=idx, columns=closes.columns)
    # 每 REBAL 天定一次仓, 持有 [r+1, r+REBAL]
    r = 0
    picks = []
    while r < n:
        mrow = mom.iloc[r]
        valid = mrow[mrow.notna()]
        if not valid.empty:
            chosen = valid.nlargest(topk).index.tolist()
            picks.append((idx[r], chosen))
            for c in chosen:
                weights.loc[idx[r + 1:r + 1 + REBAL], c] = 1.0 / len(chosen)
        r += REBAL
    port_ret = (weights * ret_panel).sum(axis=1)
    port_ret = port_ret.fillna(0.0)
    curve = (1.0 + port_ret).cumprod()
    # 起始点: 第一个有效收益之后
    first = ret_panel.apply(lambda s: s.first_valid_index()).min()
    curve = curve[curve.index >= first]
    return port_ret, curve, picks


def main():
    codes = [u[0] for u in UNIVERSE]
    names = {u[0]: u[1] for u in UNIVERSE}
    cats = {u[0]: u[2] for u in UNIVERSE}
    print(f"取数 {len(codes)} 只 ETF 前复权日线 {START}~{END} ...")
    kl = DataFetcher.get_kline(codes, START, END, period="1d",
                               dividend_type="front", use_cache=True,
                               force_refresh=True)
    close = kl["Close"]
    gold_s = close[GOLD].dropna()
    print(f"黄金ETF {GOLD}: {gold_s.index[0].date()} ~ {gold_s.index[-1].date()}, {len(gold_s)} 根\n")

    # 全局黄金基准 (2019-07 起)
    g_full = gold_s[gold_s.index >= START]
    g_ret = g_full.pct_change().fillna(0.0).values
    g_curve = np.cumprod(1.0 + g_ret)
    g_met = metrics(g_ret, g_curve)
    print("=== 黄金ETF 买入持有 (全窗基准) ===")
    print(f"  累计 {g_met['total']*100:+.1f}%  年化 {g_met['ann']*100:+.1f}%  "
          f"回撤 {g_met['maxdd']*100:.1f}%  夏普 {g_met['sharpe']:.2f}  "
          f"Calmar {g_met['calmar']:.2f}  胜率 {g_met['win']*100:.0f}%\n")

    # ── 规则 A: 风险腿扫描 ──
    rows = []
    for code, name, cat in UNIVERSE:
        if code == GOLD:
            continue
        s = close[code].dropna()
        if len(s) < HIGH + 5:
            print(f"  [跳过] {name}({code}) 数据不足: {len(s)} 根")
            continue
        r = rule_a_rotation(s, gold_s)
        if r is None:
            continue
        ret, curve, trades = r
        m = metrics(ret, curve)
        # 同窗黄金 (与该 ETF 对齐的窗口内买入持有黄金)
        dfw = pd.concat([s, gold_s], axis=1, join="inner").dropna()
        gw = dfw.iloc[:, 1].pct_change().fillna(0.0).values
        gc = np.cumprod(1.0 + gw)
        gm = metrics(gw, gc)
        rows.append({
            "name": name, "cat": cat, "code": code,
            "start": s.index[0].date(), "n": m["n"],
            "total": m["total"], "ann": m["ann"], "maxdd": m["maxdd"],
            "sharpe": m["sharpe"], "calmar": m["calmar"], "win": m["win"],
            "trades": trades,
            "g_total": gm["total"], "g_sharpe": gm["sharpe"],
            "g_calmar": gm["calmar"], "g_maxdd": gm["maxdd"],
        })

    rdf = pd.DataFrame(rows)
    rdf["beat_total"] = rdf["total"] > rdf["g_total"]
    rdf["beat_sharpe"] = rdf["sharpe"] > rdf["g_sharpe"]
    rdf = rdf.sort_values("sharpe", ascending=False).reset_index(drop=True)

    print(f"\n=== 规则A 风险腿扫描: 候选X ↔ 黄金 轮动, 按夏普排序 (共 {len(rdf)} 只) ===")
    print(f"{'排名':>3} {'风险腿':<8}{'类别':<6}{'轮动累计':>9}{'轮动夏普':>9}"
          f"{'轮动Calmar':>11}{'轮动回撤':>9}{'胜率':>6}{'换手':>4}  "
          f"{'同期黄金累计':>12}{'黄金夏普':>9}  {'强于黄金?'}")
    for i, r in rdf.iterrows():
        star = "★" if (r["beat_total"] and r["beat_sharpe"]) else ("·" if r["beat_total"] else " ")
        print(f"{i+1:>3} {r['name']:<8}{r['cat']:<6}{r['total']*100:>+8.1f}%"
              f"{r['sharpe']:>9.2f}{r['calmar']:>11.2f}{r['maxdd']*100:>8.1f}%"
              f"{r['win']*100:>5.0f}%{r['trades']:>4}  "
              f"{r['g_total']*100:>+11.1f}%{r['g_sharpe']:>9.2f}  {star}")

    # ── 规则 B: 动量轮动 ──
    mom_closes = close.copy()
    print(f"\n=== 规则B 动量轮动 (20日动量, 每20交易日调仓, 全池{len(codes)}只含黄金) ===")
    for topk in (1, 2):
        pret, pcurve, picks = rule_b_momentum(mom_closes, topk)
        m = metrics(pret.fillna(0.0).values, pcurve.values)
        # 同窗黄金
        gw = gold_s[pcurve.index[0]:pcurve.index[-1]].pct_change().fillna(0.0).values
        gc = np.cumprod(1.0 + gw)
        gm = metrics(gw, gc)
        print(f"  top-{topk}: 累计 {m['total']*100:+.1f}% 年化 {m['ann']*100:+.1f}% "
              f"回撤 {m['maxdd']*100:.1f}% 夏普 {m['sharpe']:.2f} Calmar {m['calmar']:.2f} "
              f"胜率 {m['win']*100:.0f}%  | 同窗黄金 累计 {gm['total']*100:+.1f}% "
              f"夏普 {gm['sharpe']:.2f} Calmar {gm['calmar']:.2f}")
        if topk == 1:
            from collections import Counter
            cnt = Counter(c for _, chosen in picks for c in chosen)
            sel = ", ".join(f"{names.get(c, c)}×{v}" for c, v in cnt.most_common(10))
            print(f"    历史选中: {sel}")

    # ── 落盘 CSV + markdown ──
    os.makedirs(OUT_DIR, exist_ok=True)
    rdf.to_csv(os.path.join(OUT_DIR, "etf_rotation_sweep.csv"),
               index=False, encoding="utf-8-sig")
    today = datetime.now().strftime("%Y-%m-%d")
    md_path = os.path.join(OUT_DIR, f"{today}_ETF轮动遍历_比黄金强_研究报告.md")
    _write_report(md_path, rdf, g_met, mom_closes)
    print(f"\nCSV: {os.path.join(OUT_DIR, 'etf_rotation_sweep.csv')}")
    print(f"报告: {md_path}")


def _write_report(path, rdf, g_met, mom_closes):
    gold_total = g_met["total"] * 100
    gold_sharpe = g_met["sharpe"]
    gold_calmar = g_met["calmar"]
    gold_dd = g_met["maxdd"] * 100
    lines = []
    lines.append(f"# ETF 轮动遍历 —— 谁比买入持有黄金更强 (2026-08-18)\n")
    lines.append("## 结论先行\n")
    top = rdf.head(8)
    winners = rdf[(rdf["beat_total"]) & (rdf["beat_sharpe"])]
    lines.append(f"- 黄金ETF 买入持有（2019-07 至今）: 累计 **{gold_total:+.1f}%**、"
                 f"年化 {annualize(g_met['total'], g_met['n'])*100:+.1f}%、"
                 f"最大回撤 {gold_dd:.1f}%、夏普 **{gold_sharpe:.2f}**、Calmar {gold_calmar:.2f}。")
    lines.append(f"- 规则A（风险腿↔黄金轮动）遍历 {len(rdf)} 只 ETF，"
                 f"**累计收益与夏普同时跑赢同期黄金**的有 {len(winners)} 只。")
    lines.append(f"- 最强风险腿前三: " + "、".join(
        f"{r['name']}(夏普{r['sharpe']:.2f}/累计{r['total']*100:+.1f}%)"
        for _, r in top.head(3).iterrows()))
    lines.append("\n## 规则A 完整排名（风险腿 ↔ 黄金，MA20 + 250日回撤三态）\n")
    lines.append("| 排名 | 风险腿 | 类别 | 轮动累计 | 轮动年化 | 轮动夏普 | 轮动Calmar | 轮动回撤 | 胜率 | 换手 | 同期黄金累计 | 黄金夏普 | 强于黄金 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in rdf.iterrows():
        star = "★" if (r["beat_total"] and r["beat_sharpe"]) else ("·" if r["beat_total"] else "")
        lines.append(
            f"| {i+1} | {r['name']} | {r['cat']} | {r['total']*100:+.1f}% | {r['ann']*100:+.1f}% | "
            f"{r['sharpe']:.2f} | {r['calmar']:.2f} | {r['maxdd']*100:.1f}% | {r['win']*100:.0f}% | "
            f"{r['trades']} | {r['g_total']*100:+.1f}% | {r['g_sharpe']:.2f} | {star} |")
    lines.append("\n## 规则B 动量轮动（20日动量，每20交易日调仓）\n")
    lines.append("（见控制台输出；结论见上。）\n")
    lines.append("\n## 口径与免责\n")
    lines.append("- 数据：TDX 前复权日线，2019-07 至 2026-08-18。")
    lines.append("- 规则A 信号用 t-1 及以前收盘，应用到 t 日收益，无未来函数；复用生产 `trade/rotation.py::compute_signal`。")
    lines.append("- 未计交易成本与滑点；轮动换手次数已列，可自行估算。")
    lines.append("- 每只 ETF 用其自身有效区间与**同期**黄金对比（短历史 ETF 年化噪声大）。")
    lines.append("- 回测绝对值不可全信（项目惯例提示），相对排序在同口径下可参考。")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    main()
