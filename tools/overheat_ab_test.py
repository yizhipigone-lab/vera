# -*- coding: utf-8 -*-
"""过热剔除 A/B 终审 — IC 筛出的强因子,在真实回测里验证"剔除最热信号"是否增收/控回撤。

背景(2026-07-19): factor_ic_screen 两窗口确认"过热反转"为唯一强族
(dist_ma20/turnover_rate/mom5/intraday20 等 14 因子互证, IC≈-0.10~-0.18)。
本实验是终审: IC 好≠组合赚钱, 同历史跑两遍, 只换剔除规则。

预注册设计(评审后可调, 跑数后冻结):
    臂: base / turnover_rate top10%剔除 / top20%剔除 / dist_ma20 top10%剔除 / top20%剔除
        / circ_mv 最小20%剔除(大市值优先臂, 验证弱正方向)
    排名口径: 每个 select_date 内横截面 rank(pct) — 与 IC 方法论一致
    因子口径: turnover_rate/circ_mv = T 日 daily_basic 快照(T 收盘已知); dist_ma20 trailing
    缺失值: 保留(不构成"过热"), 打印各臂剔除条数
    判定(vs base): Calmar 不差于 0.1 且 年化提升 → PASS(增收)
                   Calmar 不差于 0.1 且 回撤改善≥2pp 且 年化损失≤1pp → PASS(控回撤)

用法:
    python tools/overheat_ab_test.py --tag 20250719_20260718
    python tools/overheat_ab_test.py --tag 20230719_20260718
"""
import sys
import os
import argparse
import time
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
from backtest.stop_config import load_stop_config
from combo_filter_test import run_backtest
from factor_ic_screen import load_panels, f_dist_ma20, lookup

# 实验臂: (名称, 因子, 剔除规则) — top10/top20 = 剔除日截面最热 X%; bottom20 = 剔除最小 20%
ARMS = [
    ("base", None, None),
    ("drop_turnover_top10", "turnover_rate", "top10"),
    ("drop_turnover_top20", "turnover_rate", "top20"),
    ("drop_distma_top10", "dist_ma20", "top10"),
    ("drop_distma_top20", "dist_ma20", "top20"),
    ("keep_bigcap80", "circ_mv", "bottom20"),
]

KEEP_COLS = ["stock_code", "select_date", "formula_name"]


def parse_arms(s: str) -> list:
    """解析 --arms "turnover_rate:top10,dist_ma20:top20,circ_mv:bottom20" → (名称, 因子, 规则)。
    规则: top10/top20 = 剔日截面最高 10%/20%(负 IC 因子用); bottom10/bottom20 = 剔最低(正 IC 因子用)。
    组合臂(2026-07-20 用户拍板): "f1:top10+f2:top10" → factor=None, rule="f1:top10+f2:top10",
    按生产 selection/factor_filter.apply_rules 同款顺序语义执行。"""
    arms = [("base", None, None)]
    for item in s.split(","):
        item = item.strip()
        if "+" in item:
            parts = item.split("+")
            for p in parts:
                _f, r = p.strip().split(":")
                if r not in ("top10", "top20", "bottom10", "bottom20"):
                    raise ValueError(f"未知规则: {r}")
            arms.append(("combo_" + item.replace(":", "_").replace("+", "+"), None, item))
            continue
        factor, rule = item.split(":")
        if rule not in ("top10", "top20", "bottom10", "bottom20"):
            raise ValueError(f"未知规则: {rule}(支持 top10/top20/bottom10/bottom20)")
        arms.append((f"{factor}_{rule}", factor, rule))
    return arms


def apply_filter(sel: pd.DataFrame, factor: str | None, rule: str | None) -> pd.DataFrame:
    """按日截面 rank 过滤。缺失因子值的行保留(不构成过热/小市值证据)。
    factor=None 且 rule 含 '+' 时为组合臂: 走生产同款 selection/factor_filter.apply_rules。"""
    if rule and "+" in rule:
        from selection.factor_filter import apply_rules
        return apply_rules(sel, [p.strip() for p in rule.split("+")])[KEEP_COLS].copy()
    if factor is None:
        return sel[KEEP_COLS].copy()
    rank = sel.groupby("select_date")[factor].rank(pct=True)
    if rule == "top10":
        keep = rank.isna() | (rank <= 0.90)   # 剔 rank>0.9 = 最热 10%
    elif rule == "top20":
        keep = rank.isna() | (rank <= 0.80)   # 剔 rank>0.8 = 最热 20%
    elif rule == "bottom10":
        keep = rank.isna() | (rank > 0.10)    # 剔 rank≤0.1 = 最低 10%
    elif rule == "bottom20":
        keep = rank.isna() | (rank > 0.20)    # 剔 rank≤0.2 = 最低 20%
    else:
        raise ValueError(rule)
    return sel[keep][KEEP_COLS].copy()


def verdict(r: dict, base: dict) -> str:
    """预注册判定(vs base)。"""
    if base["calmar"] is None or r["calmar"] is None:
        return "N/A"
    calmar_ok = r["calmar"] >= base["calmar"] - 0.1
    ann_gain = r["annret"] - base["annret"]
    dd_gain = abs(base["maxdd"]) - abs(r["maxdd"])
    if calmar_ok and ann_gain > 0:
        return "PASS(增收)"
    if calmar_ok and dd_gain >= 0.02 and ann_gain >= -0.01:
        return "PASS(控回撤)"
    return "FAIL"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--formula", default="QUANTQQ")
    ap.add_argument("--strategy-yaml", default=None,
                    help="策略 yaml; 缺省自动解析: current.yaml(前端保存) > strategy_QUANTQQ.yaml")
    ap.add_argument("--arms", default=None,
                    help='自定义臂 "因子:规则,..." (规则 top10/top20/bottom10/bottom20); 提供后从因子矩阵读因子值')
    ap.add_argument("--factor-matrix", default=None,
                    help="因子矩阵 parquet(默认 output/gs_filter/factor_matrix_{formula}_{tag}.parquet)")
    args = ap.parse_args()
    args.strategy_yaml = resolve_strategy_yaml(args.strategy_yaml)
    start, end = args.tag.split("_")

    strat = ConfigLoader.load_yaml(args.strategy_yaml)
    bt_cfg = strat.get("backtest", ConfigLoader.load_defaults().get("backtest", {}))
    stop_config = load_stop_config(args.strategy_yaml)

    if args.arms:
        # 矩阵模式(formula_lab S4 路径): 因子值已在 S2 算好, 单一来源
        mpath = Path(args.factor_matrix) if args.factor_matrix else (
            ROOT / "output" / "gs_filter" / f"factor_matrix_{args.formula}_{args.tag}.parquet")
        sel = pd.read_parquet(mpath)
        sel["select_date"] = pd.to_datetime(sel["select_date"])
        arms = parse_arms(args.arms)
        # 缺列检查: 单臂因子 + 组合臂展开的全部因子
        need = []
        for _n, f, r in arms:
            if f:
                need.append(f)
            elif r and "+" in r:
                need += [p.strip().split(":")[0] for p in r.split("+")]
        missing = [f for f in dict.fromkeys(need) if f not in sel.columns]
        if missing:
            raise SystemExit(f"[FAIL] 因子矩阵缺列: {missing}(先跑 factor_ic_screen 生成矩阵)")
        print(f"[INFO] 矩阵模式: {mpath} {len(sel)} 行, 臂: {[a[0] for a in arms]}")
    else:
        arms = ARMS
        sel = pd.read_parquet(ROOT / "data" / "baseline" / f"{args.formula}_selections_{args.tag}.parquet")
        sel["select_date"] = pd.to_datetime(sel["select_date"])
        print(f"[INFO] {args.formula} 信号 {len(sel)} 条, 区间 {start}~{end}")

        # 因子值: dist_ma20(面板) + turnover_rate/circ_mv(daily_basic 缓存, 无新拉取)
        panels = load_panels(start, warmup_days=400, tag=args.tag)
        sel["dist_ma20"] = lookup(f_dist_ma20(panels), sel["select_date"], sel["stock_code"])
        db = pd.read_parquet(ROOT / "data" / "factors" / f"daily_basic_{args.tag}.parquet")
        sel["_td"] = sel["select_date"].dt.strftime("%Y%m%d")
        sel = sel.merge(db[["ts_code", "trade_date", "turnover_rate", "circ_mv"]]
                        .rename(columns={"ts_code": "stock_code"}),
                        left_on=["stock_code", "_td"], right_on=["stock_code", "trade_date"],
                        how="left").drop(columns=["_td", "trade_date"])
        for c in ("turnover_rate", "circ_mv"):
            sel[c] = pd.to_numeric(sel[c], errors="coerce")
        print(f"[INFO] 因子覆盖: dist_ma20 {sel['dist_ma20'].notna().mean():.1%} "
              f"turnover {sel['turnover_rate'].notna().mean():.1%} circ_mv {sel['circ_mv'].notna().mean():.1%}")

    # 逐臂回测
    results = []
    for name, factor, rule in arms:
        filtered = apply_filter(sel, factor, rule)
        t0 = time.time()
        r = run_backtest(filtered, bt_cfg, stop_config, start, end, name)
        r.update({"arm": name, "kept": len(filtered),
                  "removed": len(sel) - len(filtered), "elapsed_s": int(time.time() - t0)})
        results.append(r)
        print(f"  {name}: 剩 {len(filtered)} 条(剔 {r['removed']}) | 年化 {r['annret']:+.4f} | "
              f"回撤 {r['maxdd']:.4f} | Calmar {r['calmar']} | ({r['elapsed_s']}s)")

    base = results[0]
    for r in results:
        r["verdict"] = "BASE" if r["arm"] == "base" else verdict(r, base)
    summary = pd.DataFrame(results)[["arm", "kept", "removed", "annret", "maxdd",
                                     "sharpe", "winrate", "calmar", "verdict"]]
    out = ROOT / "output" / "reports" / f"overheat_ab_{args.formula}_{args.tag}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(out, index=False, encoding="utf-8-sig")

    print(f"\n=== A/B 终审结果({args.tag}, vs base: 年化 {base['annret']:+.4f} "
          f"回撤 {base['maxdd']:.4f} Calmar {base['calmar']})===")
    print(summary.to_string(index=False))
    print(f"\n[INFO] 明细已存 {out}")
    print("[判定口径] Calmar不差于0.1 且 年化提升 → PASS(增收); "
          "Calmar不差于0.1 且 回撤改善≥2pp 且 年化损失≤1pp → PASS(控回撤)")


if __name__ == "__main__":
    main()
