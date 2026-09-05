# -*- coding: utf-8 -*-
"""Stage 1/2/3 日线三阶段回测 (2026-08-26, 全新编写)。

每公式全链路:
  源码 → 本地解释器全区间信号矩阵 → selections 长表 (30日首信号过滤)
  → engine.run × 3 区间切片 (三件套出场) → 指标 → 分阶段淘汰判定 → 结果落盘

区间 (计划书 §5):
  S1: 2020-01-01 ~ 2026-07-31   (先杀 2/3)
  S2: 2014-01-01 ~ 2026-08-25   (幸存者, 更严)
  S3: 2005-01-01 ~ 2024-12-31   (换年代考卷)

用法:
    python tools/formula_pipeline/stage_d1.py --run-dir <dir> [--formula 名字]
"""
import argparse
import os
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

from tools.formula_pipeline.bootstrap import load_benchmark, load_stocks  # noqa: E402
from tools.formula_pipeline.common import (  # noqa: E402
    GS_DIR, load_json, read_formula_txt, save_json)
from tools.formula_pipeline.interpreter.runner import (  # noqa: E402
    run_formula_batch)
from tools.formula_pipeline.interpreter.functions import (  # noqa: E402
    UnsupportedFormula)

ROOT = Path(r"E:\1target\VERA")
CAL_PATH = ROOT / "data" / "trading_calendar.json"

# ---------------------------------------------------------------- 参数 (用户拍板)

S1_RANGE = ("20200101", "20260731")
S2_RANGE = ("20140101", "20260825")
S3_RANGE = ("20050101", "20241231")
SIGNAL_RANGE = ("20040101", "20260825")  # 信号矩阵一次算全区间

ENGINE_CFG = {
    "initial_capital": 1_000_000.0,
    "commission": 0.0003, "slippage": 0.001, "stamp_tax": 0.0005,
    "enable_realistic_costs": True, "period": "1d",
    "use_kline_cache": True,
    "matrix_cache": True,            # P2: 矩阵级缓存, 同 selections 复用准备段
    "sell_cooldown_days": 20,
    "entry_price_mode": "close_t",
    "position_sizing": {"min_buy_amount": 2000.0, "max_buy_amount": 20000.0,
                        "lot_size": 100, "min_lots": 1},
}
STOP_CONFIG = {
    "priority": "stop_first",                      # 止损优先
    "cost_stop": {"enabled": True, "threshold": -0.06},   # 6% 硬止损
    "trailing_stop": {"enabled": True, "activation": 0.035, "drawdown": 0.01},
    "ladder_tp": {"enabled": False, "levels": []},        # 不用阶段止盈
    "time_stop": {"enabled": True, "max_hold_days": 10},  # 10 交易日强平
    "cond_time_stop": {"enabled": False},
    "first_day": {"enabled": False},
    "formula_sell": {"enabled": False},
    "capabilities": {"formula_exit": True, "gap_protection": True,
                     "delisting": True},
}
FIRST_SIGNAL_WINDOW = 30  # 30 日首信号过滤


# ---------------------------------------------------------------- 工具

def load_calendar(bench: pd.DataFrame) -> pd.DatetimeIndex:
    """交易日历 = 基准指数日期 (全历史; trading_calendar.json 仅滚动近一年)。"""
    return pd.DatetimeIndex(pd.to_datetime(bench["date"]))


def stock_pool(stocks: dict) -> list:
    """沪深 A 股, 剔 ST, 且本地 parquet 存在 (缺缓存的次新/退市股跳过)。"""
    kdir = ROOT / "data" / "kline_cache" / "1d"
    out = []
    for code, name in stocks.items():
        if name and "ST" in name.upper():
            continue
        num, _, mkt = code.partition(".")
        if mkt == "SH" and num.startswith(("60", "68")):
            pass
        elif mkt == "SZ" and num.startswith(("00", "30")):
            pass
        else:
            continue
        if (kdir / f"{code}.parquet").exists():
            out.append(code)
    return sorted(out)


def filter_first_signal(df: pd.DataFrame, calendar: pd.DatetimeIndex,
                        window: int = 30) -> pd.DataFrame:
    """30 日首信号过滤 (自写): 同票与上一信号交易日距离 > window 才保留。"""
    if df is None or not len(df):
        return df
    cal_pos = pd.Series(np.arange(len(calendar)), index=calendar)
    df = df.copy()
    df["select_date"] = pd.to_datetime(df["select_date"])
    df["_pos"] = df["select_date"].map(cal_pos)
    df = df.sort_values(["stock_code", "_pos"]).reset_index(drop=True)
    keep = np.ones(len(df), dtype=bool)
    last_pos = {}
    pos_arr = df["_pos"].values
    code_arr = df["stock_code"].values
    for i in range(len(df)):
        c, p = code_arr[i], pos_arr[i]
        if np.isnan(p):
            continue
        lp = last_pos.get(c)
        if lp is not None and (p - lp) <= window:
            keep[i] = False
        else:
            last_pos[c] = p   # 被丢弃的信号同样占用窗口 (TDX 旧语义)
    out = df[keep].drop(columns="_pos")
    return out.reset_index(drop=True)


def signal_to_selections(signal: np.ndarray, dates: pd.DatetimeIndex,
                         codes: list, formula_name: str,
                         start: str, end: str) -> pd.DataFrame:
    """布尔矩阵 → 长表 (区间裁剪由调用方完成)。"""
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    m = (dates >= s) & (dates <= e)
    sub = signal[m]
    sub_dates = dates[m]
    if not len(sub) or not sub.any():
        return pd.DataFrame(columns=["stock_code", "select_date",
                                     "formula_name"])
    r, c = np.nonzero(sub)
    return pd.DataFrame({
        "stock_code": [codes[j] for j in c],
        "select_date": sub_dates[r],
        "formula_name": formula_name,
    })


# ---------------------------------------------------------------- 回测

def run_backtest(selections: pd.DataFrame, start: str, end: str):
    """engine.run 标准回测 → (metrics, trades)。"""
    from backtest.engine import BacktestEngine
    eng = BacktestEngine(dict(ENGINE_CFG))
    result = eng.run(selections=selections, start_time=start, end_time=end,
                     stop_config=dict(STOP_CONFIG))
    metrics = result.get("metrics") or {}
    trades = result.get("trades")
    return metrics, trades


def bench_annual(bench: pd.DataFrame, start: str, end: str) -> float:
    """基准区间年化。"""
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    sub = bench[(bench["date"] >= s) & (bench["date"] <= e)]
    if len(sub) < 2:
        return 0.0
    ret = sub["close"].iloc[-1] / sub["close"].iloc[0] - 1.0
    years = len(sub) / 244.0
    return (1.0 + ret) ** (1.0 / years) - 1.0 if years > 0 and ret > -1 else ret


def monthly_win_ratio(trades: pd.DataFrame) -> float:
    """盈利月占比。"""
    if trades is None or not len(trades):
        return 0.0
    t = trades.copy()
    t["exit_date"] = pd.to_datetime(t["exit_date"])
    t["month"] = t["exit_date"].dt.to_period("M")
    pnl = t["profit_pct"] * t.get("shares", 1)
    g = pnl.groupby(t["month"]).sum() if "profit_pct" in t else None
    if g is None or not len(g):
        return 0.0
    return float((g > 0).mean())


def subwindow_min_return(trades: pd.DataFrame,
                         windows: list) -> float:
    """任一子窗口收益的最小值 (按平仓盈亏近似区间收益)。"""
    if trades is None or not len(trades):
        return -1.0
    t = trades.copy()
    t["exit_date"] = pd.to_datetime(t["exit_date"])
    t["pnl"] = t["profit_pct"]
    worst = 1.0
    for ws, we in windows:
        m = (t["exit_date"] >= pd.Timestamp(ws)) & (t["exit_date"] <= pd.Timestamp(we))
        if m.any():
            r = t.loc[m, "pnl"].sum()
            worst = min(worst, r)
    return worst


# ---------------------------------------------------------------- 淘汰标准

def judge_s1(m, trades, bench_ret) -> dict:
    fail = []
    if not (m.get("annualized_return", 0) > bench_ret):
        fail.append(f"年化{m.get('annualized_return', 0)*100:.1f}%≤基准{bench_ret*100:.1f}%")
    if not (m.get("max_drawdown", 0) <= 0.45):
        fail.append(f"回撤{m.get('max_drawdown', 0)*100:.0f}%>45%")
    if not (m.get("total_trades", 0) >= 30):
        fail.append(f"交易{m.get('total_trades', 0)}<30")
    wr, pf = m.get("win_rate", 0), m.get("profit_factor", 0)
    if not (wr >= 0.40 or (isinstance(pf, (int, float)) and pf >= 1.3)):
        fail.append(f"胜率{wr*100:.0f}%<40%且PF{pf}<1.3")
    return {"pass": not fail, "fail_reasons": fail}


def judge_s2(m, trades, bench_ret) -> dict:
    fail = []
    if not (m.get("annualized_return", 0) > bench_ret):
        fail.append(f"年化{m.get('annualized_return', 0)*100:.1f}%≤基准")
    if not (m.get("max_drawdown", 0) <= 0.40):
        fail.append(f"回撤{m.get('max_drawdown', 0)*100:.0f}%>40%")
    if not (m.get("total_trades", 0) >= 80):
        fail.append(f"交易{m.get('total_trades', 0)}<80")
    mwr = monthly_win_ratio(trades)
    if not (mwr >= 0.45):
        fail.append(f"盈利月占比{mwr*100:.0f}%<45%")
    return {"pass": not fail, "fail_reasons": fail, "monthly_win": mwr}


def judge_s3(m, trades, bench_ret) -> dict:
    fail = []
    if not (m.get("annualized_return", 0) > bench_ret):
        fail.append(f"年化{m.get('annualized_return', 0)*100:.1f}%≤基准")
    if not (m.get("max_drawdown", 0) <= 0.45):
        fail.append(f"回撤{m.get('max_drawdown', 0)*100:.0f}%>45%")
    worst = subwindow_min_return(trades, [
        ("2005-01-01", "2009-12-31"), ("2010-01-01", "2014-12-31"),
        ("2015-01-01", "2019-12-31"), ("2020-01-01", "2024-12-31")])
    if not (worst > -0.20):
        fail.append(f"5年子窗最差收益{worst*100:.0f}%≤-20%")
    return {"pass": not fail, "fail_reasons": fail, "subwindow_worst": worst}


# ---------------------------------------------------------------- 单公式全链路

def run_one_formula(entry: dict, codes: list, calendar: pd.DatetimeIndex,
                    bench: pd.DataFrame, out_dir: Path, state: dict) -> dict:
    name = entry["name"]
    rec = {"name": name, "file": entry["file"], "params": entry["params"],
           "status": "error", "elapsed_s": 0.0}
    t0 = time.time()
    try:
        f = read_formula_txt(GS_DIR / entry["file"])
        sig_dates = calendar[(calendar >= pd.Timestamp(SIGNAL_RANGE[0]))
                             & (calendar <= pd.Timestamp(SIGNAL_RANGE[1]))]
        signal, idx, used_codes = run_formula_batch(
            f["source"], f["params"], codes,
            start=SIGNAL_RANGE[0], end=SIGNAL_RANGE[1], chunk_size=400,
            fixed_index=sig_dates)
        n_sig_total = int(signal.sum())
        if n_sig_total == 0:
            rec.update(status="no_signals", n_signals=0,
                       elapsed_s=round(time.time() - t0, 1))
            return rec
        rec["n_signals_raw"] = n_sig_total
        # 信号密度守门 (Warden 质检前置): 全区间日均信号 > 200 票 → 无法实操
        n_days = max(int((signal.sum(axis=1) > 0).sum()), 1)
        avg_per_day = n_sig_total / n_days
        rec["signal_density"] = round(avg_per_day, 1)
        if avg_per_day > 200:
            rec.update(status="killed_density",
                       error=f"日均信号{avg_per_day:.0f}只>200, 无法实操",
                       elapsed_s=round(time.time() - t0, 1))
            return rec

        # 三区间: 信号矩阵只算一次, 切片复用; 逐区间回测+判定, 不过即杀
        for tag, rng, judge_fn in (
                ("s1", S1_RANGE, judge_s1),
                ("s2", S2_RANGE, judge_s2),
                ("s3", S3_RANGE, judge_s3)):
            sel = filter_first_signal(
                signal_to_selections(signal, idx, used_codes, name, *rng),
                calendar, FIRST_SIGNAL_WINDOW)
            if not len(sel):
                rec.update(status="no_signals", n_signals=0,
                           elapsed_s=round(time.time() - t0, 1))
                return rec
            m, trades = run_backtest(sel, *rng)
            if not m:
                rec.update(status="no_metrics",
                           elapsed_s=round(time.time() - t0, 1))
                return rec
            bench_ret = bench_annual(bench, *rng)
            j = judge_fn(m, trades, bench_ret)
            rec[tag] = {"metrics": {k: _num(v) for k, v in m.items()
                                    if isinstance(v, (int, float))},
                        "judge": j, "bench": _num(bench_ret),
                        "n_trades": int(len(trades)) if trades is not None else 0,
                        "n_signals": int(len(sel))}
            if not j["pass"]:
                rec.update(status=f"killed_{tag}",
                           elapsed_s=round(time.time() - t0, 1))
                return rec
        rec["status"] = "survivor"
    except UnsupportedFormula as e:
        rec.update(status="unsupported", error=str(e)[:200])
    except Exception as e:
        rec.update(status="error", error=f"{type(e).__name__}: {e}"[:300])
    rec["elapsed_s"] = round(time.time() - t0, 1)
    return rec


def _num(v):
    return float(v) if isinstance(v, (int, float, np.floating)) else v


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--formula", default=None, help="只跑指定公式 (调试)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch", default="gs_1_", help="批次前缀 (gs_0_/gs_1_/...)")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    out_dir = run_dir / "stage_d1"
    out_dir.mkdir(parents=True, exist_ok=True)

    stocks = load_stocks()
    bench = load_benchmark()
    calendar = load_calendar(bench)
    codes = stock_pool(stocks)
    print(f"[d1] 池 {len(codes)} 只 | 基准 {len(bench)} 行 | 日历 {len(calendar)} 天")

    cov = load_json(run_dir / "stage0_scan" / "parse_coverage.json")
    # 可执行清单 = S0 幸存 ∩ 解析可执行 (verify_parse 已剔除 unsupported)
    s0 = load_json(run_dir / "stage0_scan" / "report.json")
    err_names = {e["name"] for e in cov["exec_errors"]}
    unsup_names = set()
    for r, _n in cov["unsupported_reasons"].items():
        pass  # reasons 无名字; 逐个执行时 Unsupported 自然出局
    todo = [e for e in s0["ok"]
             if e["name"] not in err_names and e["name"] not in unsup_names]
    if args.formula:
        todo = [e for e in todo if args.formula in e["name"]]
    if args.limit:
        todo = todo[:args.limit]
    print(f"[d1] 待跑 {len(todo)} 个公式"
          f"{' (公式=' + args.formula + ')' if args.formula else ''}")

    state_fp = out_dir / "state.json"
    state = load_json(state_fp) if state_fp.exists() else {"done": {}}
    results_fp = out_dir / "results.json"
    results = load_json(results_fp) if results_fp.exists() else []

    todo = [e for e in todo if e["name"] not in state["done"]]
    if not todo:
        print("[d1] 全部已完成 (断点续跑命中)")
        return

    # 多进程并行: 每公式一任务。进程数克制——引擎准备段峰值内存 ~1GB/进程
    # (4975股×5500天 float64 矩阵×6字段), 31 进程会 MemoryError, 8 进程稳妥
    import multiprocessing as mp
    nproc = min(8, max(1, (os.cpu_count() or 4) - 1))
    print(f"[d1] 并行 {nproc} 进程 × {len(todo)} 公式")

    _G = dict(codes=codes, calendar=calendar, bench=bench,
              out_dir=str(out_dir))
    t0 = time.time()
    done_count = 0
    with mp.Pool(nproc, initializer=_init_worker, initargs=(_G,)) as pool:
        for rec in pool.imap_unordered(_worker_one, todo, chunksize=1):
            results.append(rec)
            state["done"][rec["name"]] = rec["status"]
            save_json(results_fp, results)
            save_json(state_fp, state)
            done_count += 1
            avg = (time.time() - t0) / done_count
            print(f"[d1 {done_count}/{len(todo)}] {rec['name']}: "
                  f"{rec['status']} ({rec['elapsed_s']}s, "
                  f"ETA {avg * (len(todo) - done_count) / nproc / 60:.0f}min)",
                  flush=True)

    # 汇总
    from collections import Counter
    stat = Counter(r["status"] for r in results)
    print(f"[d1] 完成: {dict(stat)}")
    print(f"[OUT] {results_fp}")


# ---------------------------------------------------------------- 多进程

_G = {}


def _init_worker(g):
    global _G
    _G = g
    import os as _os
    _os.environ.setdefault("VERA_KLINE_READONLY", "1")


def _worker_one(entry):
    import pandas as _pd
    g = _G
    cal = g["calendar"]
    cal = _pd.DatetimeIndex(cal) if not isinstance(cal, _pd.DatetimeIndex) else cal
    return run_one_formula(entry, g["codes"], cal, g["bench"],
                           Path(g["out_dir"]), {})


if __name__ == "__main__":
    main()
