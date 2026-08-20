"""两腿动量 + 空仓改买黄金 + 日频15%止损 —— 2024年起重点看 (2026-08-20)。

规则改动: 原「都≤0 → 空仓现金(2%)」改为「都≤0 → 买入黄金ETF」;
日频移动止损触发的「空仓现金」也改为「转黄金」。黄金=避险替代现金, 不参与动量选腿。
对比: 现金版 vs 黄金版, 全窗口 + 2024起逐年。
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

NAS = "513100.SH"
GOLD = "518880.SH"
COST = 0.001
CASH_DAILY = (1.02) ** (1.0 / 252.0) - 1.0
MOM_WEEKS = 4
STOP_X = 0.15
INIT = 1_000_000.0


def run(dfc, hedge="cash"):
    """hedge='cash' 空仓现金2%; hedge='gold' 空仓买黄金。"""
    dates = dfc.index
    cyb, nas = dfc.columns[0], dfc.columns[1]
    has_gold = "gold" in dfc.columns
    ret = dfc.pct_change().fillna(0.0)
    fridays = [d for d in dates if d.weekday() == 4]
    mondays = set(d for d in dates if d.weekday() == 0)

    mom = {}
    for a in (cyb, nas):
        s = []
        for i, f in enumerate(fridays):
            if i >= MOM_WEEKS:
                f0 = fridays[i - MOM_WEEKS]
                s.append(dfc.at[f, a] / dfc.at[f0, a] - 1.0)
            else:
                s.append(np.nan)
        mom[a] = pd.Series(s, index=fridays)
    mom_df = pd.DataFrame(mom)
    safe = "gold" if (hedge == "gold" and has_gold) else "cash"

    def decide(t):
        cand = [f for f in mom_df.index if f <= t]
        if not cand:
            return safe
        row = mom_df.loc[cand[-1]]
        valid = row[row.notna()]
        if valid.empty:
            return safe
        best = valid.idxmax()
        return best if valid[best] > 0 else safe

    pos = safe
    entry_high = 0.0
    equity = INIT
    eq = pd.Series(index=dates, dtype=float)
    eq.iloc[0] = INIT
    for i in range(len(dates) - 1):
        t = dates[i]
        t_next = dates[i + 1]
        if pos == "cash":
            equity *= (1.0 + CASH_DAILY)
        else:
            equity *= (1.0 + ret.at[t_next, pos])
        eq.iloc[i + 1] = equity
        if pos in (cyb, nas):   # 只对风险腿做日频止损, 黄金/现金不止损
            c = dfc.at[t, pos]
            entry_high = max(entry_high, c)
            if c < entry_high * (1.0 - STOP_X):
                pos = safe
                equity *= (1.0 - COST)
                entry_high = 0.0
        if t in mondays:
            target = decide(t)
            if target != pos:
                pos = target
                equity *= (1.0 - COST)
                entry_high = dfc.at[t, target] if target in (cyb, nas) else 0.0
    return eq.dropna()


def report(name, eq, from_year=None):
    e = eq if from_year is None else eq[eq.index.year >= from_year]
    total = e.iloc[-1] / e.iloc[0] - 1.0
    ny = len(e) / 252.0
    ann = (1.0 + total) ** (1.0 / ny) - 1.0
    dd = float((e / e.cummax() - 1.0).min())
    calmar = ann / abs(dd) if dd < 0 else 0.0
    r = e.pct_change().dropna()
    sharpe = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else 0.0
    print(f"{name:<24} 累计{total*100:>+8.1f}% 年化{ann*100:>+6.1f}% "
          f"回撤{dd*100:>7.1f}% Calmar{calmar:>6.2f} 夏普{sharpe:>5.2f}")


def yearly_table(eq, label):
    print(f"\n--- {label} 逐年 ---")
    print(f"{'年份':<6}{'年初金额':>14}{'年末金额':>14}{'当年收益':>13}{'当年收益率':>11}")
    for y in sorted(set(eq.index.year)):
        seg = eq[eq.index.year == y]
        s, e = seg.iloc[0], seg.iloc[-1]
        print(f"{y:<6}{s:>14,.0f}{e:>14,.0f}{e-s:>+13,.0f}{(e/s-1)*100:>+10.1f}%")


def main():
    kl = DataFetcher.get_kline([NAS, GOLD, "159949.SZ"], "20130101", "20260819",
                               period="1d", dividend_type="front",
                               use_cache=True, force_refresh=True)
    close = kl["Close"]
    import akshare as ak
    idx = ak.stock_zh_index_daily_tx(symbol="sz399673")
    idx = idx.set_index("date")
    idx.index = pd.to_datetime(idx.index)
    nas = close[NAS].dropna()
    gold = close[GOLD].dropna()
    dfc = pd.concat([idx["close"].rename("cyb"), nas, gold], axis=1, join="inner").dropna()
    dfc.columns = ["cyb", "nas", "gold"]

    eq_cash = run(dfc, hedge="cash")
    eq_gold = run(dfc, hedge="gold")

    print("=== 全窗口 (2014-06 起) ===")
    report("现金版(空仓2%)", eq_cash)
    report("黄金版(空仓买黄金)", eq_gold)
    print("\n=== 2024 年起 ===")
    report("现金版 2024起", eq_cash, 2024)
    report("黄金版 2024起", eq_gold, 2024)

    yearly_table(eq_gold, "黄金版(空仓买黄金) 逐年")


if __name__ == "__main__":
    main()
