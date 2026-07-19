"""
因子组合扫描(循环迭代增强的核心)

扫 8 种因子组合(mf/dragon/block on/off)× 阈值,找系统性正 delta 的增强配置。
思路:基线是黑盒,只看增强 delta(基线+增强 vs 基线)。

用法:
    python tools/factor_sweep.py --strategy-yaml config/strategy_QUANTQQ.yaml --tag 20250719_20260718
    python tools/factor_sweep.py --strategy-yaml config/strategy_QUANTQQ.yaml --tag 20250719_20260718 --threshold -2
"""
import sys
import os
import argparse
import itertools
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

from utils.config_loader import ConfigLoader
from backtest.engine import BacktestEngine
from backtest.stop_config import load_stop_config
from factor_score import score_selections


def run_bt(selections, bt_cfg, stop_config, start, end):
    engine = BacktestEngine(bt_cfg)
    result = engine.run(selections=selections, start_time=start, end_time=end, stop_config=stop_config)
    m = result.get("metrics", {})
    return {
        "annret": m.get("annualized_return", 0),
        "sharpe": m.get("sharpe_ratio", 0),
        "maxdd": m.get("max_drawdown", 0),
        "winrate": m.get("win_rate", 0),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy-yaml", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--threshold", type=int, default=-2, help="剔除阈值 total≤此值")
    args = ap.parse_args()

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
    print(f"[INFO] {label} 信号 {len(selections)},扫因子组合 × threshold≤{args.threshold}")

    # 评分一次(全因子,各分列都有了,total 在循环里选择性重组)
    print("[INFO] 评分(全因子)...")
    scored = score_selections(selections, mf, ti, bt)
    keep_cols = [c for c in ["stock_code", "select_date", "formula_name"] if c in scored.columns]

    # 基线
    print("[INFO] 跑基线...")
    base = run_bt(scored[keep_cols].copy(), bt_cfg, stop_config, start, end)
    print(f"  基线: 年化 {base['annret']*100:.2f}% 夏普 {base['sharpe']:.3f} 回撤 {base['maxdd']*100:.2f}%")

    # 8 种因子组合(全关跳过)
    combos = list(itertools.product([True, False], repeat=3))  # (mf, dragon, block)
    results = []
    for use_mf, use_dragon, use_block in combos:
        if not (use_mf or use_dragon or use_block):
            continue
        total = pd.Series(0, index=scored.index, dtype=int)
        if use_mf:
            total = total + scored["mf_score"]
        if use_dragon:
            total = total + scored["dragon_score"]
        if use_block:
            total = total + scored["block_score"]
        filtered = scored[total > args.threshold][keep_cols].copy()
        if len(filtered) == 0:
            continue
        r = run_bt(filtered, bt_cfg, stop_config, start, end)
        name = "+".join([k for k, v in [("mf", use_mf), ("dragon", use_dragon), ("block", use_block)] if v])
        results.append({
            "组合": name,
            "信号": len(filtered),
            "年化%": round(r["annret"] * 100, 2),
            "夏普": round(r["sharpe"], 3),
            "回撤%": round(r["maxdd"] * 100, 2),
            "Δ年化%": round((r["annret"] - base["annret"]) * 100, 2),
            "Δ夏普": round(r["sharpe"] - base["sharpe"], 3),
        })
        print(f"  {name:20s} 信号 {len(filtered):5d} | Δ夏普 {r['sharpe']-base['sharpe']:+.3f} Δ年化 {(r['annret']-base['annret'])*100:+.2f}%")

    print(f"\n=== {label} 因子组合扫描(threshold≤{args.threshold}, tag={args.tag})===")
    print(f"基线: 年化 {base['annret']*100:.2f}% 夏普 {base['sharpe']:.3f} 回撤 {base['maxdd']*100:.2f}%")
    df = pd.DataFrame(results).sort_values("Δ夏普", ascending=False).reset_index(drop=True)
    print(df.to_string(index=False))

    pos = df[df["Δ夏普"] > 0]
    print(f"\n[正 Δ夏普 的组合] {len(pos)}/{len(df)} 个")
    if len(pos) > 0:
        best = df.iloc[0]
        print(f"[最佳] {best['组合']}: Δ夏普 {best['Δ夏普']:+.3f}, Δ年化 {best['Δ年化%']:+.2f}%")

    # 落盘扫描结果
    out = ROOT / "data" / "baseline" / f"factor_sweep_{label}_{args.tag}.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n[OK] 扫描结果落盘 {out}")


if __name__ == "__main__":
    main()
