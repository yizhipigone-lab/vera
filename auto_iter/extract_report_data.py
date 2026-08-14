# -*- coding: utf-8 -*-
"""报告数据提取: 关键策略的全量 metrics + 逐年收益 + 最大回撤区间。

输出 JSON 到 stdout, 供撰写研究报告使用 (一次性脚本)。
用法: python -X utf8 auto_iter/extract_report_data.py
"""
import sys, os, json, copy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from auto_iter.common import (enforce_offline, build_pool_candidates, load_panel,
                              filter_pool, shrink_panel, seed_stock_info,
                              build_signals, run_backtest)
from auto_iter import auto_strategy_loop as m

KEY_ITERS = [1092, 1087, 1079, 1086, 828, 967, 874, 575]


def yearly_returns(equity: pd.Series) -> dict:
    """按自然年切分权益曲线算每年收益 (含首尾残段)。"""
    eq = equity.copy()
    eq.index = pd.to_datetime(eq.index)
    out = {}
    for y, g in eq.groupby(eq.index.year):
        if len(g) < 2:
            continue
        out[str(y)] = round(float(g.iloc[-1] / g.iloc[0] - 1), 4)
    return out


def worst_dd_window(equity: pd.Series) -> dict:
    """最大回撤的起峰/谷底/修复日期。"""
    eq = equity.copy()
    eq.index = pd.to_datetime(eq.index)
    peak = eq.cummax()
    dd = eq / peak - 1.0
    trough = dd.idxmin()
    start = eq.loc[:trough].idxmax()
    after = eq.loc[trough:]
    rec = after[after >= eq.loc[start]]
    return {"peak_date": str(start.date()), "trough_date": str(trough.date()),
            "max_dd": round(float(dd.min()), 4),
            "recovered": str(rec.index[0].date()) if len(rec) else "未修复",
            "trough_to_end_dd": round(float(dd.iloc[-1]), 4)}


def main():
    enforce_offline()
    args = m.parse_args([])
    pool_params = {"amount_quantile": args.amount_quantile,
                   "min_price": 1.0, "max_stocks": args.pool_size}
    candidates = build_pool_candidates()
    panel = load_panel(candidates)
    codes = filter_pool(panel, **pool_params)
    panel = shrink_panel(panel, codes)
    seed_stock_info(codes)

    df = pd.read_csv("output/auto_iter/iter_log.csv")
    result = {}
    for it in KEY_ITERS:
        row = df[df["iter"] == it].sort_values("annualized_return",
                                               ascending=False).iloc[0]
        spec = {"side": {"左侧": "left", "右侧": "right"}[row["side"]],
                "factors": json.loads(row["factors"]),
                "stop": json.loads(row["stop_config"]),
                "bt_cfg": m.DEFAULT_BT_CFG, "pool": dict(pool_params)}
        entries = build_signals(panel, spec["side"], spec["factors"])
        res = run_backtest(panel, entries, spec["stop"], spec["bt_cfg"])
        mt = res["metrics"]
        eq = res.get("equity_curve")
        # run_cached 返回 DataFrame (date/equity/drawdown), 取 equity 列为时间序列
        eq_s = eq.set_index("date")["equity"] if isinstance(eq, pd.DataFrame) \
            else pd.Series(eq)
        result[str(it)] = {
            "script_file": row["script_file"],
            "factors": spec["factors"], "stop": spec["stop"],
            "n_signals": int(entries.loc[m.BT_START:m.BT_END].sum().sum()),
            "metrics": {k: (round(v, 4) if isinstance(v, float) else v)
                        for k, v in mt.items()},
            "yearly": yearly_returns(eq_s),
            "worst_dd": worst_dd_window(eq_s),
        }
        print(f"iter {it} done", file=sys.stderr, flush=True)
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
