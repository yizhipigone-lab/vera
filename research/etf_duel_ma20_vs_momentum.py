"""MA20三态 vs 4周动量 —— 同成本口径正面对决 (2026-08-23)。

问题: 到底是 MA20 三态好, 还是动量基线换仓好?
窗口: 主窗口 2010-06 (创业板指上市) 至今, 指数/现货代理版;
      稳健窗口 2014-06 (创业板50指数上市) 至今, 真实ETF价格版。

对决双方 (各自沿用其研究脚本的信号口径, 成本统一):
  A1 三态·黄金避险     : MA20方向 + 250日回撤三态, 日频信号 t-1 应用 t
                         (复用 trade/rotation.compute_signal), 避险=黄金
  A2 三态·金+纳指各半   : 同 A1, 避险篮子 = 50%黄金 + 50%纳指 (12年研究改良版)
  B1 动量·现金避险     : 创业板×纳指 4周动量择腿, 周五收盘信号、周一开盘成交,
                         双腿≤0 → 现金(年化2%)  [dd_sweep 文档基线口径]
  B2 动量·黄金避险     : 同 B1, 避险=黄金  [实盘 2026-08-20 上线版口径]

成本口径 (统一, 双方一致):
  每次 weight 变化, cost = sum(|Δw|) × 0.001 (万10佣金, 按总成交金额双边计)。
  全仓换腿 = 0.2%, 三态满↔半 = 0.1%。
  注: 旧动量基线脚本 COST=0.001 定义后从未使用 (未扣成本), 本脚本统一补扣。

数据 (主窗口, 代理版):
  创业板 = 399006 创业板指 (腾讯, 2010-06-02 起)
  纳指   = IXIC 纳指综合指数 (sina, USD, 忽略汇率 → 对纳指腿略乐观, 即对动量略有利)
  黄金   = AU0 SHFE黄金主力连续 (sina, CNY计价, 与518880跟踪的SGE金价同源)
数据 (稳健窗口, ETF版):
  创业板 = 399673 创业板50指数 (腾讯), 纳指 = 513100 前复权, 黄金 = 518880 前复权
  (对齐旧12年研究口径: 旧报告三态黄金版 +1923.1%/-29.0% 未计成本, 本脚本计成本后会略低)

数据缓存: research/etf_duel_ma20_vs_momentum/data/*.csv (首次拉取后离线复跑)。
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

from trade.legacy_three_state import STATE_RATIOS, compute_signal

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "etf_duel_ma20_vs_momentum")
DATA_DIR = os.path.join(OUT_DIR, "data")
COST = 0.001            # 万10, 按 sum(|Δw|) 计
CASH_ANN = 0.02         # 现金年化
MA, HIGH, DD = 20, 250, 0.20   # 三态参数 (与生产/研究一致)
MOM_WEEKS = 4


# ─────────────────────────── 数据 ───────────────────────────

def _load_tx_index(symbol: str) -> pd.Series:
    """腾讯指数日线 → 收盘/开盘 Series。"""
    import akshare as ak
    df = ak.stock_zh_index_daily_tx(symbol=symbol)
    df = df.set_index(pd.to_datetime(df["date"]))
    return df["close"], df["open"]


def _load_sina_us(symbol: str) -> pd.Series:
    """sina 美股指数日线。"""
    import akshare as ak
    df = ak.index_us_stock_sina(symbol=symbol)
    df = df.set_index(pd.to_datetime(df["date"]))
    return df["close"], df["open"]


def _load_au0() -> pd.Series:
    """SHFE 黄金主力连续 (CNY)。"""
    import akshare as ak
    df = ak.futures_main_sina(symbol="AU0", start_date="20100101",
                              end_date="20260823")
    df = df.set_index(pd.to_datetime(df["日期"]))
    return df["收盘价"], df["开盘价"]


def _load_etf(code: str, start: str) -> pd.Series:
    """EM 场内 ETF 前复权日线。"""
    import akshare as ak
    df = ak.fund_etf_hist_em(symbol=code, period="daily", start_date=start,
                             end_date="20260823", adjust="qfq")
    df = df.set_index(pd.to_datetime(df["日期"]))
    return df["收盘"], df["开盘"]


def fetch_all():
    os.makedirs(DATA_DIR, exist_ok=True)
    jobs = {
        "399006_close": ("tx", "sz399006"), "399006_open": ("tx", "sz399006"),
    }
    # 用 (name, loader) 逐个缓存: 每个源存一个双列 CSV (close/open)
    sources = {
        "idx399006": lambda: _load_tx_index("sz399006"),
        "idx399673": lambda: _load_tx_index("sz399673"),
        "ixic": lambda: _load_sina_us(".ixic"),
        "au0": lambda: _load_au0(),
        "etf513100": lambda: _load_etf("513100", "20130101"),
        "etf518880": lambda: _load_etf("518880", "20130101"),
    }
    out = {}
    for name, loader in sources.items():
        path = os.path.join(DATA_DIR, f"{name}.csv")
        if os.path.exists(path):
            df = pd.read_csv(path, index_col=0, parse_dates=True)
        else:
            close, open_ = loader()
            df = pd.DataFrame({"close": close, "open": open_})
            df.to_csv(path, encoding="utf-8")
            print(f"[拉取] {name}: {df.index[0].date()} ~ {df.index[-1].date()} ({len(df)} 根)")
        out[name] = df
    return out


# ─────────────────────────── 指标 ───────────────────────────

def metrics(ret: pd.Series, freq_per_year: int, curve_start: float = 1.0):
    curve = (1.0 + ret).cumprod() * curve_start
    total = curve.iloc[-1] / curve_start - 1.0
    n = len(ret)
    years = n / freq_per_year
    ann = (1.0 + total) ** (1.0 / years) - 1.0 if years > 0 else 0.0
    dd = float((curve / curve.cummax() - 1.0).min())
    sd = ret.std(ddof=1)
    sharpe = float(ret.mean() / sd * np.sqrt(freq_per_year)) if sd > 0 else 0.0
    calmar = ann / abs(dd) if dd < 0 else 0.0
    return dict(total=total, ann=ann, dd=dd, sharpe=sharpe, calmar=calmar,
                win=float((ret > 0).mean()), n=n)


def yearly(ret: pd.Series, freq_per_year: int) -> pd.Series:
    """收益序列 → 逐年收益。周频记在信号周所在年。"""
    g = (1.0 + ret).groupby(ret.index.year).prod() - 1.0
    return g


# ─────────────────────────── A 组: MA20 三态 (日频) ───────────────────────────

def run_tri_state(close_cyb: pd.Series, hedge_close: dict, label: str):
    """hedge_close: {腿名: 收盘Series}; 避险篮子内等权 (A1 单腿=100%, A2 双腿=各50%)。

    信号 t-1 收盘数据 → t 日应用; 状态变化收 cost=sum(|Δw|)*COST。
    数据不足250根时按旧约定满仓创业板 (hedge_sweep 口径)。
    """
    legs = {"cyb": close_cyb}
    legs.update(hedge_close)
    df = pd.concat(legs, axis=1, join="inner").dropna()
    if len(df) < 10:
        return None
    n = len(df)
    closes = df["cyb"].values
    hedge_names = list(hedge_close.keys())
    k = len(hedge_names)
    curve = np.empty(n)
    e = 1.0
    last_w = None
    trades = 0
    total_cost = 0.0
    rets = np.empty(n)
    for i in range(n):
        if i == 0:
            w = {"cyb": 1.0}
            for h in hedge_names:
                w[h] = 0.0
        else:
            hist = closes[:i]
            if len(hist) >= HIGH:
                st = compute_signal(list(hist), MA, HIGH, DD)["state"]
                wc, wh = STATE_RATIOS.get(st, (1.0, 0.0))
            else:
                st, wc, wh = None, 1.0, 0.0
            w = {"cyb": wc}
            for h in hedge_names:
                w[h] = wh / k
        # 当日收益
        r = 0.0
        for leg, wt in w.items():
            if wt != 0 and i >= 1:
                r += wt * (df[leg].iloc[i] / df[leg].iloc[i - 1] - 1.0)
        # 换手成本
        if last_w is not None:
            turn = sum(abs(w[leg] - last_w.get(leg, 0.0)) for leg in w)
            if turn > 1e-9:
                c = turn * COST
                r -= c
                total_cost += c
                trades += 1
        e *= (1.0 + r)
        curve[i] = e
        rets[i] = r
        last_w = w
    ret = pd.Series(rets, index=df.index)
    ret.iloc[0] = 0.0
    mm = metrics(ret, 252)
    mm.update(trades=trades, total_cost=total_cost,
              start=df.index[0], end=df.index[-1], label=label)
    return ret, mm


# ─────────────────────────── B 组: 4周动量 (周频) ───────────────────────────

def run_momentum(close: pd.DataFrame, open_: pd.DataFrame, hedge: str,
                 hedge_close: pd.Series, hedge_open: pd.Series, label: str):
    """周五收盘算动量 → 周一开盘成交; 持仓收益 = 下周一开盘/本周一开盘。

    hedge: 'cash'(年化2%) 或 'gold'(满仓避险腿)。
    """
    dfc = close.dropna()
    legs = list(dfc.columns)          # [创业板, 纳指]
    dates = dfc.index
    fridays = [d for d in dates if d.weekday() == 4]
    mondays = [d for d in dates if d.weekday() == 0]

    # 周五动量: 间隔 MOM_WEEKS 个周五
    mom = pd.DataFrame(index=fridays, columns=legs, dtype=float)
    for i, f in enumerate(fridays):
        if i >= MOM_WEEKS:
            f0 = fridays[i - MOM_WEEKS]
            mom.loc[f] = [dfc.at[f, a] / dfc.at[f0, a] - 1.0 for a in legs]

    def px_open(d, leg):
        if leg == "cash":
            return None
        if leg == "gold":
            return hedge_open.reindex([d]).iloc[0] if d in hedge_open.index else np.nan
        s = open_[leg]
        return s.loc[d] if d in s.index else np.nan

    pos = None
    entry_leg = None
    weekly = {}
    switches = 0
    total_cost = 0.0
    for i in range(len(mondays) - 1):
        M, Mn = mondays[i], mondays[i + 1]
        cand = [f for f in fridays if f <= M]
        f = cand[-1] if cand else None
        target = None
        if f is not None and f in mom.index and not pd.isna(mom.loc[f]).any():
            vals = mom.loc[f]
            best = vals.idxmax()
            target = best if vals[best] > 0 else ("cash" if hedge == "cash" else "gold")
        else:
            target = "cash" if hedge == "cash" else "gold"   # fail-safe
        turn = 0.0
        if target != pos:
            turn = 2.0 if pos is not None else 1.0
            total_cost += turn * COST
            switches += 1
            pos = target
        # 周收益
        if pos == "cash":
            r = (1.0 + CASH_ANN) ** (5.0 / 252.0) - 1.0
        else:
            p0 = px_open(M, pos)
            p1 = px_open(Mn, pos)
            if p0 is None or p1 is None or pd.isna(p0) or pd.isna(p1) or p0 <= 0:
                r = (1.0 + CASH_ANN) ** (5.0 / 252.0) - 1.0
            else:
                r = p1 / p0 - 1.0
        weekly[M] = r - turn * COST   # 切换成本并入当周
    w = pd.Series(weekly)
    # 成本并入对应周
    curve = np.empty(len(w))
    e = 1.0
    for j, (d, r) in enumerate(w.items()):
        e *= (1.0 + r)
        curve[j] = e
    ret = pd.Series(w.values, index=w.index)
    mm = metrics(ret, 52)
    mm.update(trades=switches, total_cost=total_cost,
              start=mondays[0], end=mondays[-2] if len(mondays) > 1 else mondays[0],
              label=label)
    return ret, mm


# ─────────────────────────── 汇总 ───────────────────────────

def fmt_row(name, mm):
    return (f"{name:<22} 累计{mm['total']*100:>+9.1f}% 年化{mm['ann']*100:>+7.2f}% "
            f"回撤{mm['dd']*100:>7.1f}% Calmar{mm['calmar']:>6.2f} 夏普{mm['sharpe']:>5.2f} "
            f"切换{mm['trades']:>4}次 成本拖累{mm['total_cost']*100:>6.2f}%")


def bench_and_hold(df_close: pd.DataFrame, legs_label):
    out = []
    for col in df_close.columns:
        s = df_close[col].dropna()
        ret = s.pct_change().dropna()
        mm = metrics(ret, 252)
        mm.update(trades=0, total_cost=0.0, start=ret.index[0], end=ret.index[-1])
        out.append((legs_label.get(col, col), ret, mm))
    return out


def main():
    data = fetch_all()

    # ══ 主窗口: 2010-06 起, 指数/现货代理版 ══
    cyb_c = data["idx399006"]["close"]
    cyb_o = data["idx399006"]["open"]
    nas_c = data["ixic"]["close"]
    nas_o = data["ixic"]["open"]
    au_c = data["au0"]["close"]
    au_o = data["au0"]["open"]

    results = []

    r, mm = run_tri_state(cyb_c, {"gold": au_c}, "A1 三态·黄金避险")
    results.append(("A1 三态·黄金避险", r, mm))
    r, mm = run_tri_state(cyb_c, {"gold": au_c, "nas": nas_c}, "A2 三态·金+纳指各半避险")
    results.append(("A2 三态·金+纳指各半避险", r, mm))

    close2 = pd.concat([cyb_c.rename("cyb"), nas_c.rename("nas")], axis=1, join="inner").dropna()
    open2 = pd.concat([cyb_o.rename("cyb"), nas_o.rename("nas")], axis=1, join="inner").reindex(close2.index)
    r, mm = run_momentum(close2, open2, "cash", au_c, au_o, "B1 动量·现金避险")
    results.append(("B1 动量·现金避险(基线口径)", r, mm))
    r, mm = run_momentum(close2, open2, "gold", au_c, au_o, "B2 动量·黄金避险(实盘口径)")
    results.append(("B2 动量·黄金避险(实盘口径)", r, mm))

    # 基准
    bench = bench_and_hold(pd.DataFrame({"创业板指": cyb_c, "纳指": nas_c, "黄金": au_c}),
                           {"创业板指": "基准·创业板指B&H", "纳指": "基准·纳指B&H",
                            "黄金": "基准·黄金B&H"})

    print("═" * 100)
    print(f"主窗口 (指数代理版): {results[0][2]['start'].date()} ~ {results[0][2]['end'].date()}  "
          f"统一成本 万10×换手, 现金年化2%")
    print("═" * 100)
    for name, r, mm in results:
        print(fmt_row(name, mm))
    print("-" * 100)
    for name, r, mm in bench:
        print(fmt_row(name, mm))

    # 逐年对比
    yr = pd.DataFrame({name: yearly(r, 52 if '动量' in name else 252)
                       for name, r, mm in results})
    print("\n=== 逐年收益 (主窗口) ===")
    print((yr * 100).round(1).to_string())

    # ══ 稳健窗口: 2014-06 起, 真实ETF价格版 ══
    cyb50_c = data["idx399673"]["close"]
    cyb50_o = data["idx399673"]["open"]
    etf_nas_c = data["etf513100"]["close"]
    etf_nas_o = data["etf513100"]["open"]
    etf_au_c = data["etf518880"]["close"]
    etf_au_o = data["etf518880"]["open"]

    common = cyb50_c.index.intersection(etf_nas_c.index).intersection(etf_au_c.index)
    common = common[common >= pd.Timestamp("2014-06-18")]
    cyb50_c, cyb50_o = cyb50_c.loc[common], cyb50_o.loc[common]
    etf_nas_c2, etf_nas_o2 = etf_nas_c.loc[common], etf_nas_o.loc[common]
    etf_au_c2, etf_au_o2 = etf_au_c.loc[common], etf_au_o.loc[common]

    results2 = []
    r, mm = run_tri_state(cyb50_c, {"gold": etf_au_c2}, "A1 三态·黄金避险")
    results2.append(("A1 三态·黄金避险", r, mm))
    r, mm = run_tri_state(cyb50_c, {"gold": etf_au_c2, "nas": etf_nas_c2},
                          "A2 三态·金+纳指各半避险")
    results2.append(("A2 三态·金+纳指各半避险", r, mm))
    close3 = pd.concat([cyb50_c.rename("cyb"), etf_nas_c2.rename("nas")], axis=1).dropna()
    open3 = pd.concat([cyb50_o.rename("cyb"), etf_nas_o2.rename("nas")], axis=1).reindex(close3.index)
    r, mm = run_momentum(close3, open3, "cash", etf_au_c2, etf_au_o2, "B1 动量·现金避险")
    results2.append(("B1 动量·现金避险(基线口径)", r, mm))
    r, mm = run_momentum(close3, open3, "gold", etf_au_c2, etf_au_o2, "B2 动量·黄金避险(实盘口径)")
    results2.append(("B2 动量·黄金避险(实盘口径)", r, mm))

    print("\n" + "═" * 100)
    print(f"稳健窗口 (真实ETF价格版): {results2[0][2]['start'].date()} ~ {results2[0][2]['end'].date()}")
    print("═" * 100)
    for name, r, mm in results2:
        print(fmt_row(name, mm))

    yr2 = pd.DataFrame({name: yearly(r, 52 if '动量' in name else 252)
                        for name, r, mm in results2})
    print("\n=== 逐年收益 (稳健窗口) ===")
    print((yr2 * 100).round(1).to_string())

    # 落盘
    os.makedirs(OUT_DIR, exist_ok=True)
    rows = []
    for win, rs in (("主窗口2010·代理版", results), ("稳健窗口2014·ETF版", results2)):
        for name, r, mm in rs:
            rows.append({"window": win, "strategy": name,
                         "total": mm["total"], "ann": mm["ann"], "dd": mm["dd"],
                         "calmar": mm["calmar"], "sharpe": mm["sharpe"],
                         "trades": mm["trades"], "total_cost": mm["total_cost"]})
    pd.DataFrame(rows).to_csv(os.path.join(OUT_DIR, "duel_summary.csv"),
                              index=False, encoding="utf-8-sig")
    yr.round(4).to_csv(os.path.join(OUT_DIR, "duel_yearly_2010.csv"), encoding="utf-8-sig")
    yr2.round(4).to_csv(os.path.join(OUT_DIR, "duel_yearly_2014.csv"), encoding="utf-8-sig")
    print(f"\nCSV 落盘: {OUT_DIR}")


if __name__ == "__main__":
    main()
