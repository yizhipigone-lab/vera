# -*- coding: utf-8 -*-
"""随机入场对照实验 (2026-07-25) — 分解 公式择股 vs 止盈止损机制 的贡献。

设计 (van Tharp 随机入场思想的严格版):
  对照A (随机股票/保时机): 每日信号数与真实公式逐日对齐, 只随机股票身份。
    → 隔离"择股"贡献, 保留公式的暴露/时机模式。
  对照B (完全随机): 股票+日期都随机, 只保信号总数。
    → 含时机运气的天真基线。
  各 N 次, 看真实收益在随机分布中的分位/z 值。

判定 (事先约定): 真实收益 < 对照A 均值+2σ → 择股无显著 alpha。

用法: python tools/random_entry_control.py --formulas 超赢王牛股,GUPIAO_009,GP1014 --runs 30
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent

from backtest.engine import BacktestEngine  # noqa: E402
from backtest.stop_config import load_stop_config  # noqa: E402
from core.connector import TdxConnector  # noqa: E402
from core.data_fetcher import DataFetcher  # noqa: E402
from selection.selector import StockSelector  # noqa: E402
from selection.signal_rules import filter_first_signal_in_window  # noqa: E402
from utils.config_loader import ConfigLoader  # noqa: E402

PAD_DAYS = 70


def run_engine(sel, start, end, bt_cfg, stop_config):
    engine = BacktestEngine(bt_cfg)
    res = engine.run(selections=sel, start_time=start, end_time=end,
                     stop_config=stop_config)
    m = res.get("metrics") or {}
    trades = res.get("trades")
    n_trades = int(len(trades)) if trades is not None else 0
    return {"cumulative_return": m.get("cumulative_return"),
            "win_rate": m.get("win_rate"),
            "max_drawdown": m.get("max_drawdown"),
            "sharpe": m.get("sharpe_ratio"),
            "n_trades": n_trades}


def random_same_days(sel: pd.DataFrame, stocks: list, rng) -> pd.DataFrame:
    """对照A: 每日信号数不变, 股票换随机 (日内不放回, 跨日独立)。"""
    rows = []
    codes = np.array(stocks)
    for d, g in sel.groupby("select_date"):
        k = len(g)
        pick = rng.choice(codes, size=k, replace=False) if k <= len(codes) \
            else rng.choice(codes, size=k, replace=True)
        rows.append(pd.DataFrame({"stock_code": pick, "select_date": d,
                                  "formula_name": "RAND_A"}))
    return pd.concat(rows, ignore_index=True) if rows else sel.iloc[:0]


def random_full(sel: pd.DataFrame, stocks: list, dates: list, rng) -> pd.DataFrame:
    """对照B: 股票+日期全随机, 只保信号总数。"""
    n = len(sel)
    pick_s = rng.choice(np.array(stocks), size=n, replace=True)
    pick_d = rng.choice(np.array(dates), size=n, replace=True)
    return pd.DataFrame({"stock_code": pick_s, "select_date": pick_d,
                         "formula_name": "RAND_B"})


def summarize(vals: list) -> dict:
    a = np.array([v for v in vals if v is not None], dtype=float)
    return {"mean": float(a.mean()), "std": float(a.std()),
            "p05": float(np.percentile(a, 5)),
            "p50": float(np.percentile(a, 50)),
            "p95": float(np.percentile(a, 95)), "n": int(len(a))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--formulas", required=True)
    ap.add_argument("--runs", type=int, default=30)
    ap.add_argument("--seed", type=int, default=20260725)
    ap.add_argument("--start", default="20260101")
    ap.add_argument("--end", default=pd.Timestamp.now().strftime("%Y%m%d"))
    ap.add_argument("--universe-type", default="5")
    ap.add_argument("--max-buy-amount", type=float, default=10000.0)
    ap.add_argument("--cooldown-days", type=int, default=20)
    ap.add_argument("--first-signal-window", type=int, default=30)
    ap.add_argument("--out", default=str(ROOT / "output" / "formula_rank"
                                          / "random_control.json"))
    args = ap.parse_args()

    TdxConnector.initialize()
    stocks = StockSelector({"formula_name": "_", "universe": {
        "type": args.universe_type, "exclude_st": True}}).resolve_universe()
    print(f"[INFO] 股票池 {len(stocks)} 只, 每公式 {args.runs} 次×2 对照")

    padded_start = (pd.Timestamp(args.start) - pd.Timedelta(days=PAD_DAYS)
                    ).strftime("%Y%m%d")
    calendar = pd.DatetimeIndex(pd.to_datetime(DataFetcher.get_calendar_days(
        "SH", start_time=padded_start, end_time=args.end)))
    trade_dates = [d for d in calendar
                   if pd.Timestamp(args.start) <= d <= pd.Timestamp(args.end)]

    defaults = ConfigLoader.load_defaults()
    bt_cfg = dict(defaults.get("backtest", {}))
    ps = dict(bt_cfg.get("position_sizing", {}))
    ps["max_buy_amount"] = float(args.max_buy_amount)
    bt_cfg["position_sizing"] = ps
    bt_cfg["matrix_cache"] = False
    bt_cfg["sell_cooldown_days"] = int(args.cooldown_days)
    stop_config = load_stop_config()

    report = {}
    for f in [s.strip() for s in args.formulas.split(",") if s.strip()]:
        print(f"[INFO] {f} 选股中...")
        cfg = {"formula_name": f, "formula_arg": "",
               "universe": {"type": args.universe_type, "exclude_st": True},
               "period": "1d", "dividend_type": 1}
        raw = StockSelector(cfg).run(start_time=padded_start,
                                     end_time=args.end, stock_list=stocks)
        sel = filter_first_signal_in_window(
            raw, window_td=args.first_signal_window,
            real_start=args.start, calendar=calendar)
        sel["select_date"] = pd.to_datetime(sel["select_date"])
        print(f"[INFO] {f}: 信号 {len(sel)} 条")

        t0 = time.time()
        real = run_engine(sel, args.start, args.end, bt_cfg, stop_config)
        print(f"  真实: {real['cumulative_return']*100:+.2f}% "
              f"({time.time()-t0:.0f}s)")

        rng = np.random.default_rng(args.seed)
        ret_a, ret_b = [], []
        wr_a, wr_b = [], []
        for i in range(args.runs):
            ra = random_same_days(sel, stocks, rng)
            ma = run_engine(ra, args.start, args.end, bt_cfg, stop_config)
            ret_a.append(ma["cumulative_return"]); wr_a.append(ma["win_rate"])
            rb = random_full(sel, stocks, trade_dates, rng)
            mb = run_engine(rb, args.start, args.end, bt_cfg, stop_config)
            ret_b.append(mb["cumulative_return"]); wr_b.append(mb["win_rate"])
            print(f"  [{i+1}/{args.runs}] A={ma['cumulative_return']*100:+.2f}% "
                  f"B={mb['cumulative_return']*100:+.2f}%")

        sa, sb = summarize(ret_a), summarize(ret_b)
        za = (real["cumulative_return"] - sa["mean"]) / sa["std"] if sa["std"] > 0 else float("nan")
        pct_a = float(np.mean([v <= real["cumulative_return"] for v in ret_a]))
        report[f] = {"real": real, "signals": int(len(sel)),
                     "control_A": {**sa, "win_rate": summarize(wr_a)},
                     "control_B": {**sb, "win_rate": summarize(wr_b)},
                     "z_vs_A": za, "pct_vs_A": pct_a}
        print(f"  == {f}: 真实={real['cumulative_return']*100:+.2f}% | "
              f"A均值={sa['mean']*100:+.2f}%±{sa['std']*100:.2f} "
              f"(z={za:.2f}, 分位={pct_a*100:.0f}%) | "
              f"B均值={sb['mean']*100:+.2f}%±{sb['std']*100:.2f}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    TdxConnector.close()
    print(f"[OK] 落盘 {args.out}")


if __name__ == "__main__":
    main()
