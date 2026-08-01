# -*- coding: utf-8 -*-
"""未来函数甄别 (2026-07-25): 信号日移位测试。

原理: 未来函数 (ZIG/事后重画类) 的信号是走势确认后"贴"回历史低点的,
入场延后 1~2 个交易日, 超额必然崩塌; 正常公式只受 modest 衰减。

流程: 与 batch_formula_rank 完全同参 (全A/剔ST/单票1万/冷却20td/首信号30td),
T+0 基线必须复现批次数字 (装置自校验), 然后 T+1/T+2 移位重跑。

用法: python tools/future_func_check.py --formulas 超赢王牛股,财富金生！,绝底,GUPIAO_009
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

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


def shift_dates(selections: pd.DataFrame, calendar: pd.DatetimeIndex,
                n_td: int) -> pd.DataFrame:
    """select_date 后移 n_td 个交易日; 移出区间的丢弃。"""
    if n_td == 0:
        return selections
    cal = calendar
    idx = cal.searchsorted(pd.to_datetime(selections["select_date"]).values) + n_td
    keep = idx < len(cal)
    out = selections[keep].copy()
    out["select_date"] = cal[idx[keep]]
    return out


def run_shift(formula: str, base_sel: pd.DataFrame, calendar, shifts,
              start, end, bt_cfg, stop_config) -> list:
    rows = []
    for n in shifts:
        sel = shift_dates(base_sel, calendar, n)
        if len(sel) == 0:
            rows.append({"shift": n, "status": "empty"})
            continue
        t0 = time.time()
        engine = BacktestEngine(bt_cfg)
        res = engine.run(selections=sel, start_time=start, end_time=end,
                         stop_config=stop_config)
        m = res.get("metrics") or {}
        trades = res.get("trades")
        rows.append({
            "shift": n, "status": "ok",
            "signals": int(len(sel)),
            "n_trades": int(len(trades)) if trades is not None else 0,
            "cumulative_return": m.get("cumulative_return"),
            "win_rate": m.get("win_rate"),
            "max_drawdown": m.get("max_drawdown"),
            "sharpe": m.get("sharpe_ratio"),
            "elapsed_s": round(time.time() - t0, 1),
        })
        print(f"  T+{n}: 收益={m.get('cumulative_return', 0)*100:+.2f}% "
              f"胜率={m.get('win_rate', 0)*100:.1f}% "
              f"交易={rows[-1]['n_trades']} ({rows[-1]['elapsed_s']:.0f}s)")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--formulas", required=True)
    ap.add_argument("--start", default="20260101")
    ap.add_argument("--end", default=pd.Timestamp.now().strftime("%Y%m%d"))
    ap.add_argument("--universe-type", default="5")
    ap.add_argument("--max-buy-amount", type=float, default=10000.0)
    ap.add_argument("--cooldown-days", type=int, default=20)
    ap.add_argument("--first-signal-window", type=int, default=30)
    ap.add_argument("--shifts", default="0,1,2")
    ap.add_argument("--out", default=str(ROOT / "output" / "formula_rank"
                                          / "future_check.json"))
    args = ap.parse_args()

    formulas = [s.strip() for s in args.formulas.split(",") if s.strip()]
    shifts = [int(s) for s in args.shifts.split(",")]
    TdxConnector.initialize()

    uni = StockSelector({"formula_name": "_", "universe": {
        "type": args.universe_type, "exclude_st": True}})
    stocks = uni.resolve_universe()
    print(f"[INFO] 股票池 {len(stocks)} 只")

    padded_start = (pd.Timestamp(args.start) - pd.Timedelta(days=PAD_DAYS)
                    ).strftime("%Y%m%d")
    calendar = pd.DatetimeIndex(pd.to_datetime(
        DataFetcher.get_trading_dates("SH", start_time=padded_start,
                                      end_time=args.end)))

    defaults = ConfigLoader.load_defaults()
    bt_cfg = dict(defaults.get("backtest", {}))
    ps = dict(bt_cfg.get("position_sizing", {}))
    ps["max_buy_amount"] = float(args.max_buy_amount)
    bt_cfg["position_sizing"] = ps
    bt_cfg["matrix_cache"] = False
    bt_cfg["sell_cooldown_days"] = int(args.cooldown_days)
    stop_config = load_stop_config()

    report = {}
    for f in formulas:
        print(f"[INFO] {f} 选股中...")
        sel_cfg = {"formula_name": f, "formula_arg": "",
                   "universe": {"type": args.universe_type, "exclude_st": True},
                   "period": "1d", "dividend_type": 1}
        raw = StockSelector(sel_cfg).run(start_time=padded_start,
                                         end_time=args.end, stock_list=stocks)
        base = filter_first_signal_in_window(
            raw, window_td=args.first_signal_window,
            real_start=args.start, calendar=calendar)
        print(f"[INFO] {f}: 信号 {len(raw) if raw is not None else 0} → "
              f"{len(base) if base is not None else 0} (30日规则后)")
        if base is None or len(base) == 0:
            report[f] = {"status": "no_signals"}
            continue
        report[f] = {"status": "ok", "rows": run_shift(
            f, base, calendar, shifts, args.start, args.end, bt_cfg, stop_config)}

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    TdxConnector.close()
    print(f"[OK] 落盘 {args.out}")

    # 判定摘要
    print("\n=== 判定 (T+1 保留率 = T+1收益 / T+0收益) ===")
    for f, rep in report.items():
        if rep.get("status") != "ok":
            continue
        rows = {r["shift"]: r for r in rep["rows"] if r["status"] == "ok"}
        r0, r1 = rows.get(0), rows.get(1)
        if not r0 or not r1:
            continue
        ret0, ret1 = r0["cumulative_return"], r1["cumulative_return"]
        keep = (ret1 / ret0) if ret0 else float("nan")
        verdict = "疑似未来函数" if (ret0 > 0.10 and keep < 0.3) else \
                  ("显著衰减" if keep < 0.6 else "衰减正常")
        print(f"{f}: T+0={ret0*100:+.1f}% T+1={ret1*100:+.1f}% "
              f"保留率={keep*100:.0f}% → {verdict}")


if __name__ == "__main__":
    main()
