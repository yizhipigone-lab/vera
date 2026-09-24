"""日频移动止损 + 双资产各半 —— 降回撤补充测试 (2026-08-20)。

前面周频移动止损没生效(被动量规则自身"≤0空仓"覆盖)。这里改日频:
  每日收盘检查「持仓腿从持仓期最高收盘回撤 X%」→ 立即空仓 (不等周五)。
另测: 双资产各半 (两腿动量都>0时各持一半, 分散降回撤)。
日频模拟: 日收盘对日收盘收益, 位置在收盘后调整(次日生效)。
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
COST = 0.001
CASH_DAILY = (1.02) ** (1.0 / 252.0) - 1.0
MOM_WEEKS = 4


def run_daily(df_close, stop_x=None, split=False):
    """日频模拟。stop_x: 日频移动止损回撤阈值; split: 两腿都>0时各半。"""
    dates = df_close.index
    cyb, nas = df_close.columns[0], df_close.columns[1]
    ret = df_close.pct_change().fillna(0.0)
    fridays = [d for d in dates if d.weekday() == 4]
    mondays = set(d for d in dates if d.weekday() == 0)
    mom_rows = []
    for i, f in enumerate(fridays):
        if i >= MOM_WEEKS:
            f0 = fridays[i - MOM_WEEKS]
            mom_rows.append([df_close.at[f, cyb] / df_close.at[f0, cyb] - 1.0,
                             df_close.at[f, nas] / df_close.at[f0, nas] - 1.0])
        else:
            mom_rows.append([np.nan, np.nan])
    mom = pd.DataFrame(mom_rows, index=fridays, columns=[cyb, nas])

    def decide(t):
        cand = [f for f in mom.index if f <= t]
        if not cand:
            return "cash"
        m_c, m_n = mom.loc[cand[-1]]
        if np.isnan(m_c) or np.isnan(m_n):
            return "cash"
        if split:
            if m_c > 0 and m_n > 0:
                return "both"
            if m_c > 0:
                return cyb
            if m_n > 0:
                return nas
            return "cash"
        if m_c > 0 or m_n > 0:
            return cyb if m_c >= m_n else nas
        return "cash"

    pos = "cash"
    entry_high = 0.0
    switches = 0
    equity = 1.0
    for i in range(len(dates) - 1):
        t = dates[i]
        t_next = dates[i + 1]
        # 日收益 (当前持仓, t -> t_next)
        if pos == "cash":
            equity *= (1.0 + CASH_DAILY)
        elif pos == "both":
            equity *= (1.0 + (ret.at[t_next, cyb] + ret.at[t_next, nas]) / 2.0)
        else:
            equity *= (1.0 + ret.at[t_next, pos])
        # 收盘后: 更新最高价 + 日频移动止损
        c = df_close.at[t, pos] if pos in (cyb, nas) else None
        if c is not None:
            entry_high = max(entry_high, c)
            if stop_x and c < entry_high * (1.0 - stop_x):
                pos = "cash"; switches += 1; equity *= (1.0 - COST); entry_high = 0.0
        # 周一收盘后调仓
        if t in mondays:
            target = decide(t)
            if target != pos:
                pos = target; switches += 1; equity *= (1.0 - COST)
                entry_high = df_close.at[t, target] if target in (cyb, nas) else 0.0
                if target == "both":
                    entry_high = min(df_close.at[t, cyb], df_close.at[t, nas])

    # 期末估值
    n = len(dates)
    n_years = n / 252.0
    total = equity - 1.0
    ann = (1.0 + total) ** (1.0 / n_years) - 1.0
    # 回撤: 从日净值曲线算
    curve = np.empty(n)
    e = 1.0
    # 重放日净值 (简化: 用最终 equity 不够, 需重算曲线)
    return total, ann, switches, equity


def run_daily_full(df_close, stop_x=None, split=False):
    """完整日频回测, 返回 (总收益, 年化, 最大回撤, Calmar, 换腿)。"""
    dates = df_close.index
    cyb, nas = df_close.columns[0], df_close.columns[1]
    ret = df_close.pct_change().fillna(0.0)
    fridays = [d for d in dates if d.weekday() == 4]
    mondays = set(d for d in dates if d.weekday() == 0)
    mom_rows = []
    for i, f in enumerate(fridays):
        if i >= MOM_WEEKS:
            f0 = fridays[i - MOM_WEEKS]
            mom_rows.append([df_close.at[f, cyb] / df_close.at[f0, cyb] - 1.0,
                             df_close.at[f, nas] / df_close.at[f0, nas] - 1.0])
        else:
            mom_rows.append([np.nan, np.nan])
    mom = pd.DataFrame(mom_rows, index=fridays, columns=[cyb, nas])

    def decide(t):
        cand = [f for f in mom.index if f <= t]
        if not cand:
            return "cash"
        m_c, m_n = mom.loc[cand[-1]]
        if np.isnan(m_c) or np.isnan(m_n):
            return "cash"
        if split:
            if m_c > 0 and m_n > 0:
                return "both"
            if m_c > 0:
                return cyb
            if m_n > 0:
                return nas
            return "cash"
        if m_c > 0 or m_n > 0:
            return cyb if m_c >= m_n else nas
        return "cash"

    pos = "cash"
    entry_high = 0.0
    switches = 0
    curve = np.empty(len(dates))
    equity = 1.0
    curve[0] = 1.0
    for i in range(len(dates) - 1):
        t = dates[i]
        t_next = dates[i + 1]
        if pos == "cash":
            equity *= (1.0 + CASH_DAILY)
        elif pos == "both":
            equity *= (1.0 + (ret.at[t_next, cyb] + ret.at[t_next, nas]) / 2.0)
        else:
            equity *= (1.0 + ret.at[t_next, pos])
        curve[i + 1] = equity
        c = df_close.at[t, pos] if pos in (cyb, nas) else None
        if c is not None:
            entry_high = max(entry_high, c)
            if stop_x and c < entry_high * (1.0 - stop_x):
                pos = "cash"; switches += 1; equity *= (1.0 - COST); entry_high = 0.0
        if t in mondays:
            target = decide(t)
            if target != pos:
                pos = target; switches += 1; equity *= (1.0 - COST)
                entry_high = (df_close.at[t, target] if target in (cyb, nas)
                              else 0.0)
                if target == "both":
                    entry_high = min(df_close.at[t, cyb], df_close.at[t, nas])
    total = curve[-1] - 1.0
    n_years = len(dates) / 252.0
    ann = (1.0 + total) ** (1.0 / n_years) - 1.0
    dd = float((curve / np.maximum.accumulate(curve) - 1.0).min())
    calmar = ann / abs(dd) if dd < 0 else 0.0
    return total, ann, dd, calmar, switches


def main():
    kl = DataFetcher.get_kline([NAS, "159949.SZ"], "20130101", "20260819",
                               period="1d", dividend_type="front",
                               use_cache=True, force_refresh=True)
    close = kl["Close"]
    import akshare as ak
    idx = ak.stock_zh_index_daily_tx(symbol="sz399673")
    idx = idx.set_index("date")
    idx.index = pd.to_datetime(idx.index)
    nas_c = close[NAS].dropna()
    dfc = pd.concat([idx["close"].rename("399673"), nas_c], axis=1, join="inner").dropna()

    for name, kw in [
        ("基线(周频动量)", dict()),
        ("日频止损 8%", dict(stop_x=0.08)),
        ("日频止损 10%", dict(stop_x=0.10)),
        ("日频止损 15%", dict(stop_x=0.15)),
        ("双资产各半", dict(split=True)),
        ("双资产各半+日频止损10%", dict(split=True, stop_x=0.10)),
    ]:
        t, a, d, ca, sw = run_daily_full(dfc, **kw)
        print(f"{name:<18} 累计{t*100:>+8.1f}% 年化{a*100:>+6.1f}% "
              f"回撤{d*100:>7.1f}% Calmar{ca:>6.2f} 换腿{sw:>4}")


if __name__ == "__main__":
    main()
