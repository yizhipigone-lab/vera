"""
1d 日线参数扫描 (针对性复测, 2026-07-20)

针对年化>12% 的干净公式, 全A 3年 1d, 36 组合寻优.
区别 gs_5m_sweep: 1d 不需 5m 矩阵 prep, 直接 engine.run (get_kline 缓存命中).

用法:
  python tools/gs_1d_sweep.py --formulas 黑马选股1 --limit 1   # 测耗时
  python tools/gs_1d_sweep.py                                    # 全候选(从 candidates_12pct.json)
"""
import argparse
import json
import os
import sys
import time
import ctypes

import pandas as pd

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _THIS)

from quantqq_5m_sweep import (  # noqa: E402
    gen_coarse_combos, combo_key, combo_stop_config, CSV_COLUMNS,
)
from utils.config_loader import ConfigLoader  # noqa: E402
from selection.selector import StockSelector  # noqa: E402
from backtest.engine import BacktestEngine  # noqa: E402

OUT_BASE = "output/gs_1d_sweep"
PRIORITY = "trailing_first"  # 默认移动止盈优先; "stop_first"=止损优先


def run_formula(formula, start, end, universe, combos, bt_cfg):
    """单公式: 选股(1d 全A) + N 组合 engine.run. 返回 rows list."""
    os.makedirs(os.path.join(OUT_BASE, formula), exist_ok=True)
    sel_path = os.path.join(OUT_BASE, formula, "selections.csv")
    if os.path.exists(sel_path):
        sel = pd.read_csv(sel_path, dtype={"stock_code": str})
    else:
        sel_cfg = {"formula_name": formula, "formula_arg": "",
                   "universe": {"type": universe, "exclude_st": True,
                                "exclude_new_listings_days": 60,
                                "include_etf": False, "etf_only": False, "sectors": []},
                   "period": "1d", "dividend_type": 1}
        t0 = time.time()
        sel = StockSelector(sel_cfg).run(start_time=start, end_time=end)
        if sel is None or len(sel) == 0:
            return [{"formula": formula, "key": "", "status": "no_signals",
                      "elapsed": round(time.time() - t0, 1)}]
        sel.to_csv(sel_path, index=False)
        print(f"[{formula}] 选股 {len(sel)} 信号 {sel['stock_code'].nunique()}股 "
              f"({time.time()-t0:.0f}s)", flush=True)

    out_path = os.path.join(OUT_BASE, formula, "sweep_1d.csv")
    done = set()
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        prev = pd.read_csv(out_path)
        done = set(prev.loc[prev["annret"].notna(), "key"])
    header = not os.path.exists(out_path) or os.path.getsize(out_path) == 0

    engine = BacktestEngine(bt_cfg)
    rows = []
    t_start = time.time()
    with open(out_path, "a", encoding="utf-8", newline="") as fout:
        for i, c in enumerate(combos):
            key = combo_key(c)
            if key in done:
                continue
            t0 = time.time()
            try:
                res = engine.run(selections=sel, start_time=start, end_time=end,
                                 stop_config=combo_stop_config(c, PRIORITY))
                m = res["metrics"]
                row = {"formula": formula, "key": key,
                       "cost": c["cost"], "act": c["act"], "dd": c["dd"],
                       "ladder": c["ladder"], "time_days": c["time_days"],
                       "cond_days": c["cond_days"], "cond_profit": c["cond_profit"],
                       "cumret": m.get("cumulative_return", 0),
                       "annret": m.get("annualized_return", 0),
                       "maxdd": m.get("max_drawdown", 0),
                       "sharpe": m.get("sharpe_ratio", 0),
                       "calmar": m.get("calmar_ratio", 0),
                       "winrate": m.get("win_rate", 0),
                       "trades": m.get("total_trades", 0),
                       "profit_factor": m.get("profit_factor", 0),
                       "avg_hold": m.get("avg_hold_days", 0),
                       "elapsed": round(time.time() - t0, 1), "error": ""}
            except Exception as e:
                row = {"formula": formula, "key": key, "cost": c["cost"],
                       "act": c["act"], "dd": c["dd"], "ladder": c["ladder"],
                       "time_days": c["time_days"], "cond_days": c["cond_days"],
                       "cond_profit": c["cond_profit"],
                       "elapsed": round(time.time() - t0, 1),
                       "error": f"{type(e).__name__}: {str(e)[:80]}"}
            pd.DataFrame([row]).to_csv(fout, header=header, index=False)
            header = False
            fout.flush()
            rows.append(row)
            print(f"[{formula}] {i+1}/{len(combos)} {key} annret="
                  f"{row.get('annret',0)*100 if row.get('annret') else 0:.1f}% "
                  f"({row['elapsed']}s)", flush=True)
    print(f"[{formula}] DONE {len(rows)}组合 用时{(time.time()-t_start)/60:.1f}min")
    return rows


def main():
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--formulas", default=None, help="逗号分隔公式名")
    ap.add_argument("--candidates-file", default="output/gs_filter/candidates_12pct.json")
    ap.add_argument("--start", default="20230801")
    ap.add_argument("--end", default="20260717")
    ap.add_argument("--universe", default="50", help="50=沪深A股全A")
    ap.add_argument("--combos-file", default="output/gs_filter/coarse_subset36.json")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--priority", default="trailing_first",
                    choices=["trailing_first", "stop_first"],
                    help="trailing_first=移动止盈优先(默认), stop_first=止损优先")
    args = ap.parse_args()

    global OUT_BASE, PRIORITY
    PRIORITY = args.priority
    if PRIORITY != "trailing_first":
        OUT_BASE = f"output/gs_1d_sweep_{PRIORITY}"  # 止损优先输出到新目录, 不覆盖

    if args.formulas:
        formulas = args.formulas.split(",")
    else:
        formulas = json.load(open(args.candidates_file, encoding="utf-8"))

    if args.combos_file and os.path.exists(args.combos_file):
        combos = json.load(open(args.combos_file, encoding="utf-8"))
    else:
        combos = gen_coarse_combos()

    defaults = ConfigLoader.load_defaults()
    bt_cfg = {"initial_capital": 3_000_000.0, "commission": 0.0003,
              "slippage": 0.001, "stamp_tax": 0.0005, "enable_realistic_costs": True,
              "period": "1d",
              "position_sizing": {"min_buy_amount": 2000.0, "max_buy_amount": 20000.0,
                                  "lot_size": 100, "min_lots": 1},
              "use_kline_cache": True}

    print(f"[1d_sweep] 公式{len(formulas)} 全A universe={args.universe} "
          f"{args.start}~{args.end} 组合{len(combos)} period=1d")
    all_rows = []
    for fi, formula in enumerate(formulas):
        print(f"\n=== [{fi+1}/{len(formulas)}] {formula} ===", flush=True)
        rows = run_formula(formula, args.start, args.end, args.universe,
                           combos if not args.limit else combos[:args.limit], bt_cfg)
        all_rows.extend(rows)


if __name__ == "__main__":
    main()
