"""
基线回测(阶段 B)— 落盘 selections + per-trade trades

两种模式:
  1. --formula UPN(默认):用 default.yaml 的 formula_arg + stop_config
  2. --strategy-yaml config/strategy_QUANTQQ.yaml:读 strategy yaml 的 selection + stop_loss(真实策略)

用法:
    python tools/run_upn_baseline.py --start 20250719 --end 20260718
    python tools/run_upn_baseline.py --strategy-yaml config/strategy_QUANTQQ.yaml --start 20250719 --end 20260718

落盘(data/baseline/):
    {label}_selections_<tag>.parquet
    {label}_trades_<tag>.parquet
"""
import sys
import os
import argparse
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
BASELINE_DIR = ROOT / "data" / "baseline"
BASELINE_DIR.mkdir(parents=True, exist_ok=True)

from utils.config_loader import ConfigLoader
from selection.selector import StockSelector
from backtest.engine import BacktestEngine
from backtest.stop_config import load_stop_config


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="开始日期 YYYYMMDD")
    ap.add_argument("--end", required=True, help="结束日期 YYYYMMDD")
    ap.add_argument("--formula", default="UPN", help="公式名(无 --strategy-yaml 时用)")
    ap.add_argument("--strategy-yaml", default=None,
                    help="strategy yaml 路径(读 selection+stop_loss,跑真实策略)")
    args = ap.parse_args()

    if args.strategy_yaml:
        # 模式 2:从 strategy yaml 读 selection + backtest + stop_loss(真实策略)
        strat = ConfigLoader.load_yaml(args.strategy_yaml)
        sel_section = strat.get("selection", {})
        bt_cfg = strat.get("backtest", ConfigLoader.load_defaults().get("backtest", {}))
        sel_cfg = {
            "formula_name": sel_section.get("formula_name", args.formula),
            "formula_arg": str(sel_section.get("formula_arg", "")),
            "universe": sel_section.get("universe", {"type": "50", "exclude_st": True}),
            "period": sel_section.get("period", "1d"),
            "dividend_type": sel_section.get("dividend_type", 1),
        }
        stop_config = load_stop_config(args.strategy_yaml)   # 读 strategy 的 stop_loss
        label = sel_cfg["formula_name"]
        print(f"[INFO] strategy={args.strategy_yaml}")
    else:
        # 模式 1:--formula + default.yaml
        defaults = ConfigLoader.load_defaults()
        bt_cfg = defaults.get("backtest", {})
        sel_tmpl = defaults.get("selection", {})
        sel_cfg = {
            "formula_name": args.formula,
            "formula_arg": str(sel_tmpl.get("formula_arg", "")),
            "universe": sel_tmpl.get("universe", {"type": "50", "exclude_st": True}),
            "period": sel_tmpl.get("period", "1d"),
            "dividend_type": sel_tmpl.get("dividend_type", 1),
        }
        stop_config = load_stop_config()
        label = args.formula

    print(f"[INFO] {label}(arg={sel_cfg['formula_arg']}) {args.start}~{args.end}")

    # 1. 选股
    selector = StockSelector(sel_cfg)
    selections = selector.run(start_time=args.start, end_time=args.end)
    if selections is None or len(selections) == 0:
        print("[FAIL] 无信号")
        sys.exit(1)
    print(f"[INFO] 信号 {len(selections)} 条,列: {list(selections.columns)}")

    # 2. 回测
    engine = BacktestEngine(bt_cfg)
    result = engine.run(
        selections=selections,
        start_time=args.start,
        end_time=args.end,
        stop_config=stop_config,
    )
    if "metrics" not in result or not result["metrics"]:
        print("[FAIL] 无 metrics")
        sys.exit(1)

    m = result["metrics"]
    trades = result.get("trades", [])

    # 3. 落盘 selections + trades
    tag = f"{args.start}_{args.end}"
    selections.to_parquet(BASELINE_DIR / f"{label}_selections_{tag}.parquet")
    if hasattr(trades, "to_parquet"):
        trades.to_parquet(BASELINE_DIR / f"{label}_trades_{tag}.parquet")
        trades_cols = list(trades.columns)
        trades_len = len(trades)
    else:
        pd.DataFrame(trades).to_parquet(BASELINE_DIR / f"{label}_trades_{tag}.parquet", index=False)
        trades_cols = list(pd.DataFrame(trades).columns)
        trades_len = len(trades)
    print(f"[INFO] trades {trades_len} 笔,列: {trades_cols}")

    # 4. 聚合 metrics(含 Calmar/回撤 — CLAUDE.md 铁律4)
    print(f"\n=== 基线 {label} {tag} ===")
    print(f"信号数: {len(selections)}")
    print(f"交易数: {trades_len}")
    print(f"累积收益: {m.get('cumulative_return', 0):.4f}")
    print(f"年化:     {m.get('annualized_return', 0):.4f}")
    print(f"最大回撤: {m.get('max_drawdown', 0):.4f}")
    print(f"夏普:     {m.get('sharpe_ratio', 0):.4f}")
    print(f"胜率:     {m.get('win_rate', 0):.4f}")
    calmar = m.get("calmar_ratio")
    print(f"Calmar:   {calmar:.4f}" if calmar else "[Calmar 未提供]")
    print(f"\n[OK] 落盘到 {BASELINE_DIR}")


if __name__ == "__main__":
    main()
