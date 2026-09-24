"""research/gupiao_stability_sweep.py — GUPIAO 三公式 5m 稳定性参数扫描 (2026-08-20)

用户需求:
- 5 分钟线; 移动止盈用"条件单语义"(trailing confirm=real: 创新高bar不触发, 跳空按开盘价, 触线按线价成交)
- 优先级 = 止损优先 (stop_first: 硬止损先于移动止盈判)
- 三公式: GUPIAO_009 / GUPIAO_011 / GUPIAO_018
- 两时段: 2020-01-01~今 (长窗) 与 2026-01-01~今 (年内短窗) — 跨窗看"稳定性"

口径 (沿用 gs_5m_sweep 已验证口径):
- 信号日 T 最后一根 5m bar 收盘买入 (close_t); 全A前复权; 100万/单票2万; 真实成本
- 5m 稀疏窗口 60 交易日 (time_stop ≤ 45)
- ladder/cond/ATR 不扫 (用户只问止盈止损)

网格 (192 组合 = 4×3×4×4):
  cost ∈ {-6%, -8%, -10%, -12%}
  activation ∈ {3%, 5%, 8%}
  drawdown ∈ {0.5%, 1%, 1.5%, 2%}
  time_days ∈ {10, 20, 30, 45}

稳定性评分: 每组合在两窗各自得 Calmar(年化/回撤), 取 min(两窗Calmar) 作"最差窗
表现"排序 — 一个在长窗+短窗都不崩的组合才是"稳定"。同时报告两窗年化/回撤/夏普。

用法:
  prep    python research/gupiao_stability_sweep.py prep --formula GUPIAO_009 --window 2020
  run     python research/gupiao_stability_sweep.py run  --formula GUPIAO_009 --window 2020 --shard 0 --nshards 6
  report  python research/gupiao_stability_sweep.py report
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tools"))

from quantqq_5m_sweep import CSV_COLUMNS, combo_key, combo_stop_config  # noqa: E402
from utils.config_loader import ConfigLoader  # noqa: E402
from utils.logger import get_logger  # noqa: E402

logger = get_logger("gupiao_stability_sweep")

WINDOW_TD = 80                 # 5m 稀疏窗口交易日 (time_stop 探边到 60 天, 需 >60+15)
CAPITAL = 1_000_000.0          # 100 万 (对齐用户近期回测口径)
MAX_BUY = 20_000.0             # 单票 2 万
PRIORITY = "stop_first"        # 止损优先
CONFIRM = "real"               # 移动止盈条件单语义
MODES = ("close_t", "open_t1")  # 两种买入口径都扫

# 时段定义 (end 取 20260820; 5m 本地缓存最深 08-14, 引擎自动截断)
WINDOWS = {
    "2020": ("20200101", "20260820"),
    "ytd":  ("20260101", "20260820"),
}

BASE = "output/gupiao_stability_sweep"


def _safe(name: str) -> str:
    s = name.strip().rstrip(".")
    for ch in '\\/:*?"<>|':
        s = s.replace(ch, "_")
    return s or "UNNAMED"


def _cache_dir(formula: str, window: str) -> str:
    return os.path.join(BASE, _safe(formula), window, "cache")


def _result_dir(formula: str, window: str) -> str:
    return os.path.join(BASE, _safe(formula), window)


# ---------------------------------------------------------------- grid

def gen_grid():
    """625 组合 (加宽网格): cost×activation×drawdown×time_days, ladder/cond 全关。"""
    combos = []
    for cost in (-0.06, -0.08, -0.10, -0.12, -0.15):
        for act in (0.03, 0.05, 0.08, 0.10, 0.15):
            for dd in (0.005, 0.01, 0.015, 0.02, 0.03):
                for td in (10, 20, 30, 45, 60):
                    combos.append({
                        "cost": cost, "act": act, "dd": dd,
                        "ladder": "off", "levels": [],
                        "time_days": td, "cond_days": 0, "cond_profit": 0.0,
                    })
    return combos


def stop_config(c):
    """combo → stop_config: 止损优先 + 移动止盈条件单语义。"""
    cfg = combo_stop_config(c, PRIORITY)
    cfg["trailing_stop"]["confirm"] = CONFIRM
    return cfg


def mode_run_kwargs(mode: str) -> dict:
    """买入口径 → run_cached 的 (entry_price_mode, filter_limit_up) 映射。

    close_t: T 日收盘买入, 需 T 日涨停过滤 (filter_limit_up=True)。
    open_t1: 次日开盘买入, 引擎 _apply_entry_price_mode 用 T+1 一字板判定替代
    T 日涨停过滤 → filter_limit_up=False (防两口径串味, 见审计 §2)。
    """
    if mode == "close_t":
        return {"entry_price_mode": "close_t", "filter_limit_up": True}
    if mode == "open_t1":
        return {"entry_price_mode": "open_t1", "filter_limit_up": False}
    raise ValueError(f"mode 非法: {mode!r} (合法: close_t/open_t1)")


# ---------------------------------------------------------------- prep

def do_prep(args):
    from backtest.engine import (
        ENGINE_VERSION, BacktestEngine, check_prep_caliber, PREP_SEAM,
    )
    from selection.selector import StockSelector

    formula, window = args.formula, args.window
    start, end = WINDOWS[window]
    cache_dir = _cache_dir(formula, window)
    os.makedirs(cache_dir, exist_ok=True)
    sel_path = os.path.join(cache_dir, "selections.csv")
    meta_path = os.path.join(cache_dir, "meta.json")

    if os.path.exists(sel_path):
        selections = pd.read_csv(sel_path, dtype={"stock_code": str})
        logger.info("[prep:%s/%s] 选股缓存命中 %d 信号", formula, window, len(selections))
    else:
        defaults = ConfigLoader.load_defaults()
        sel_tpl = defaults.get("selection", {})
        sel_cfg = {
            "formula_name": formula, "formula_arg": "",
            "universe": sel_tpl.get("universe", {"type": "50", "exclude_st": True}),
            "period": "1d", "dividend_type": 1,
        }
        t0 = time.time()
        selections = StockSelector(sel_cfg).run(start_time=start, end_time=end)
        if selections is None or len(selections) == 0:
            print(json.dumps({"status": "no_signals", "formula": formula,
                              "window": window, "start": start, "end": end}))
            return
        selections.to_csv(sel_path, index=False)
        logger.info("[prep:%s/%s] 选股 %d 信号 %d 股 %.1fs",
                    formula, window, len(selections),
                    selections["stock_code"].nunique(), time.time() - t0)

    engine = BacktestEngine({
        "initial_capital": CAPITAL, "commission": 0.0003, "slippage": 0.001,
        "stamp_tax": 0.0005, "enable_realistic_costs": True, "period": "5m",
        "position_sizing": {"min_buy_amount": 2000.0, "max_buy_amount": MAX_BUY,
                            "lot_size": 100, "min_lots": 1},
        "use_kline_cache": True,
    })

    # 2026-09-20 审计 P1-4: 本段原是 engine.run() 准备段的第 5 份手工复刻
    # (架构审查 P1-7 只收编了 tools/ 下 4 个, 漏了本文件) → 改调公开接缝
    # prepare_matrices。口径变化提示: 接缝把窗口终点截断到 end (2026-07-21
    # 引擎口径) 且走 _drop_nonstandard_intraday_bars, 旧复刻段都不做 → meta
    # 落 prep_seam 标记, _load_cache 加载旧缓存时 fail-closed 拒绝。
    t0 = time.time()
    prep = engine.prepare_matrices(selections, start, end, WINDOW_TD)
    if prep is None:
        print(json.dumps({"status": "no_kline", "formula": formula,
                          "window": window, "start": start, "end": end}))
        return
    logger.info("[prep:%s/%s] 窗口取数+矩阵准备完成 %.1fs",
                formula, window, time.time() - t0)

    close = prep["close"]
    entries = prep["entries"]
    idx, cols = prep["idx"], prep["cols"]
    high_np, low_np, open_np = prep["high"], prep["low"], prep["open"]
    tradable_np, last_tradable_idx = prep["tradable"], prep["last_tradable_idx"]
    # 口径无关: 落盘的入场信号保持 RAW (不做涨停过滤) —— close_t 的 T 日涨停
    # 过滤 / open_t1 的 T+1 一字板判定都在 run 阶段按 entry_price_mode 应用,
    # 防两口径串味 (本文件与 tools/ 四个 sweep 的唯一有意差异)。

    np.save(os.path.join(cache_dir, "close.npy"), close.values.astype(np.float64))
    np.save(os.path.join(cache_dir, "high.npy"), high_np)
    np.save(os.path.join(cache_dir, "low.npy"), low_np)
    np.save(os.path.join(cache_dir, "open.npy"), open_np)
    np.save(os.path.join(cache_dir, "entries.npy"), entries.values.astype(bool))
    np.save(os.path.join(cache_dir, "tradable.npy"), tradable_np.astype(bool))
    np.save(os.path.join(cache_dir, "last_tradable_idx.npy"),
            np.asarray(last_tradable_idx, dtype=np.int64))
    meta = {
        "index": [str(t) for t in idx], "columns": [str(c) for c in cols],
        "start": start, "end": end, "formula": formula, "window": window,
        "window_td": WINDOW_TD, "capital": CAPITAL, "max_buy": MAX_BUY,
        "engine_version": ENGINE_VERSION, "priority": PRIORITY, "confirm": CONFIRM,
        "prep_seam": PREP_SEAM,
        "n_signals": int(entries.values.sum()), "shape": [int(len(idx)), int(len(cols))],
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    gb = 4 * np.prod(meta["shape"]) * 8 / 1e9
    print(json.dumps({"status": "ok", "formula": formula, "window": window,
                      "shape": meta["shape"], "n_signals": meta["n_signals"],
                      "matrix_gb": round(gb, 2)}))


# ---------------------------------------------------------------- run

def _load_cache(formula, window):
    # 局部导入: 本函数是模块级, 不能靠 do_prep 内的函数级 import (2026-09-20 审计)。
    from backtest.engine import check_prep_caliber

    cache_dir = _cache_dir(formula, window)
    with open(os.path.join(cache_dir, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    check_prep_caliber(meta, where="gupiao_stability_sweep")
    idx = pd.DatetimeIndex(pd.to_datetime(meta["index"]))
    cols = meta["columns"]
    ld = lambda n, mmap=None: np.load(os.path.join(cache_dir, n), mmap_mode=mmap)
    mats = {
        "close_df": pd.DataFrame(ld("close.npy", "r"), index=idx, columns=cols),
        "entries_df": pd.DataFrame(ld("entries.npy"), index=idx, columns=cols),
        "high_np": ld("high.npy", "r"), "low_np": ld("low.npy", "r"),
        "open_np": ld("open.npy", "r"),
        "tradable_np": ld("tradable.npy"),
        "last_tradable_idx": ld("last_tradable_idx.npy"),
    }
    return meta, mats


def do_run(args):
    import logging
    logging.getLogger().setLevel(logging.WARNING)
    from backtest.engine import ENGINE_VERSION, BacktestEngine
    from backtest.prepared import PreparedMatrix

    formula, window, mode = args.formula, args.window, args.mode
    meta, mats = _load_cache(formula, window)
    if meta.get("engine_version") != ENGINE_VERSION:
        logger.warning("[%s/%s] 引擎版本漂移 prep=%s now=%s, 须复跑核对",
                       formula, window, meta.get("engine_version"), ENGINE_VERSION)

    mkw = mode_run_kwargs(mode)
    engine = BacktestEngine({
        "initial_capital": meta.get("capital", CAPITAL),
        "commission": 0.0003, "slippage": 0.001, "stamp_tax": 0.0005,
        "enable_realistic_costs": True, "period": "5m",
        "entry_price_mode": mkw["entry_price_mode"],
        "position_sizing": {"min_buy_amount": 2000.0,
                            "max_buy_amount": meta.get("max_buy", MAX_BUY),
                            "lot_size": 100, "min_lots": 1},
    })

    max_t = WINDOW_TD - 15
    combos = [c for c in gen_grid() if int(c["time_days"]) <= max_t]
    combos = [c for i, c in enumerate(combos) if i % args.nshards == args.shard]
    if args.limit:
        combos = combos[:args.limit]

    out_path = args.out or os.path.join(
        _result_dir(formula, window),
        f"sweep_{mode}_shard{args.shard}of{args.nshards}.csv")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    done = set()
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        prev = pd.read_csv(out_path)
        done = set(prev.loc[prev["annret"].notna(), "key"])
    header = not os.path.exists(out_path) or os.path.getsize(out_path) == 0

    n_done, t_start = 0, time.time()
    with open(out_path, "a", encoding="utf-8", newline="") as fout:
        for c in combos:
            key = combo_key(c)
            if key in done:
                continue
            t0 = time.time()
            try:
                prepared = PreparedMatrix(
                    close=mats["close_df"], entries=mats["entries_df"],
                    high_np=mats["high_np"], low_np=mats["low_np"],
                    open_np=mats["open_np"], tradable_np=mats["tradable_np"],
                    last_tradable_idx=mats["last_tradable_idx"])
                res = engine.run_cached(
                    prepared, stop_config(c),
                    np.array([], dtype=np.float64), np.array([], dtype=np.float64), 0,
                    filter_limit_up=mkw["filter_limit_up"])
                m = res["metrics"]
                row = {"key": key, "cost": c["cost"], "act": c["act"], "dd": c["dd"],
                       "ladder": "off", "time_days": c["time_days"],
                       "cond_days": 0, "cond_profit": 0.0, "mode": mode,
                       "cumret": m.get("cumulative_return", 0),
                       "annret": m.get("annualized_return", 0),
                       "maxdd": m.get("max_drawdown", 0),
                       "sharpe": m.get("sharpe_ratio", 0),
                       "calmar": m.get("calmar_ratio", 0),
                       "winrate": m.get("win_rate", 0),
                       "trades": m.get("total_trades", 0),
                       "profit_factor": m.get("profit_factor", 0),
                       "avg_hold": m.get("avg_hold_days", 0),
                       "elapsed": round(time.time() - t0, 2), "error": ""}
            except Exception as e:
                row = {col: None for col in CSV_COLUMNS + ["mode"]}
                row.update({"key": key, "cost": c["cost"], "act": c["act"],
                            "dd": c["dd"], "ladder": "off",
                            "time_days": c["time_days"], "cond_days": 0,
                            "cond_profit": 0.0, "mode": mode,
                            "elapsed": round(time.time() - t0, 2),
                            "error": f"{type(e).__name__}: {str(e)[:80]}"})
            pd.DataFrame([row], columns=CSV_COLUMNS + ["mode"]).to_csv(
                fout, header=header, index=False)
            header = False
            fout.flush()
            n_done += 1
            if n_done % 20 == 0:
                rate = n_done / (time.time() - t_start)
                eta = (len(combos) - n_done) / rate / 60 if rate > 0 else -1
                print(f"[{formula}/{window} shard{args.shard}] {n_done}/{len(combos)} "
                      f"({rate:.2f}/s, ETA {eta:.0f}min)", flush=True)
    print(json.dumps({"status": "ok", "formula": formula, "window": window,
                      "shard": args.shard, "done": n_done, "out": out_path,
                      "minutes": round((time.time() - t_start) / 60, 1)}))


# ---------------------------------------------------------------- batch

def do_batch(args):
    """编排器: 进程池并行调度所有 (formula×window×mode) 单元的分片子进程。

    绕过 pwsh 后台任务的并发上限 — 单个 batch 进程内部 spawn 子进程, 子进程数
    由 --workers 控制。每个子进程把 stdout 重定向到单元目录下的 .log (避开管道
    EPERM 边界)。run 子命令自带 done-key 断点续跑, 重复调度是幂等的。
    """
    import subprocess
    from concurrent.futures import ThreadPoolExecutor

    units = []
    formulas = [args.formula] if args.formula else ("GUPIAO_009", "GUPIAO_011", "GUPIAO_018")
    for formula in formulas:
        for window in WINDOWS:
            if args.window and window != args.window:
                continue
            meta_path = os.path.join(_cache_dir(formula, window), "meta.json")
            if not os.path.exists(meta_path):
                print(f"[skip] {formula}/{window}: 无 prep (meta.json 缺失)", flush=True)
                continue
            nsh = 3 if window == "ytd" else args.nshards
            for mode in MODES:
                if args.mode and mode != args.mode:
                    continue
                for s in range(nsh):
                    units.append((formula, window, mode, s, nsh))

    print(f"共 {len(units)} 个分片子进程, 并发 {args.workers}", flush=True)

    def run_one(u):
        formula, window, mode, s, nsh = u
        rdir = _result_dir(formula, window)
        os.makedirs(rdir, exist_ok=True)
        logf = os.path.join(rdir, f"shard_{mode}_{s}of{nsh}.log")
        cmd = [sys.executable, os.path.abspath(__file__), "run",
               "--formula", formula, "--window", window, "--mode", mode,
               "--shard", str(s), "--nshards", str(nsh)]
        with open(logf, "w", encoding="utf-8") as fout:
            rc = subprocess.run(cmd, stdout=fout, stderr=subprocess.STDOUT).returncode
        return (formula, window, mode, s, nsh, rc)

    done = fail = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for formula, window, mode, s, nsh, rc in ex.map(run_one, units):
            done += 1
            if rc != 0:
                fail += 1
                print(f"[FAIL rc={rc}] {formula}/{window}/{mode} shard{s}", flush=True)
            if done % 10 == 0 or done == len(units):
                print(f"[batch] {done}/{len(units)} 分片完成, 失败 {fail}", flush=True)
    print(json.dumps({"status": "ok" if fail == 0 else "partial",
                      "units": len(units), "failed": fail}))


# ---------------------------------------------------------------- report

def do_report(args):
    rows = []
    for formula in os.listdir(BASE):
        fdir = os.path.join(BASE, formula)
        if not os.path.isdir(fdir):
            continue
        for window in os.listdir(fdir):
            wdir = os.path.join(fdir, window)
            if not os.path.isdir(wdir):
                continue
            for f in os.listdir(wdir):
                if f.startswith("sweep_") and f.endswith(".csv"):
                    df = pd.read_csv(os.path.join(wdir, f))
                    df["formula"] = formula
                    df["window"] = window
                    rows.append(df)
    if not rows:
        print("无扫描结果 (先跑 prep + run)"); return
    df = pd.concat(rows, ignore_index=True)
    df = df[df["annret"].notna()].drop_duplicates(
        subset=["formula", "window", "mode", "key"], keep="last")

    # 跨窗稳定性: 同一 (formula, mode, key) 的两窗 Calmar 取 min 作"最差窗表现"
    piv = df.pivot_table(index=["formula", "mode", "cost", "act", "dd", "time_days"],
                         columns="window",
                         values=["annret", "maxdd", "calmar", "sharpe", "trades"])
    piv.columns = [f"{m}_{w}" for m, w in piv.columns]
    piv = piv.reset_index()
    both = piv.dropna(subset=["calmar_2020", "calmar_ytd"]).copy()
    both["min_calmar"] = both[["calmar_2020", "calmar_ytd"]].min(axis=1)
    both["min_annret"] = both[["annret_2020", "annret_ytd"]].min(axis=1)
    both = both.sort_values("min_calmar", ascending=False)

    print(f"\n=== 跨窗稳定 Top 25 (min(两窗Calmar) 降序, 止损优先+条件单语义) ===")
    show = both.head(25)[["formula", "mode", "cost", "act", "dd", "time_days",
                          "annret_2020", "annret_ytd", "maxdd_2020", "maxdd_ytd",
                          "calmar_2020", "calmar_ytd", "min_calmar"]]
    print(show.to_string(index=False))

    print("\n=== 各公式最优 (按 min_calmar) ===")
    per = both.sort_values("min_calmar", ascending=False).drop_duplicates("formula")
    print(per[["formula", "mode", "cost", "act", "dd", "time_days",
               "annret_2020", "annret_ytd", "min_calmar"]].to_string(index=False))

    out = os.path.join(BASE, "stability_report.csv")
    both.to_csv(out, index=False)
    print(f"\n明细: {out}")
    return both


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("prep")
    pp.add_argument("--formula", required=True)
    pp.add_argument("--window", required=True, choices=list(WINDOWS))
    pr = sub.add_parser("run")
    pr.add_argument("--formula", required=True)
    pr.add_argument("--window", required=True, choices=list(WINDOWS))
    pr.add_argument("--mode", required=True, choices=list(MODES))
    pr.add_argument("--shard", type=int, default=0)
    pr.add_argument("--nshards", type=int, default=1)
    pr.add_argument("--out", default=None)
    pr.add_argument("--limit", type=int, default=0)
    pb = sub.add_parser("batch")
    pb.add_argument("--workers", type=int, default=6)
    pb.add_argument("--nshards", type=int, default=6)
    pb.add_argument("--formula", default=None)
    pb.add_argument("--window", default=None, choices=list(WINDOWS))
    pb.add_argument("--mode", default=None, choices=list(MODES))
    sub.add_parser("report")
    args = ap.parse_args()
    if args.cmd == "prep":
        do_prep(args)
    elif args.cmd == "run":
        do_run(args)
    elif args.cmd == "batch":
        do_batch(args)
    else:
        do_report(args)


if __name__ == "__main__":
    main()
