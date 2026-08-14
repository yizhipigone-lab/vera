# -*- coding: utf-8 -*-
"""杂食性验证: v2.1 三个达标策略在 2012-01-01 ~ 2026-06-30 的长周期表现。

策略是在 2019-2026 区间调出来的, 2012~2018 是它从未见过的"新高考题"
(2015 股灾 / 2016 熔断 / 2018 大熊)。分段看:
  A段 2012-01-01 ~ 2018-12-31  纯样本外 (OOS)
  B段 2019-01-01 ~ 2026-06-30  样本内 (调参区间)
  全程 2012-01-01 ~ 2026-06-30  完整长周期

注意口径差异: 池子改为"缓存覆盖 2012 起"的股票 (比主循环的 2018 起池子小),
三段用同一个池子, 保证段间可比。

用法: python -X utf8 auto_iter/validate_2012.py
"""
import sys, os, json, sqlite3, copy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
from auto_iter.common import (enforce_offline, load_panel, seed_stock_info,
                              build_signals, run_backtest, shrink_panel,
                              MANIFEST_DB)
from auto_iter import auto_strategy_loop as m

FULL_START, FULL_END = "2012-01-01", "2026-06-30"
SEGMENTS = [("A段·样本外(2012-2018)", "2012-01-01", "2018-12-31"),
            ("B段·样本内(2019-2026.6)", "2019-01-01", "2026-06-30"),
            ("全程(2012-2026.6)", FULL_START, FULL_END)]

# v2.1 三个达标策略 (出自 iter_log_v2.csv, 参数逐字照抄)
SPECS = {
    "iter_2085": json.loads('''{"factors": [{"name": "new_high", "n": 12}, {"name": "break_ma", "n": 5}, {"name": "ma_bull", "fast": 5, "mid": 10, "slow": 20}], "stop": {"priority": "stop_first", "cost_stop": {"enabled": true, "threshold": -0.1127}, "trailing_stop": {"enabled": true, "activation": 0.0267, "drawdown": 0.0115, "confirm": "real"}, "ladder_tp": {"enabled": false, "levels": [{"profit": 0.0815, "sell_ratio": 0.1486}, {"profit": 0.115, "sell_ratio": 0.2306}]}, "time_stop": {"enabled": true, "max_hold_days": 23}, "cond_time_stop": {"enabled": false}, "first_day": {"enabled": false}, "formula_sell": {"enabled": false}}}'''),
    "iter_2086": json.loads('''{"factors": [{"name": "new_high", "n": 12}, {"name": "break_ma", "n": 5}, {"name": "ma_bull", "fast": 5, "mid": 10, "slow": 20}], "stop": {"priority": "stop_first", "cost_stop": {"enabled": true, "threshold": -0.0937}, "trailing_stop": {"enabled": true, "activation": 0.023, "drawdown": 0.0104, "confirm": "real"}, "ladder_tp": {"enabled": false, "levels": [{"profit": 0.0906, "sell_ratio": 0.1534}, {"profit": 0.1202, "sell_ratio": 0.2166}]}, "time_stop": {"enabled": true, "max_hold_days": 24}, "cond_time_stop": {"enabled": false}, "first_day": {"enabled": false}, "formula_sell": {"enabled": false}}}'''),
    "iter_2088": json.loads('''{"factors": [{"name": "new_high", "n": 10}, {"name": "break_ma", "n": 5}, {"name": "ma_bull", "fast": 5, "mid": 10, "slow": 20}], "stop": {"priority": "stop_first", "cost_stop": {"enabled": true, "threshold": -0.1151}, "trailing_stop": {"enabled": true, "activation": 0.0276, "drawdown": 0.0102, "confirm": "real"}, "ladder_tp": {"enabled": false, "levels": [{"profit": 0.084, "sell_ratio": 0.13}, {"profit": 0.1212, "sell_ratio": 0.237}]}, "time_stop": {"enabled": true, "max_hold_days": 22}, "cond_time_stop": {"enabled": false}, "first_day": {"enabled": false}, "formula_sell": {"enabled": false}}}'''),
}


def build_pool_2012() -> list:
    """缓存覆盖 [2012-01-01, 2026-06-30] 的股票 (比主循环 2018 起的池子小)。"""
    conn = sqlite3.connect(str(MANIFEST_DB))
    try:
        rows = conn.execute(
            "SELECT stock_code FROM manifest WHERE period='1d' "
            "AND first_date <= ? AND last_date >= ?",
            ("20120101", "20260630")).fetchall()
    finally:
        conn.close()
    return sorted(r[0] for r in rows)


def filter_pool_window(panel, start, end, amount_quantile=0.2,
                       min_price=1.0, max_stocks=3000):
    """与 common.filter_pool 同逻辑, 但统计窗口可自定义 (长周期版)。"""
    amt = panel["amount"].loc[start:end].mean(skipna=True)
    min_close = panel["close"].loc[start:end].min(skipna=True)
    ok = (min_close >= min_price) & (amt >= amt.quantile(amount_quantile))
    codes = amt[ok].sort_values(ascending=False).index.tolist()[:max_stocks]
    return sorted(codes)


def yearly(eq: pd.Series) -> dict:
    eq = eq.copy()
    eq.index = pd.to_datetime(eq.index)
    return {str(y): float(g.iloc[-1] / g.iloc[0] - 1)
            for y, g in eq.groupby(eq.index.year) if len(g) > 1}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    enforce_offline()
    candidates = build_pool_2012()
    print(f"覆盖2012起的候选 {len(candidates)} 只, 加载面板(2011-07起)...", flush=True)
    panel = load_panel(candidates, start="2011-07-01")
    codes = filter_pool_window(panel, FULL_START, FULL_END)
    panel = shrink_panel(panel, codes)
    seed_stock_info(codes)
    print(f"长周期股票池 {len(codes)} 只", flush=True)

    for name, spec in SPECS.items():
        entries = build_signals(panel, "right", spec["factors"])
        print(f"\n════════ {name} ════════", flush=True)
        for seg_name, s, e in SEGMENTS:
            res = run_backtest(panel, entries, spec["stop"],
                               copy.deepcopy(m.DEFAULT_BT_CFG),
                               bt_start=s, bt_end=e)
            mt = res["metrics"]
            n_sig = int(entries.loc[s:e].sum().sum())
            print(f"{seg_name}: 年化 {mt['annualized_return']:+.1%} "
                  f"回撤 {mt['max_drawdown']:+.1%} 夏普 {mt['sharpe_ratio']:.2f} "
                  f"胜率 {mt['win_rate']:.1%} PF {mt['profit_factor']:.2f} "
                  f"交易 {mt['total_trades']}笔 信号 {n_sig}", flush=True)
        # 全程逐年收益 + 月度信号均匀度
        res = run_backtest(panel, entries, spec["stop"],
                           copy.deepcopy(m.DEFAULT_BT_CFG),
                           bt_start=FULL_START, bt_end=FULL_END)
        eq = res["equity_curve"].set_index("date")["equity"]
        print("逐年:", {k: f"{v:+.0%}" for k, v in yearly(eq).items()})
        per_month = entries.loc[FULL_START:FULL_END].sum(axis=1)
        per_month = per_month.groupby(per_month.index.to_period("M")).sum()
        print(f"月信号: 最少 {int(per_month.min())} / 中位 "
              f"{int(per_month.median())} / 最多 {int(per_month.max())}, "
              f"<10的月数 {int((per_month < 10).sum())}/{len(per_month)}")


if __name__ == "__main__":
    main()
