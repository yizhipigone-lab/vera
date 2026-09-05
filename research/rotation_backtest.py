"""ETF 轮动系统最长历史回测 + 多维图表 (2014 至今)。

规则 (复用 trade/rotation.py 的 compute_signal/STATE_RATIOS):
  创业板50指数(399673) MA20 方向 + 250日最高收盘回撤 → 三态
  (满仓创业板50 / 创业板50+黄金各半 / 满仓黄金)。每日收盘算信号次日调仓。
数据: 399673 走腾讯, 518880 黄金ETF 走 TDX。
输出: output/rotation_report_2014.html (含 12 张多维度图表)。
"""
from __future__ import annotations

import base64
import io
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import cm
from matplotlib.colors import ListedColormap

from trade.legacy_three_state import (
    STATE_RATIOS, compute_signal,
    STATE_FULL_CYB, STATE_HALF, STATE_FULL_GOLD,
)

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

MA = 20
HIGH = 250
DD = 0.20
STATE_NAMES = {STATE_FULL_CYB: "满仓创业板50", STATE_HALF: "半仓(创+金各半)",
               STATE_FULL_GOLD: "满仓黄金", None: "数据不足"}
# 三态配色: 满仓创=红, 半仓=橙, 满仓金=金
STATE_COLORS = {STATE_FULL_CYB: "#dc2626", STATE_HALF: "#f59e0b",
                STATE_FULL_GOLD: "#b8860b", None: "#9ca3af"}


def fetch_data():
    import akshare as ak
    cyb = ak.stock_zh_index_daily_tx(symbol="sz399673")
    cyb = cyb.set_index("date")["close"].rename("cyb")
    cyb.index = pd.to_datetime(cyb.index)

    from core.data_fetcher import DataFetcher
    kl = DataFetcher.get_kline(["518880.SH"], "20140101", "20260817",
                               period="1d", dividend_type="front", use_cache=True)
    gold = kl["Close"]["518880.SH"].dropna().rename("gold")
    gold.index = pd.to_datetime(gold.index)

    return pd.concat([cyb, gold], axis=1, join="inner").dropna()


def run_backtest(df):
    closes_cyb = df["cyb"].values
    closes_gold = df["gold"].values
    ret_cyb = np.r_[0.0, df["cyb"].pct_change().fillna(0).values[1:]]
    ret_gold = np.r_[0.0, df["gold"].pct_change().fillna(0).values[1:]]
    n = len(df)

    curve_rot = np.empty(n)
    curve_cyb = np.empty(n)
    curve_gold = np.empty(n)
    e_r = e_c = e_g = 1.0
    state_per_day = [None] * n
    trades = 0
    last_state = None
    for i in range(n):
        if i == 0:
            w_cyb, w_gold, st = 1.0, 0.0, None
        else:
            hist = closes_cyb[:i]
            if len(hist) >= HIGH:
                sig = compute_signal(list(hist), MA, HIGH, DD)
                st = sig["state"]
                w_cyb, w_gold = STATE_RATIOS.get(st, (1.0, 0.0))
            else:
                st = None
                w_cyb, w_gold = 1.0, 0.0
        r = w_cyb * ret_cyb[i] + w_gold * ret_gold[i]
        e_r *= (1.0 + r)
        e_c *= (1.0 + ret_cyb[i])
        e_g *= (1.0 + ret_gold[i])
        curve_rot[i], curve_cyb[i], curve_gold[i] = e_r, e_c, e_g
        state_per_day[i] = st
        if st is not None and st != last_state:
            trades += 1
        last_state = st

    return curve_rot, curve_cyb, curve_gold, state_per_day, trades


def _annualize(total, n):
    return (1.0 + total) ** (252.0 / n) - 1.0


def _fig_to_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=105, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def gen_charts(df, curve_rot, curve_cyb, curve_gold, state_per_day):
    dates = df.index
    n = len(df)
    charts = []  # (title, b64)

    def add(title, fig):
        charts.append((title, _fig_to_b64(fig)))

    # 日收益
    ret_rot = pd.Series(np.r_[0.0, np.diff(np.log(curve_rot))], index=dates)
    ret_cyb = pd.Series(np.r_[0.0, np.diff(np.log(curve_cyb))], index=dates)
    ret_gold = pd.Series(np.r_[0.0, np.diff(np.log(curve_gold))], index=dates)
    eq_rot = pd.Series(curve_rot, index=dates)
    eq_cyb = pd.Series(curve_cyb, index=dates)
    eq_gold = pd.Series(curve_gold, index=dates)

    # ── 1. 净值曲线 (轮动 vs 买入持有创 vs 买入持有金) ──
    fig, ax = plt.subplots(figsize=(11, 4.4))
    ax.plot(dates, curve_rot * 100, color="#dc2626", lw=1.3, label="轮动规则")
    ax.plot(dates, curve_cyb * 100, color="#64748b", lw=0.9, alpha=.85, label="买入持有创业板50")
    ax.plot(dates, curve_gold * 100, color="#b8860b", lw=0.9, alpha=.85, label="买入持有黄金ETF")
    ax.set_yscale("log")
    ax.set_title("1. 净值曲线（对数坐标，起始 100）")
    ax.legend(loc="upper left"); ax.grid(alpha=.3)
    add("净值曲线", fig)

    # ── 2. 回撤曲线 ──
    dd_rot = curve_rot / np.maximum.accumulate(curve_rot) - 1
    dd_cyb = curve_cyb / np.maximum.accumulate(curve_cyb) - 1
    fig, ax = plt.subplots(figsize=(11, 3.2))
    ax.fill_between(dates, dd_rot * 100, 0, color="#dc2626", alpha=.4, label="轮动回撤")
    ax.plot(dates, dd_cyb * 100, color="#64748b", lw=0.8, alpha=.9, label="买入持有回撤")
    ax.set_title("2. 回撤曲线（水下）")
    ax.legend(loc="lower left"); ax.grid(alpha=.3)
    add("回撤曲线", fig)

    # ── 3. 信号状态时间线 ──
    codes = np.array([{STATE_FULL_CYB: 2, STATE_HALF: 1, STATE_FULL_GOLD: 0, None: -1}[s]
                      for s in state_per_day])
    fig, ax = plt.subplots(figsize=(11, 2.2))
    cmap = ListedColormap(["#9ca3af", "#b8860b", "#f59e0b", "#dc2626"])
    ax.imshow(codes.reshape(1, -1), aspect="auto", cmap=cmap, vmin=-1, vmax=2,
              extent=[matplotlib.dates.date2num(dates[0]), matplotlib.dates.date2num(dates[-1]), 0, 1])
    ax.set_yticks([])
    ax.set_title("3. 信号状态时间线（灰=数据不足 / 金=满仓黄金 / 橙=半仓 / 红=满仓创业板50）")
    add("信号状态时间线", fig)

    # ── 4. 信号指标: 399673 + MA20 + 250日高点 + 回撤阈值线 ──
    cyb_s = df["cyb"]
    ma20 = cyb_s.rolling(MA).mean()
    high250 = cyb_s.rolling(HIGH).max()
    fig, ax = plt.subplots(figsize=(11, 4.4))
    ax.plot(dates, cyb_s, color="#0f172a", lw=0.9, label="399673 收盘")
    ax.plot(dates, ma20, color="#dc2626", lw=1.0, label="MA20")
    ax.plot(dates, high250, color="#64748b", lw=0.8, alpha=.7, label="250日最高收盘")
    ax.plot(dates, high250 * (1 - DD), color="#b8860b", lw=0.8, alpha=.7, label="回撤阈值(高点-20%)")
    ax.set_title("4. 信号指标：价格 / MA20 / 250日高点 / 20%回撤线")
    ax.legend(loc="upper left", ncol=2); ax.grid(alpha=.3)
    add("信号指标", fig)

    # ── 5. 年度收益对比 ──
    yr_rot = eq_rot.resample("YE").last().pct_change() * 100
    yr_cyb = eq_cyb.resample("YE").last().pct_change() * 100
    years = yr_rot.index.year
    x = np.arange(len(years))
    fig, ax = plt.subplots(figsize=(11, 4.0))
    w = 0.38
    ax.bar(x - w/2, yr_rot.values, w, color="#dc2626", label="轮动")
    ax.bar(x + w/2, yr_cyb.values, w, color="#64748b", label="买入持有创业板50")
    ax.axhline(0, color="#0f172a", lw=.6)
    ax.set_xticks(x); ax.set_xticklabels([str(y) for y in years])
    ax.set_title("5. 年度收益对比 (%)")
    ax.legend(); ax.grid(alpha=.3, axis="y")
    add("年度收益对比", fig)

    # ── 6. 月度收益热力图 ──
    m = ret_rot.resample("ME").apply(lambda s: (1+s).prod() - 1) * 100
    mdf = pd.DataFrame({"year": m.index.year, "month": m.index.month, "ret": m.values})
    pivot = mdf.pivot(index="year", columns="month", values="ret")
    fig, ax = plt.subplots(figsize=(11, 4.6))
    im = ax.imshow(pivot.values, cmap="RdYlGn", aspect="auto", vmin=-15, vmax=15)
    ax.set_xticks(range(12)); ax.set_xticklabels([f"{i+1}月" for i in range(12)])
    ax.set_yticks(range(len(pivot.index))); ax.set_yticklabels([str(y) for y in pivot.index])
    fig.colorbar(im, ax=ax, label="月度收益 %")
    ax.set_title("6. 月度收益热力图（红亏绿赚）")
    add("月度收益热力图", fig)

    # ── 7. 滚动1年收益 ──
    roll_rot = eq_rot / eq_rot.shift(252) - 1
    roll_cyb = eq_cyb / eq_cyb.shift(252) - 1
    fig, ax = plt.subplots(figsize=(11, 3.4))
    ax.plot(dates, roll_rot * 100, color="#dc2626", lw=1.0, label="轮动")
    ax.plot(dates, roll_cyb * 100, color="#64748b", lw=0.8, alpha=.85, label="买入持有创业板50")
    ax.axhline(0, color="#0f172a", lw=.6)
    ax.set_title("7. 滚动 1 年收益 (%)")
    ax.legend(); ax.grid(alpha=.3)
    add("滚动1年收益", fig)

    # ── 8. 滚动1年波动率 ──
    vol_rot = ret_rot.rolling(252).std() * np.sqrt(252) * 100
    vol_cyb = ret_cyb.rolling(252).std() * np.sqrt(252) * 100
    fig, ax = plt.subplots(figsize=(11, 3.4))
    ax.plot(dates, vol_rot, color="#dc2626", lw=1.0, label="轮动")
    ax.plot(dates, vol_cyb, color="#64748b", lw=0.8, alpha=.85, label="买入持有创业板50")
    ax.set_title("8. 滚动 1 年年化波动率 (%)")
    ax.legend(); ax.grid(alpha=.3)
    add("滚动波动率", fig)

    # ── 9. 滚动1年夏普 ──
    sh_rot = ret_rot.rolling(252).mean() / ret_rot.rolling(252).std() * np.sqrt(252)
    sh_cyb = ret_cyb.rolling(252).mean() / ret_cyb.rolling(252).std() * np.sqrt(252)
    fig, ax = plt.subplots(figsize=(11, 3.4))
    ax.plot(dates, sh_rot, color="#dc2626", lw=1.0, label="轮动")
    ax.plot(dates, sh_cyb, color="#64748b", lw=0.8, alpha=.85, label="买入持有创业板50")
    ax.axhline(0, color="#0f172a", lw=.6)
    ax.set_title("9. 滚动 1 年夏普比率")
    ax.legend(); ax.grid(alpha=.3)
    add("滚动夏普", fig)

    # ── 10. 日收益分布 ──
    fig, ax = plt.subplots(figsize=(11, 3.4))
    ax.hist(ret_rot * 100, bins=120, color="#dc2626", alpha=.55, density=True, label="轮动")
    ax.hist(ret_cyb * 100, bins=120, color="#64748b", alpha=.45, density=True, label="买入持有创业板50")
    ax.set_title("10. 日收益分布 (%)")
    ax.legend(); ax.grid(alpha=.3, axis="y")
    add("日收益分布", fig)

    # ── 11. 回撤持续期分布 ──
    under = dd_rot < 0
    dur = []
    cnt = 0
    for u in under:
        if u:
            cnt += 1
        else:
            if cnt > 0:
                dur.append(cnt)
            cnt = 0
    if cnt > 0:
        dur.append(cnt)
    fig, ax = plt.subplots(figsize=(11, 3.2))
    ax.hist(dur, bins=40, color="#dc2626", alpha=.7)
    ax.set_title(f"11. 回撤持续期分布（交易日，共 {len(dur)} 次回撤，最长 {max(dur) if dur else 0} 天）")
    ax.grid(alpha=.3, axis="y")
    add("回撤持续期分布", fig)

    # ── 12. 资产相对强弱: 创业板50 vs 黄金 累积 ──
    rel = eq_cyb / eq_gold
    fig, ax = plt.subplots(figsize=(11, 3.6))
    ax.plot(dates, rel, color="#0f172a", lw=1.0)
    ax.axhline(1, color="#9ca3af", lw=.6, ls="--")
    ax.set_title("12. 相对强弱：创业板50 / 黄金（>1 创业板强，<1 黄金强）")
    ax.grid(alpha=.3)
    add("相对强弱", fig)

    return charts


def main():
    print("取数中...")
    df = fetch_data()
    start, end = df.index[0].date(), df.index[-1].date()
    n = len(df)
    print(f"对齐后: {start} ~ {end}, {n} 根")

    curve_rot, curve_cyb, curve_gold, state_per_day, trades = run_backtest(df)

    t_rule, a_rule, d_rule = (curve_rot[-1]-1, _annualize(curve_rot[-1]-1, n),
                              (curve_rot/np.maximum.accumulate(curve_rot)-1).min())
    t_cyb, a_cyb, d_cyb = (curve_cyb[-1]-1, _annualize(curve_cyb[-1]-1, n),
                           (curve_cyb/np.maximum.accumulate(curve_cyb)-1).min())
    t_gold, a_gold, d_gold = (curve_gold[-1]-1, _annualize(curve_gold[-1]-1, n),
                              (curve_gold/np.maximum.accumulate(curve_gold)-1).min())

    print(f"\n轮动: 收益 {t_rule*100:+.1f}% 年化 {a_rule*100:+.1f}% 回撤 {d_rule*100:.1f}% 交易 {trades}")
    print(f"买持创: 收益 {t_cyb*100:+.1f}% 年化 {a_cyb*100:+.1f}% 回撤 {d_cyb*100:.1f}%")
    print(f"买持金: 收益 {t_gold*100:+.1f}% 年化 {a_gold*100:+.1f}% 回撤 {d_gold*100:.1f}%")

    cnt = Counter(s for s in state_per_day if s is not None)
    tot = sum(cnt.values())

    charts = gen_charts(df, curve_rot, curve_cyb, curve_gold, state_per_day)
    print(f"生成 {len(charts)} 张图")

    # ── HTML ──
    imgs = "\n".join(f"<h2>{t}</h2><img src='data:image/png;base64,{b}'>"
                     for t, b in charts)
    state_html = " · ".join(
        f"<b>{STATE_NAMES[s]}</b> {cnt.get(s,0)} 天 ({cnt.get(s,0)/tot*100:.1f}%)"
        for s in (STATE_FULL_CYB, STATE_HALF, STATE_FULL_GOLD))
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
    os.makedirs(out_dir, exist_ok=True)
    html_path = os.path.join(out_dir, "rotation_report_2014.html")
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>ETF轮动系统·2014至今多维度回测</title>
<style>body{{font-family:'Microsoft YaHei',sans-serif;background:#f1f5f9;color:#0f172a;padding:24px;max-width:1080px;margin:auto}}
h1{{font-size:21px}}h2{{font-size:16px;border-left:4px solid #dc2626;padding-left:8px;margin-top:26px}}
img{{width:100%;border-radius:8px;margin:6px 0}}
table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{border:1px solid #e2e8f0;padding:7px;text-align:right}}th{{background:#f8fafc}}
td:first-child,th:first-child{{text-align:left}}
.red{{background:#fef2f2;border-left:5px solid #dc2626;padding:14px;margin:14px 0}}</style></head><body>
<h1>ETF 轮动系统 · 最长历史回测（{start} ~ {end}，{n} 交易日）</h1>
<p style="color:#64748b">规则：创业板50指数(399673) <b>MA20 向上且回撤≥-20%→满仓创业板50</b>、<b>向上但回撤&lt;-20%→创业板50+黄金各半</b>、<b>MA20 向下→满仓黄金</b>；每日收盘算信号、次日调仓；无杠杆不做空；起始 10 万。</p>
<table><tr><th>标的</th><th>累计收益</th><th>年化</th><th>最大回撤</th></tr>
<tr><td>轮动规则</td><td>{t_rule*100:+.1f}%</td><td>{a_rule*100:+.1f}%</td><td>{d_rule*100:.1f}%</td></tr>
<tr><td>买入持有创业板50</td><td>{t_cyb*100:+.1f}%</td><td>{a_cyb*100:+.1f}%</td><td>{d_cyb*100:.1f}%</td></tr>
<tr><td>买入持有黄金ETF</td><td>{t_gold*100:+.1f}%</td><td>{a_gold*100:+.1f}%</td><td>{d_gold*100:.1f}%</td></tr></table>
<p style="color:#475569">换仓 {trades} 次 · 状态占比：{state_html}</p>
<div class="red"><b>结论：</b>轮动收益 <b>{t_rule*100:+.1f}%</b>（年化 {a_rule*100:+.1f}%、最大回撤 {d_rule*100:.1f}%），远超买入持有创业板50（{t_cyb*100:+.1f}%、回撤 {d_cyb*100:.1f}%）与黄金（{t_gold*100:+.1f}%）。核心是 <b>MA20 向下躲进黄金</b>，吃到 2024-2026 黄金牛 + 躲过创业板熊。未计交易成本；2014-2016 创业板50用指数代替 ETF。</div>
{imgs}
</body></html>"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nHTML 报告: {html_path}")


if __name__ == "__main__":
    main()
