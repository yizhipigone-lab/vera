"""
三因子评分与 UPN 收益相关性分析(阶段 C)

读基线 selections + trades + 因子数据,给每笔信号算评分,
分析评分-收益相关性(分组对比 + Spearman + t 检验),输出决策依据。

信号→交易匹配(审计 H2):(stock_code, entry_date ≤ select_date+N)取首笔,无匹配计 NaN
显著性(审计 M4):p<0.05 且 |均值差|≥3% 双条件

用法:
    python tools/factor_correlation_analysis.py --tag 20250719_20260718
"""
import sys
import os
import argparse
from pathlib import Path

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # 项目根
sys.path.insert(0, str(Path(__file__).resolve().parent))                          # tools/(for factor_score)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from factor_score import score_selections

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, help="区间标签,如 20250719_20260718")
    ap.add_argument("--formula", default="UPN")
    ap.add_argument("--match-window", type=int, default=3, help="信号-交易匹配窗口(T+N 天)")
    args = ap.parse_args()

    base = ROOT / "data"
    sel_path = base / "baseline" / f"{args.formula}_selections_{args.tag}.parquet"
    tr_path = base / "baseline" / f"{args.formula}_trades_{args.tag}.parquet"
    mf_path = base / "factors" / f"moneyflow_{args.tag}.parquet"
    ti_path = base / "factors" / f"top_inst_{args.tag}.parquet"
    bt_path = base / "factors" / f"block_trade_{args.tag}.parquet"

    print("[INFO] 读数据...")
    selections = pd.read_parquet(sel_path)
    trades = pd.read_parquet(tr_path)
    mf = pd.read_parquet(mf_path)
    ti = pd.read_parquet(ti_path)
    bt = pd.read_parquet(bt_path)
    print(f"  selections {len(selections)} | trades {len(trades)} | "
          f"moneyflow {len(mf)} | top_inst {len(ti)} | block_trade {len(bt)}")

    # 探测 trades 列名(容错)
    code_col = next((c for c in trades.columns if c in ("stock_code", "ts_code")), None)
    entry_col = next((c for c in trades.columns
                      if "entry" in c.lower() or c.lower() in ("buy_date", "open_date")), None)
    profit_col = next((c for c in trades.columns
                       if "profit" in c.lower() or "return" in c.lower() or "pct" in c.lower()), None)
    print(f"  trades 列: code={code_col} entry={entry_col} profit={profit_col}")
    if not all([code_col, entry_col, profit_col]):
        print(f"[FAIL] trades 列探测不全,实际列: {list(trades.columns)}")
        sys.exit(1)

    # 1. 信号→交易匹配(预分组优化,避免 11.7万×3561 笛卡尔积)
    print(f"\n[INFO] 信号→交易匹配(窗口 T+{args.match_window}天)...")
    sel = selections.copy()
    sel["select_date"] = pd.to_datetime(sel["select_date"], errors="coerce")
    trades["_entry_dt"] = pd.to_datetime(trades[entry_col], errors="coerce")

    from collections import defaultdict
    trades_by_code = defaultdict(list)
    for _, t in trades.iterrows():
        trades_by_code[t[code_col]].append((t["_entry_dt"], t[profit_col]))

    matched = []
    for _, row in sel.iterrows():
        code = row["stock_code"]
        sd = row["select_date"]
        we = sd + pd.Timedelta(days=args.match_window)
        hits = [(d, p) for d, p in trades_by_code.get(code, []) if sd <= d <= we]
        if hits:
            hits.sort(key=lambda x: x[0])  # 取首笔(entry 最早)
            matched.append(hits[0][1])
        else:
            matched.append(np.nan)
    sel["profit"] = matched
    fill_rate = sel["profit"].notna().mean()
    print(f"  匹配率: {fill_rate * 100:.1f}% ({sel['profit'].notna().sum()}/{len(sel)})")

    # 2. 先取匹配上的子集(避免对 11.7万信号全评分)
    valid = sel.dropna(subset=["profit"]).copy()
    print(f"  有效样本(匹配上): {len(valid)}")

    # 3. 评分(per-row select_date,只对有效样本)
    print("[INFO] 算三因子评分(T-1 数据)...")
    valid = score_selections(valid, mf, ti, bt)

    if len(valid) < 30:
        print("[WARN] 有效样本 <30,结果不具统计意义")

    # 3. 分组对比
    print("\n=== 分组对比 ===")
    def grp(x):
        return "高(≥2)" if x >= 2 else ("低(≤-2)" if x <= -2 else "中(-1~1)")
    valid["组"] = valid["total_score"].apply(grp)
    summary = valid.groupby("组").agg(
        样本数=("profit", "count"),
        平均收益=("profit", "mean"),
        胜率=("profit", lambda x: (x > 0).mean()),
    ).reindex(["高(≥2)", "中(-1~1)", "低(≤-2)"])
    print(summary.to_string())

    # 4. 相关性
    print("\n=== 相关性 ===")
    spearman = valid["total_score"].corr(valid["profit"], method="spearman")
    pearson = valid["total_score"].corr(valid["profit"], method="pearson")
    print(f"  Spearman: {spearman:+.4f}  |  Pearson: {pearson:+.4f}")

    # 5. t 检验(高 vs 低)
    print("\n=== t 检验(高分组 vs 低分组)===")
    high = valid[valid["组"] == "高(≥2)"]["profit"]
    low = valid[valid["组"] == "低(≤-2)"]["profit"]
    if len(high) >= 2 and len(low) >= 2:
        mean_diff = high.mean() - low.mean()
        print(f"  高分组均值 {high.mean():+.4f} vs 低分组 {low.mean():+.4f},差 {mean_diff:+.4f}")
        try:
            from scipy import stats
            t, p = stats.ttest_ind(high, low, equal_var=False)
            sig = (p < 0.05) and (abs(mean_diff) >= 0.03)
            print(f"  t={t:.3f}, p={p:.4f}")
            print(f"  显著有效(p<0.05 且 |均值差|≥3%): {'✅ 是' if sig else '❌ 否'}")
        except ImportError:
            print(f"  [scipy 未装,均值差 {mean_diff:+.4f},需 |差|≥3% + 装 scipy 做 p 值]")
    else:
        print(f"  样本不足(高 {len(high)} / 低 {len(low)}),无法 t 检验")
        print("  → 建议:放宽分组阈值或延长区间")

    # 落盘(有效样本 评分+收益,供后续细查)
    out = base / "baseline" / f"correlation_{args.tag}.parquet"
    valid.to_parquet(out)
    print(f"\n[OK] 评分+收益明细落盘 {out}({len(valid)} 行)")


if __name__ == "__main__":
    main()
