"""
组合验证:剔除低分(total≤阈值)后的组合 vs 基线组合

读基线 selections → 评分 → 剔除低分 → 重跑回测(同 stop_config)→ 对比基线。
测试"避雷"用法的实战价值(单笔差异小,组合层面是否放大)。

用法:
    python tools/combo_filter_test.py --strategy-yaml config/strategy_QUANTQQ.yaml --tag 20250719_20260718
    python tools/combo_filter_test.py --strategy-yaml config/strategy_QUANTQQ.yaml --tag 20250719_20260718 --threshold -1
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


def run_backtest(selections, bt_cfg, stop_config, start, end, label):
    """跑一版回测,返回 metrics dict"""
    engine = BacktestEngine(bt_cfg)
    result = engine.run(selections=selections, start_time=start, end_time=end, stop_config=stop_config)
    m = result.get("metrics", {})
    trades = result.get("trades", [])
    return {
        "label": label,
        "signals": len(selections),
        "trades": len(trades) if hasattr(trades, "__len__") else 0,
        "cumret": m.get("cumulative_return", 0),
        "annret": m.get("annualized_return", 0),
        "maxdd": m.get("max_drawdown", 0),
        "sharpe": m.get("sharpe_ratio", 0),
        "winrate": m.get("win_rate", 0),
        "calmar": m.get("calmar_ratio"),
    }


def print_metrics(r):
    print(f"\n--- {r['label']} ---")
    print(f"  信号 {r['signals']} | 交易 {r['trades']}")
    print(f"  累积 {r['cumret']:+.4f} | 年化 {r['annret']:+.4f} | 回撤 {r['maxdd']:.4f}")
    print(f"  夏普 {r['sharpe']:.4f} | 胜率 {r['winrate']:.4f}", end="")
    print(f" | Calmar {r['calmar']:.4f}" if r["calmar"] else "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy-yaml", default=None,
                    help="策略 yaml; 缺省自动解析: current.yaml(前端保存) > strategy_QUANTQQ.yaml")
    ap.add_argument("--tag", required=True, help="区间标签 20250719_20260718")
    ap.add_argument("--threshold", type=int, default=-2, help="剔除阈值 total≤此值")
    args = ap.parse_args()
    args.strategy_yaml = resolve_strategy_yaml(args.strategy_yaml)
    print(f"[INFO] 策略配置: {args.strategy_yaml}")

    strat = ConfigLoader.load_yaml(args.strategy_yaml)
    sel_section = strat["selection"]
    bt_cfg = strat.get("backtest", ConfigLoader.load_defaults().get("backtest", {}))
    stop_config = load_stop_config(args.strategy_yaml)
    label = sel_section["formula_name"]
    start, end = args.tag.split("_")

    # 1. 读基线 selections + 因子数据
    sel_path = ROOT / "data" / "baseline" / f"{label}_selections_{args.tag}.parquet"
    selections = pd.read_parquet(sel_path)
    mf = pd.read_parquet(ROOT / "data" / "factors" / f"moneyflow_{args.tag}.parquet")
    ti = pd.read_parquet(ROOT / "data" / "factors" / f"top_inst_{args.tag}.parquet")
    bt = pd.read_parquet(ROOT / "data" / "factors" / f"block_trade_{args.tag}.parquet")
    print(f"[INFO] {label} 基线信号 {len(selections)} 条")

    # 2. 评分
    print("[INFO] 算三因子评分...")
    scored = score_selections(selections, mf, ti, bt)
    low_dist = scored["total_score"].value_counts().sort_index()
    print(f"  total_score 分布: {dict(low_dist)}")

    # 3. 剔除低分
    keep_cols = [c for c in ["stock_code", "select_date", "formula_name"] if c in scored.columns]
    filtered = scored[scored["total_score"] > args.threshold][keep_cols].copy()
    removed = len(selections) - len(filtered)
    print(f"[INFO] 剔除 total≤{args.threshold}: {removed} 条 → 剩 {len(filtered)} 条")

    # 4. 跑两版回测对比
    print("\n[INFO] 跑回测对比(同 stop_config)...")
    base = run_backtest(selections[keep_cols].copy(), bt_cfg, stop_config, start, end, f"基线(全信号 {len(selections)})")
    filt = run_backtest(filtered, bt_cfg, stop_config, start, end, f"剔除低分(剩 {len(filtered)})")

    print_metrics(base)
    print_metrics(filt)

    # 5. 差异
    print(f"\n=== 差异(剔除后 - 基线)===")
    print(f"  年化 {filt['annret']-base['annret']:+.4f} | 回撤 {filt['maxdd']-base['maxdd']:+.4f} | 夏普 {filt['sharpe']-base['sharpe']:+.4f}")
    print(f"  胜率 {filt['winrate']-base['winrate']:+.4f}")


if __name__ == "__main__":
    main()
