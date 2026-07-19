"""
阈值扫描:不同 total≤threshold 的避雷效果对比,找最佳点

评分一次,对多个 threshold 各跑回测,对比年化/夏普/回撤,找最佳避雷阈值。

用法:
    python tools/threshold_sweep.py --strategy-yaml config/strategy_QUANTQQ.yaml --tag 20250719_20260718
    python tools/threshold_sweep.py --strategy-yaml config/strategy_QUANTQQ.yaml --tag 20250719_20260718 --thresholds -3,-2,-1,0,1
"""
import sys
import os
import argparse
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent

from utils.config_loader import ConfigLoader, resolve_strategy_yaml
from backtest.engine import BacktestEngine
from backtest.stop_config import load_stop_config
from factor_score import score_selections


def run_bt(selections, bt_cfg, stop_config, start, end):
    engine = BacktestEngine(bt_cfg)
    result = engine.run(selections=selections, start_time=start, end_time=end, stop_config=stop_config)
    m = result.get("metrics", {})
    trades = result.get("trades", [])
    return {
        "signals": len(selections),
        "trades": len(trades) if hasattr(trades, "__len__") else 0,
        "annret": m.get("annualized_return", 0),
        "maxdd": m.get("max_drawdown", 0),
        "sharpe": m.get("sharpe_ratio", 0),
        "winrate": m.get("win_rate", 0),
        "calmar": m.get("calmar_ratio"),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy-yaml", default=None,
                    help="策略 yaml; 缺省自动解析: current.yaml(前端保存) > strategy_QUANTQQ.yaml")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--thresholds", default="-3,-2,-1,0", help="逗号分隔的阈值")
    args = ap.parse_args()
    args.strategy_yaml = resolve_strategy_yaml(args.strategy_yaml)

    strat = ConfigLoader.load_yaml(args.strategy_yaml)
    sel_section = strat["selection"]
    bt_cfg = strat.get("backtest", ConfigLoader.load_defaults().get("backtest", {}))
    stop_config = load_stop_config(args.strategy_yaml)
    label = sel_section["formula_name"]
    start, end = args.tag.split("_")

    selections = pd.read_parquet(ROOT / "data" / "baseline" / f"{label}_selections_{args.tag}.parquet")
    mf = pd.read_parquet(ROOT / "data" / "factors" / f"moneyflow_{args.tag}.parquet")
    ti = pd.read_parquet(ROOT / "data" / "factors" / f"top_inst_{args.tag}.parquet")
    bt = pd.read_parquet(ROOT / "data" / "factors" / f"block_trade_{args.tag}.parquet")
    print(f"[INFO] {label} 基线信号 {len(selections)} 条")

    # 评分一次(所有 threshold 复用)
    print("[INFO] 算三因子评分(一次)...")
    scored = score_selections(selections, mf, ti, bt)
    print(f"  total_score 分布: {dict(scored['total_score'].value_counts().sort_index())}")

    keep_cols = [c for c in ["stock_code", "select_date", "formula_name"] if c in scored.columns]
    thresholds = [int(t) for t in args.thresholds.split(",")]

    # 基线 + 各 threshold
    print(f"\n[INFO] 跑基线 + {len(thresholds)} 个阈值...")
    results = []

    base = run_bt(scored[keep_cols].copy(), bt_cfg, stop_config, start, end)
    base.update({"threshold": "基线(全信号)", "removed": 0})
    results.append(base)
    print(f"  基线 done: 年化 {base['annret']*100:.2f}% 夏普 {base['sharpe']:.3f}")

    for th in thresholds:
        filtered = scored[scored["total_score"] > th][keep_cols].copy()
        if len(filtered) == 0:
            print(f"  threshold≤{th}: 剔除后无信号,跳过")
            continue
        r = run_bt(filtered, bt_cfg, stop_config, start, end)
        r.update({"threshold": f"剔除 total≤{th}", "removed": len(selections) - len(filtered)})
        results.append(r)
        print(f"  ≤{th} done: 信号 {len(filtered)} 年化 {r['annret']*100:.2f}% 夏普 {r['sharpe']:.3f}")

    # 对比表
    df = pd.DataFrame(results)
    df["年化%"] = (df["annret"] * 100).round(2)
    df["回撤%"] = (df["maxdd"] * 100).round(2)
    df["夏普"] = df["sharpe"].round(3)
    df["胜率%"] = (df["winrate"] * 100).round(1)
    show = df[["threshold", "signals", "removed", "年化%", "回撤%", "夏普", "胜率%"]]
    show.columns = ["方案", "信号", "剔除数", "年化%", "回撤%", "夏普", "胜率%"]
    print(f"\n=== {label} 阈值扫描(tag={args.tag})===")
    print(show.to_string(index=False))

    best_sharpe = df.loc[df["sharpe"].idxmax()]
    best_ann = df.loc[df["annret"].idxmax()]
    print(f"\n[最佳夏普] {best_sharpe['threshold']}: 夏普 {best_sharpe['sharpe']:.3f}, 年化 {best_sharpe['annret']*100:.2f}%")
    print(f"[最高年化] {best_ann['threshold']}: 年化 {best_ann['annret']*100:.2f}%, 夏普 {best_ann['sharpe']:.3f}")


if __name__ == "__main__":
    main()
