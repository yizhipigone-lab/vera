"""ETF 轮动系统：避险腿换成 国债ETF(511090) 回测 + 同窗口黄金对比。

规则同 rotation_backtest.py (MA20 + 250日回撤三态), 仅把避险腿 518880 换成 000012 上证国债指数。
000012 覆盖 2014 至今 (国债类型里唯一能到 2014 的); 同时跑同窗口黄金做公平对比。
注: 000012 是净价指数 (不含利息再投资), 国债腿收益被低估 ~2-3%/年。
输出: output/rotation_report_bond_2014.html (12 张图)。
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
from matplotlib.colors import ListedColormap

from trade.rotation import (
    STATE_RATIOS, compute_signal,
    STATE_FULL_CYB, STATE_HALF, STATE_FULL_GOLD,
)

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

MA = 20
HIGH = 250
DD = 0.20
STATE_NAMES = {STATE_FULL_CYB: "满仓创业板50", STATE_HALF: "半仓(创+避险各半)",
               STATE_FULL_GOLD: "满仓避险", None: "数据不足"}
STATE_COLORS = {STATE_FULL_CYB: "#dc2626", STATE_HALF: "#f59e0b",
                STATE_FULL_GOLD: "#b8860b", None: "#9ca3af"}

HEDGE_CODE = "000012.SH"   # 上证国债指数 (净价指数, 覆盖 2014 至今)
HEDGE_NAME = "上证国债指数"
GOLD_CODE = "518880.SH"    # 黄金ETF (对比)


def fetch():
    import akshare as ak
    cyb = ak.stock_zh_index_daily_tx(symbol="sz399673")
    cyb = cyb.set_index("date")["close"].rename("cyb")
    cyb.index = pd.to_datetime(cyb.index)

    from core.data_fetcher import DataFetcher
    kl = DataFetcher.get_kline([HEDGE_CODE, GOLD_CODE], "20140101", "20260818",
                               period="1d", dividend_type="front", use_cache=True)
    hedge = kl["Close"][HEDGE_CODE].dropna().rename("hedge")
    hedge.index = pd.to_datetime(hedge.index)
    gold = kl["Close"][GOLD_CODE].dropna().rename("gold")
    gold.index = pd.to_datetime(gold.index)

    df = pd.concat([cyb, hedge, gold], axis=1, join="inner").dropna()
    return df


def run_backtest(cyb_arr, hedge_arr, ret_cyb, ret_hedge, n):
    curve_rot = np.empty(n)
    curve_cyb = np.empty(n)
    curve_hedge = np.empty(n)
    e_r = e_c = e_h = 1.0
    state_per_day = [None] * n
    trades = 0
    last_state = None
    for i in range(n):
        if i == 0:
            w_cyb, w_hedge, st = 1.0, 0.0, None
        else:
            hist = cyb_arr[:i]
            if len(hist) >= HIGH:
                sig = compute_signal(list(hist), MA, HIGH, DD)
                st = sig["state"]
                w_cyb, w_hedge = STATE_RATIOS.get(st, (1.0, 0.0))
            else:
                st = None
                w_cyb, w_hedge = 1.0, 0.0
        r = w_cyb * ret_cyb[i] + w_hedge * ret_hedge[i]
        e_r *= (1 + r)
        e_c *= (1 + ret_cyb[i])
        e_h *= (1 + ret_hedge[i])
        curve_rot[i], curve_cyb[i], curve_hedge[i] = e_r, e_c, e_h
        state_per_day[i] = st
        if st is not None and st != last_state:
            trades += 1
        last_state = st
    return curve_rot, curve_cyb, curve_hedge, state_per_day, trades


def _ann(total, n):
    return (1.0 + total) ** (252.0 / n) - 1.0


def _b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=105, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def gen_charts(df, curve_rot, curve_cyb, curve_hedge, state_per_day):
    dates = df.index
    charts = []

    def add(t, fig):
        charts.append((t, _b64(fig)))

    ret_rot = pd.Series(np.r_[0.0, np.diff(np.log(curve_rot))], index=dates)
    ret_cyb = pd.Series(np.r_[0.0, np.diff(np.log(curve_cyb))], index=dates)
    eq_rot = pd.Series(curve_rot, index=dates)
    eq_cyb = pd.Series(curve_cyb, index=dates)
    eq_hedge = pd.Series(curve_hedge, index=dates)

    # 1 净值
    fig, ax = plt.subplots(figsize=(11, 4.4))
    ax.plot(dates, curve_rot * 100, color="#dc2626", lw=1.3, label="轮动(避险=国债)")
    ax.plot(dates, curve_cyb * 100, color="#64748b", lw=.9, alpha=.85, label="买入持有创业板50")
    ax.plot(dates, curve_hedge * 100, color="#b8860b", lw=.9, alpha=.85, label="买入持有国债")
    ax.set_yscale("log")
    ax.set_title("1. 净值曲线（对数坐标，起始 100）")
    ax.legend(loc="upper left"); ax.grid(alpha=.3)
    add("净值曲线", fig)

    # 2 回撤
    dd_rot = curve_rot / np.maximum.accumulate(curve_rot) - 1
    dd_cyb = curve_cyb / np.maximum.accumulate(curve_cyb) - 1
    fig, ax = plt.subplots(figsize=(11, 3.2))
    ax.fill_between(dates, dd_rot * 100, 0, color="#dc2626", alpha=.4, label="轮动回撤")
    ax.plot(dates, dd_cyb * 100, color="#64748b", lw=.8, alpha=.9, label="买入持有回撤")
    ax.set_title("2. 回撤曲线（水下）")
    ax.legend(loc="lower left"); ax.grid(alpha=.3)
    add("回撤曲线", fig)

    # 3 状态时间线
    codes = np.array([{STATE_FULL_CYB: 2, STATE_HALF: 1, STATE_FULL_GOLD: 0, None: -1}[s]
                      for s in state_per_day])
    fig, ax = plt.subplots(figsize=(11, 2.2))
    cmap = ListedColormap(["#9ca3af", "#b8860b", "#f59e0b", "#dc2626"])
    ax.imshow(codes.reshape(1, -1), aspect="auto", cmap=cmap, vmin=-1, vmax=2,
              extent=[matplotlib.dates.date2num(dates[0]), matplotlib.dates.date2num(dates[-1]), 0, 1])
    ax.set_yticks([])
    ax.set_title("3. 信号状态时间线（灰=数据不足 / 金=满仓避险 / 橙=半仓 / 红=满仓创业板50）")
    add("信号状态时间线", fig)

    # 4 信号指标
    cyb_s = df["cyb"]
    ma20 = cyb_s.rolling(MA).mean()
    high250 = cyb_s.rolling(HIGH).max()
    fig, ax = plt.subplots(figsize=(11, 4.4))
    ax.plot(dates, cyb_s, color="#0f172a", lw=.9, label="399673 收盘")
    ax.plot(dates, ma20, color="#dc2626", lw=1.0, label="MA20")
    ax.plot(dates, high250, color="#64748b", lw=.8, alpha=.7, label="250日最高收盘")
    ax.plot(dates, high250 * (1 - DD), color="#b8860b", lw=.8, alpha=.7, label="回撤阈值(高点-20%)")
    ax.set_title("4. 信号指标：价格 / MA20 / 250日高点 / 20%回撤线")
    ax.legend(loc="upper left", ncol=2); ax.grid(alpha=.3)
    add("信号指标", fig)

    # 5 年度收益
    yr_rot = eq_rot.resample("YE").last().pct_change() * 100
    yr_cyb = eq_cyb.resample("YE").last().pct_change() * 100
    years = yr_rot.index.year
    x = np.arange(len(years))
    fig, ax = plt.subplots(figsize=(11, 4.0))
    w = .38
    ax.bar(x - w/2, yr_rot.values, w, color="#dc2626", label="轮动")
    ax.bar(x + w/2, yr_cyb.values, w, color="#64748b", label="买入持有创业板50")
    ax.axhline(0, color="#0f172a", lw=.6)
    ax.set_xticks(x); ax.set_xticklabels([str(y) for y in years])
    ax.set_title("5. 年度收益对比 (%)")
    ax.legend(); ax.grid(alpha=.3, axis="y")
    add("年度收益对比", fig)

    # 6 月度热力图
    m = ret_rot.resample("ME").apply(lambda s: (1+s).prod() - 1) * 100
    mdf = pd.DataFrame({"year": m.index.year, "month": m.index.month, "ret": m.values})
    pivot = mdf.pivot(index="year", columns="month", values="ret")
    fig, ax = plt.subplots(figsize=(11, 4.6))
    im = ax.imshow(pivot.values, cmap="RdYlGn", aspect="auto", vmin=-12, vmax=12)
    ax.set_xticks(range(12)); ax.set_xticklabels([f"{i+1}月" for i in range(12)])
    ax.set_yticks(range(len(pivot.index))); ax.set_yticklabels([str(y) for y in pivot.index])
    fig.colorbar(im, ax=ax, label="月度收益 %")
    ax.set_title("6. 月度收益热力图（红亏绿赚）")
    add("月度收益热力图", fig)

    # 7 滚动1年收益
    roll_rot = eq_rot / eq_rot.shift(252) - 1
    roll_cyb = eq_cyb / eq_cyb.shift(252) - 1
    fig, ax = plt.subplots(figsize=(11, 3.4))
    ax.plot(dates, roll_rot * 100, color="#dc2626", lw=1.0, label="轮动")
    ax.plot(dates, roll_cyb * 100, color="#64748b", lw=.8, alpha=.85, label="买入持有创业板50")
    ax.axhline(0, color="#0f172a", lw=.6)
    ax.set_title("7. 滚动 1 年收益 (%)")
    ax.legend(); ax.grid(alpha=.3)
    add("滚动1年收益", fig)

    # 8 滚动波动率
    vol_rot = ret_rot.rolling(252).std() * np.sqrt(252) * 100
    vol_cyb = ret_cyb.rolling(252).std() * np.sqrt(252) * 100
    fig, ax = plt.subplots(figsize=(11, 3.4))
    ax.plot(dates, vol_rot, color="#dc2626", lw=1.0, label="轮动")
    ax.plot(dates, vol_cyb, color="#64748b", lw=.8, alpha=.85, label="买入持有创业板50")
    ax.set_title("8. 滚动 1 年年化波动率 (%)")
    ax.legend(); ax.grid(alpha=.3)
    add("滚动波动率", fig)

    # 9 滚动夏普
    sh_rot = ret_rot.rolling(252).mean() / ret_rot.rolling(252).std() * np.sqrt(252)
    sh_cyb = ret_cyb.rolling(252).mean() / ret_cyb.rolling(252).std() * np.sqrt(252)
    fig, ax = plt.subplots(figsize=(11, 3.4))
    ax.plot(dates, sh_rot, color="#dc2626", lw=1.0, label="轮动")
    ax.plot(dates, sh_cyb, color="#64748b", lw=.8, alpha=.85, label="买入持有创业板50")
    ax.axhline(0, color="#0f172a", lw=.6)
    ax.set_title("9. 滚动 1 年夏普比率")
    ax.legend(); ax.grid(alpha=.3)
    add("滚动夏普", fig)

    # 10 日收益分布
    fig, ax = plt.subplots(figsize=(11, 3.4))
    ax.hist(ret_rot * 100, bins=100, color="#dc2626", alpha=.55, density=True, label="轮动")
    ax.hist(ret_cyb * 100, bins=100, color="#64748b", alpha=.45, density=True, label="买入持有创业板50")
    ax.set_title("10. 日收益分布 (%)")
    ax.legend(); ax.grid(alpha=.3, axis="y")
    add("日收益分布", fig)

    # 11 回撤持续期
    under = dd_rot < 0
    dur, cnt = [], 0
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
    ax.hist(dur, bins=30, color="#dc2626", alpha=.7)
    ax.set_title(f"11. 回撤持续期分布（交易日，共 {len(dur)} 次，最长 {max(dur) if dur else 0} 天）")
    ax.grid(alpha=.3, axis="y")
    add("回撤持续期分布", fig)

    # 12 相对强弱: 创业板50 / 国债
    rel = eq_cyb / eq_hedge
    fig, ax = plt.subplots(figsize=(11, 3.6))
    ax.plot(dates, rel, color="#0f172a", lw=1.0)
    ax.axhline(1, color="#9ca3af", lw=.6, ls="--")
    ax.set_title("12. 相对强弱：创业板50 / 国债")
    ax.grid(alpha=.3)
    add("相对强弱", fig)

    return charts


def _metrics(curve, n):
    return curve[-1]-1, _ann(curve[-1]-1, n), (curve/np.maximum.accumulate(curve)-1).min()


def main():
    print("取数中...")
    df = fetch()
    start, end = df.index[0].date(), df.index[-1].date()
    n = len(df)
    print(f"对齐后(避险=511090): {start} ~ {end}, {n} 根")

    cyb_arr = df["cyb"].values
    hedge_arr = df["hedge"].values
    gold_arr = df["gold"].values
    ret_cyb = np.r_[0.0, df["cyb"].pct_change().fillna(0).values[1:]]
    ret_hedge = np.r_[0.0, df["hedge"].pct_change().fillna(0).values[1:]]
    ret_gold = np.r_[0.0, df["gold"].pct_change().fillna(0).values[1:]]

    # 主: 避险腿 = 国债
    curve_rot, curve_cyb, curve_hedge, states, trades = \
        run_backtest(cyb_arr, hedge_arr, ret_cyb, ret_hedge, n)
    # 对比: 同窗口避险腿 = 黄金
    curve_rot_gold, _, curve_gold, _, trades_gold = \
        run_backtest(cyb_arr, gold_arr, ret_cyb, ret_gold, n)

    m_rot = _metrics(curve_rot, n)
    m_cyb = _metrics(curve_cyb, n)
    m_hedge = _metrics(curve_hedge, n)
    m_rot_gold = _metrics(curve_rot_gold, n)
    m_gold = _metrics(curve_gold, n)

    print(f"\n=== 窗口 {start} ~ {end} ({n} 交易日) ===")
    print(f"轮动(避险=国债): 收益 {m_rot[0]*100:+.1f}% 年化 {m_rot[1]*100:+.1f}% 回撤 {m_rot[2]*100:.1f}% 交易 {trades}")
    print(f"轮动(避险=黄金)   : 收益 {m_rot_gold[0]*100:+.1f}% 年化 {m_rot_gold[1]*100:+.1f}% 回撤 {m_rot_gold[2]*100:.1f}% 交易 {trades_gold}")
    print(f"买入持有创业板50 : 收益 {m_cyb[0]*100:+.1f}% 年化 {m_cyb[1]*100:+.1f}% 回撤 {m_cyb[2]*100:.1f}%")
    print(f"买入持有国债 : 收益 {m_hedge[0]*100:+.1f}% 年化 {m_hedge[1]*100:+.1f}% 回撤 {m_hedge[2]*100:.1f}%")
    print(f"买入持有黄金     : 收益 {m_gold[0]*100:+.1f}% 年化 {m_gold[1]*100:+.1f}% 回撤 {m_gold[2]*100:.1f}%")

    charts = gen_charts(df, curve_rot, curve_cyb, curve_hedge, states)
    print(f"生成 {len(charts)} 张图")

    imgs = "\n".join(f"<h2>{t}</h2><img src='data:image/png;base64,{b}'>" for t, b in charts)
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
    os.makedirs(out_dir, exist_ok=True)
    p = os.path.join(out_dir, "rotation_report_bond_2014.html")
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>ETF轮动·国债避险腿</title>
<style>body{{font-family:'Microsoft YaHei',sans-serif;background:#f1f5f9;color:#0f172a;padding:24px;max-width:1080px;margin:auto}}
h1{{font-size:21px}}h2{{font-size:16px;border-left:4px solid #dc2626;padding-left:8px;margin-top:26px}}
img{{width:100%;border-radius:8px;margin:6px 0}}
table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{border:1px solid #e2e8f0;padding:7px;text-align:right}}th{{background:#f8fafc}}
td:first-child,th:first-child{{text-align:left}}
.red{{background:#fef2f2;border-left:5px solid #dc2626;padding:14px;margin:14px 0}}</style></head><body>
<h1>ETF 轮动系统 · 避险腿换 国债ETF(511090)（{start} ~ {end}）</h1>
<p style="color:#64748b">规则：创业板50指数(399673) MA20 向上且回撤≥-20%→满仓创业板50；向上但回撤&lt;-20%→创业板50+避险各半；MA20 向下→满仓避险（本报告避险=国债ETF 511090）。每日收盘算信号次日调仓。注：511090 仅 2023-06 起有数据。</p>
<table><tr><th>策略</th><th>累计收益</th><th>年化</th><th>最大回撤</th></tr>
<tr><td>轮动(避险=国债)</td><td>{m_rot[0]*100:+.1f}%</td><td>{m_rot[1]*100:+.1f}%</td><td>{m_rot[2]*100:.1f}%</td></tr>
<tr><td>轮动(避险=黄金)</td><td>{m_rot_gold[0]*100:+.1f}%</td><td>{m_rot_gold[1]*100:+.1f}%</td><td>{m_rot_gold[2]*100:.1f}%</td></tr>
<tr><td>买入持有创业板50</td><td>{m_cyb[0]*100:+.1f}%</td><td>{m_cyb[1]*100:+.1f}%</td><td>{m_cyb[2]*100:.1f}%</td></tr>
<tr><td>买入持有国债</td><td>{m_hedge[0]*100:+.1f}%</td><td>{m_hedge[1]*100:+.1f}%</td><td>{m_hedge[2]*100:.1f}%</td></tr>
<tr><td>买入持有黄金</td><td>{m_gold[0]*100:+.1f}%</td><td>{m_gold[1]*100:+.1f}%</td><td>{m_gold[2]*100:.1f}%</td></tr></table>
<div class="red"><b>结论：</b>同窗口({start}~{end})内, 避险腿=国债的轮动收益 <b>{m_rot[0]*100:+.1f}%</b>（回撤 {m_rot[2]*100:.1f}%），避险腿=黄金的轮动收益 <b>{m_rot_gold[0]*100:+.1f}%</b>（回撤 {m_rot_gold[2]*100:.1f}%）。未计交易成本。</div>
{imgs}
</body></html>"""
    with open(p, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nHTML 报告: {p}")


if __name__ == "__main__":
    main()
