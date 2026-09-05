# -*- coding: utf-8 -*-
"""Stage 4 五分钟执行阶段 (2026-08-26, 全新编写)。

对 S1-S3 幸存公式: 日线信号不变, 买入后以 5 分钟线执行三件套
(盘中触线即时成交, run() 慢路径 + degrade_5m 缺口降级日线)。
区间: 每股 5m 数据最早可得日起 (用户要求) — 实际以引擎数据为准。

对比维度: 同公式 S2 区间 (2014-2026) 日线版 vs 5m 版绩效。

用法:
    python tools/formula_pipeline/stage_5m.py --run-dir <dir>
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from tools.formula_pipeline.bootstrap import (  # noqa: E402
    load_benchmark, load_stocks)
from tools.formula_pipeline.common import (  # noqa: E402
    GS_DIR, load_json, read_formula_txt, save_json)
from tools.formula_pipeline.stage_d1 import (  # noqa: E402
    ENGINE_CFG, S2_RANGE, bench_annual, filter_first_signal,
    load_calendar, signal_to_selections, stock_pool)
from tools.formula_pipeline.interpreter.runner import (  # noqa: E402
    run_formula_batch)

ENGINE_5M_CFG = dict(ENGINE_CFG, period="5m", degrade_5m=True,
                     matrix_cache=False)   # degrade 路径不支持矩阵缓存

# 5m 执行区间: 2024-06-27 起 (本地 5m 数据全量完整段; 之前部分票缺 5m)
# 用户拍板方案 A (17:52): 不用全历史, 干净段出对比结论
M5_RANGE = ("20240627", "20260825")


SEG_YEARS = 1


def _segments(start: str, end: str, years: int = SEG_YEARS):
    """闭区间切分: [2014-01-01, 2026-08-25] → [(2014-01-01,2015-12-31), ...]"""
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    out = []
    cur = s
    while cur <= e:
        seg_end = pd.Timestamp(year=cur.year + years - 1, month=12, day=31)
        seg_end = min(seg_end, e)
        out.append((cur.strftime("%Y%m%d"), seg_end.strftime("%Y%m%d")))
        cur = pd.Timestamp(year=cur.year + years, month=1, day=1)
    return out


def _max_dd_from_segments(all_equity: list) -> float:
    """分段拼接的最大回撤: 全局峰值追踪 (段间用累计净值)。"""
    if not all_equity:
        return 0.0
    peak = 1.0
    max_dd = 0.0
    cum = 1.0
    for eq in all_equity:
        col = "equity" if "equity" in eq.columns else \
              "total_value" if "total_value" in eq.columns else eq.columns[-1]
        vals = eq[col].values
        first = float(vals[0])
        if first <= 0:
            continue
        for v in vals:
            nv = cum * (float(v) / first)
            if nv > peak:
                peak = nv
            dd = nv / peak - 1.0
            if dd < max_dd:
                max_dd = dd
        cum = cum * (float(vals[-1]) / first)
    return max_dd


def _seg_metrics(eq, tr):
    """单段 (一年/半年) 的独立指标 — 用于年度年化拆分。"""
    if eq is None or len(eq) == 0:
        return None
    col = ("equity" if "equity" in eq.columns
           else "total_value" if "total_value" in eq.columns
           else eq.columns[-1])
    vals = eq[col].values.astype(float)
    v0 = float(vals[0])
    if v0 <= 0:
        return None
    v1 = float(vals[-1])
    cum = v1 / v0 - 1.0
    n_days = len(vals)
    n_years = max(n_days / 244.0, 0.25)
    ar = (1.0 + cum) ** (1.0 / n_years) - 1.0 if cum > -1 else cum
    peak = vals[0]
    mdd = 0.0
    for v in vals:
        if v > peak:
            peak = v
        dd = v / peak - 1.0
        if dd < mdd:
            mdd = dd
    n_tr = len(tr) if tr is not None else 0
    wr = float((tr["profit_pct"] > 0).mean()) if n_tr else 0.0
    return {"cumulative_return": cum, "annualized_return": ar,
            "max_drawdown": mdd, "total_trades": n_tr, "win_rate": wr}


def run_backtest_5m(selections, start, end):
    from backtest.engine import BacktestEngine
    eng = BacktestEngine(dict(ENGINE_5M_CFG))
    result = eng.run(selections=selections, start_time=start, end_time=end)
    # 引擎 run() 不带 stop_config 时读 config/default.yaml — 显式传
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--yearly", action="store_true",
                    help="保存每段(年度)独立年化到 results_5m_yearly.json")
    args = ap.parse_args()
    run_dir = Path(args.run_dir)

    results = load_json(run_dir / "stage_d1" / "results.json")
    survivors = [r for r in results if r["status"] == "survivor"]
    if args.limit:
        survivors = survivors[:args.limit]
    if not survivors:
        print("[5m] 无幸存者, 跳过")
        return
    print(f"[5m] 待跑 {len(survivors)} 个幸存公式 (5m 执行, 慢路径)")

    stocks = load_stocks()
    bench = load_benchmark()
    calendar = load_calendar(bench)
    codes = stock_pool(stocks)
    # 5m 只用本地已有缓存的股票 (VERA_KLINE_READONLY 下 miss 不回源;
    # 引擎 degrade_5m 会把缺 5m 的股-天降级日线, 保住信号)
    kdir = Path(r"E:\1target\VERA\data\kline_cache\5m")
    have5m = {p.stem for p in kdir.glob("*.parquet")}
    n_before = len(codes)
    codes = [c for c in codes if c in have5m]
    print(f"[5m] 池 {n_before} → {len(codes)} 只 (保留有 5m 缓存的)")

    out_dir = run_dir / "stage4_5m"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_fp = out_dir / ("results_5m_yearly.json" if args.yearly
                        else "results_5m.json")
    out = load_json(out_fp) if out_fp.exists() else {}

    from backtest.engine import BacktestEngine
    from backtest.stop_config import load_stop_config
    stop_cfg = load_stop_config()
    # 用户参数覆盖: 6% 硬止损 / 3.5%-1% 移动止盈 / 10 日强平 / 止损优先 / 无阶梯
    stop_cfg.update({
        "priority": "stop_first",
        "cost_stop": {"enabled": True, "threshold": -0.06},
        "trailing_stop": {"enabled": True, "activation": 0.035,
                          "drawdown": 0.01},
        "ladder_tp": {"enabled": False, "levels": []},
        "time_stop": {"enabled": True, "max_hold_days": 10},
    })

    for rec in survivors:
        name = rec["name"]
        if name in out:
            print(f"[5m] {name}: 已完成 (断点续跑命中)")
            continue
        t0 = time.time()
        try:
            f = read_formula_txt(GS_DIR / rec["file"])
            sig_dates = calendar[(calendar >= pd.Timestamp("20040101"))]
            signal, idx, used = run_formula_batch(
                f["source"], f["params"], codes,
                start="20040101", end="20260825", chunk_size=400,
                fixed_index=sig_dates)
            sel_all = filter_first_signal(
                signal_to_selections(signal, idx, used, name, *M5_RANGE),
                calendar, 30)
            if not len(sel_all):
                out[name] = {"status": "no_signals"}
                save_json(out_fp, out)
                continue

            # ── 分段回测 + 内存拼接 (用户 08:58 拍板) ──
            # 每段独立跑 5m (矩阵 ~1.5GB 不爆), trades 拼接后重算指标
            segs = _segments(M5_RANGE[0], M5_RANGE[1])
            all_trades, all_equity = [], []
            per_seg = {}
            eng = BacktestEngine(dict(ENGINE_5M_CFG))
            for si, (ss, se) in enumerate(segs):
                sub = sel_all[(sel_all["select_date"] >= ss)
                              & (sel_all["select_date"] <= se)]
                if not len(sub):
                    per_seg[ss[:4]] = None
                    continue
                result = eng.run(selections=sub, start_time=ss,
                                 end_time=se, stop_config=stop_cfg)
                tr = result.get("trades")
                eq = result.get("equity_curve")
                if tr is not None and len(tr):
                    all_trades.append(tr)
                if eq is not None and len(eq):
                    all_equity.append(eq)
                per_seg[ss[:4]] = _seg_metrics(eq, tr)
                print(f"[5m] {name} 段{si+1}/{len(segs)} {ss[:4]}~{se[:4]}: "
                      f"{len(tr) if tr is not None else 0} 笔 "
                      f"年化 {(per_seg[ss[:4]] or {}).get('annualized_return',0)*100:+.1f}%"
                      if per_seg[ss[:4]] else
                      f"[5m] {name} 段{si+1}/{len(segs)} {ss[:4]}~{se[:4]}: 无数据",
                      flush=True)
            trades = pd.concat(all_trades, ignore_index=True) \
                if all_trades else pd.DataFrame()
            n_trades = len(trades)
            if all_equity:
                cum, n_days = 1.0, 0
                for eq in all_equity:
                    col = "equity" if "equity" in eq.columns else \
                      "total_value" if "total_value" in eq.columns else eq.columns[-1]
                v0 = float(eq[col].iloc[0])
                cum *= float(eq[col].iloc[-1]) / v0 if v0 > 0 else 1.0
                n_days += len(eq)
                total_ret = cum - 1.0
                n_years = max(n_days / 244.0, 0.25)
                ar = (1.0 + total_ret) ** (1.0 / n_years) - 1.0 \
                    if total_ret > -1 and n_years > 0 else total_ret
                pf = 0.0
                if n_trades:
                    gp = trades.loc[trades["profit_pct"] > 0, "profit_pct"].sum()
                    gl = -trades.loc[trades["profit_pct"] <= 0, "profit_pct"].sum()
                    pf = float(gp / gl) if gl > 0 else float("inf")
                m = {"cumulative_return": total_ret, "annualized_return": ar,
                     "total_trades": n_trades,
                     "win_rate": float((trades["profit_pct"] > 0).mean())
                     if n_trades else 0.0,
                     "max_drawdown": _max_dd_from_segments(all_equity),
                     "profit_factor": pf}
            else:
                m = {}
            out[name] = {
                "status": "ok" if n_trades else "no_metrics",
                "metrics": {k: (float(v) if isinstance(v, (int, float)) else v)
                            for k, v in m.items()
                            if isinstance(v, (int, float))},
                "n_trades": n_trades,
                "n_segments": len(segs),
                "per_segment": {k: (v if v is None else
                                    {kk: (float(vv) if isinstance(vv, (int, float))
                                          else vv) for kk, vv in v.items()})
                                for k, v in per_seg.items()},
                "d1_s2_benchmark": rec.get("s2", {}).get("metrics", {}),
                "elapsed_s": round(time.time() - t0, 1),
            }
            save_json(out_fp, out)
            ar5 = (m.get("annualized_return") or 0) * 100
            ar1 = (rec.get("s2", {}).get("metrics", {})
                   .get("annualized_return") or 0) * 100
            print(f"[5m] {name}: 年化 {ar5:+.1f}% (日线版 {ar1:+.1f}%) "
                  f"({time.time()-t0:.0f}s)", flush=True)
        except Exception as e:
            out[name] = {"status": "error", "error": str(e)[:200]}
            save_json(out_fp, out)
            print(f"[5m] {name}: ERROR {str(e)[:100]}", flush=True)

    print(f"[5m] 完成; OUT {out_fp}")


if __name__ == "__main__":
    main()
