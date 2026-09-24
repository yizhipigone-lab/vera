# -*- coding: utf-8 -*-
"""Stage 5 分池子对比 (2026-08-26, 全新编写)。

对 S1-S3 幸存公式: 同一信号矩阵按池子成分过滤行 → 各池分别回测对比。
池子: 全A(基线) / 沪深300 / 中证500 / 中证1000 / 创业板(≈创业板50) / 科创板(≈科创50)
成分来源: TDX get_stock_list 在线拉取缓存 (仅当前成分, 报告标注幸存者偏差)。

用法:
    python tools/formula_pipeline/stage_pools.py --run-dir <dir>
"""
import argparse
import sys
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from tools.formula_pipeline.bootstrap import (  # noqa: E402
    CACHE_DIR, load_benchmark, load_stocks)
from tools.formula_pipeline.common import (  # noqa: E402
    GS_DIR, load_json, read_formula_txt, save_json)
from tools.formula_pipeline.stage_d1 import (  # noqa: E402
    S2_RANGE, bench_annual, filter_first_signal,
    load_calendar, run_backtest, signal_to_selections)
from tools.formula_pipeline.interpreter.runner import (  # noqa: E402
    run_formula_batch)

# TDX list_type → 池子 (创业板50/科创50 无接口, 用户拍板整板近似)
POOLS = {
    "hs300": "23",
    "zz500": "24",
    "zz1000": "25",
    "cyb": "51",     # 创业板整板 ≈ 创业板50
    "kcb": "52",     # 科创板整板 ≈ 科创50
}


def load_pool_members(pool_key: str, list_type: str) -> list:
    """池子成分 (TDX 在线拉一次, JSON 缓存; 失败时空列表=该池跳过)。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fp = CACHE_DIR / f"pool_{pool_key}.json"
    if fp.exists():
        return json_load(fp)
    try:
        from core.data_fetcher import DataFetcher
        DataFetcher._ensure_ready()
        raw = DataFetcher._connector().tq().get_stock_list(list_type, list_type=1)
        codes = sorted(str(s["Code"]).strip() for s in raw
                       if isinstance(s, dict) and s.get("Code"))
        if codes:
            fp.write_text(json_dumps(codes), encoding="utf-8")
        return codes
    except Exception as e:
        print(f"[pools] {pool_key} 拉取失败: {e}")
        return []


def json_dumps(obj):
    import json as _j
    return _j.dumps(obj, ensure_ascii=False, indent=1)


def json_load(fp):
    import json as _j
    return _j.loads(fp.read_text(encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    args = ap.parse_args()
    run_dir = Path(args.run_dir)

    results = load_json(run_dir / "stage_d1" / "results.json")
    survivors = [r for r in results if r["status"] == "survivor"]
    if not survivors:
        print("[pools] 无幸存者, 跳过")
        return
    print(f"[pools] 幸存者 {len(survivors)} 个")

    stocks = load_stocks()
    bench = load_benchmark()
    calendar = load_calendar(bench)
    all_codes = sorted(c for c, n in stocks.items()
                       if not (n and "ST" in n.upper())
                       and (c[:2] in ("60", "68", "00", "30")))

    pools = {}
    for key, lt in POOLS.items():
        m = load_pool_members(key, lt)
        if m:
            pools[key] = m
    print(f"[pools] 池子: " + ", ".join(f"{k}={len(v)}" for k, v in pools.items())
          + " (全A基线沿用 d1/S2 结果, 不重跑)")

    out_dir = run_dir / "stage5_pools"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_fp = out_dir / "pools.json"
    out = load_json(out_fp) if out_fp.exists() else {}
    bench_ret = bench_annual(bench, *S2_RANGE)

    # 多进程 (与 d1 同款: 8 进程, 每进程结果经队列回主进程落盘)
    import multiprocessing as mp
    _G = dict(codes=all_codes, calendar=calendar,
              pools={k: list(v) for k, v in pools.items()},
              bench=str(bench.to_parquet and ""),  # bench 不跨进程传, 重载
              )
    # bench 以路径传递 (DataFrame 不可 pickle 跨池? pandas 可 pickle; 用缓存路径更稳)
    _G["bench_fp"] = str(CACHE_DIR / "bench_hs300.parquet")

    todo = [r for r in survivors if r["name"] not in out]
    print(f"[pools] 待跑 {len(todo)} 个 (断点续跑: 已有 {len(out)})")
    t0 = time.time()
    n_done = 0
    with mp.Pool(min(8, max(1, len(todo))), initializer=_init_worker,
                 initargs=(_G,)) as pool:
        for name, row in pool.imap_unordered(_worker_pools, todo, chunksize=1):
            out[name] = row
            save_json(out_fp, out)
            n_done += 1
            print(f"[pools {n_done}/{len(todo)}] {name}: "
                  f"{sum(1 for v in row.values() if isinstance(v, dict) and v.get('status') == 'ok')} 池 OK "
                  f"(ETA {(time.time()-t0)/n_done*(len(todo)-n_done)/60:.0f}min)",
                  flush=True)

    print(f"[pools] 完成, {len(out)} 公式; OUT {out_fp}")


# ---------------------------------------------------------------- 多进程

_PG = {}


def _init_worker(g):
    global _PG
    _PG = g
    import os as _os
    _os.environ.setdefault("VERA_KLINE_READONLY", "1")


def _worker_pools(rec):
    import os as _os
    import pandas as _pd
    _os.environ.setdefault("VERA_KLINE_READONLY", "1")
    g = _PG
    bench = _pd.read_parquet(g["bench_fp"])
    calendar = _pd.DatetimeIndex(g["calendar"])
    name = rec["name"]
    t0 = time.time()
    try:
        f = read_formula_txt(GS_DIR / rec["file"])
        sig_dates = calendar[calendar >= _pd.Timestamp("20040101")]
        signal, idx, used = run_formula_batch(
            f["source"], f["params"], g["codes"],
            start="20040101", end="20260825", chunk_size=400,
            fixed_index=sig_dates)
        code_pos = {c: j for j, c in enumerate(used)}
        row = {}
        for pk, members in g["pools"].items():
            cols = [code_pos[c] for c in members if c in code_pos]
            if not cols:
                continue
            sig_p = signal[:, cols]
            sel = filter_first_signal(
                signal_to_selections(
                    sig_p, idx, [used[j] for j in cols], name, *S2_RANGE),
                calendar, 30)
            if not len(sel):
                row[pk] = {"status": "no_signals"}
                continue
            m, trades = run_backtest(sel, *S2_RANGE)
            if not m:
                row[pk] = {"status": "no_metrics"}
                continue
            row[pk] = {
                "status": "ok",
                "metrics": {k: (float(v) if isinstance(v, (int, float)) else v)
                            for k, v in m.items()
                            if isinstance(v, (int, float))},
                "n_trades": int(len(trades)) if trades is not None else 0,
                "n_signals": int(len(sel)),
            }
        return name, row
    except Exception as e:
        return name, {"status": "error", "error": str(e)[:200]}


if __name__ == "__main__":
    main()
