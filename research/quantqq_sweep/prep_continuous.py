"""research/quantqq_sweep/prep_continuous.py — 连续区间 5m 矩阵直填构建器 (2026-08-15)

背景: get_kline_windowed 的 concat 合并中间产物是终态的 ~桶数倍 (2015-2026
整段 ≈300GB), 64G 机器不可行; 分段口径破坏了复利连续性 (用户拍板要连续曲线)。

本构建器绕开窗口合并: 预分配 mmap 矩阵, 逐只票读本地 5m parquet 缓存
直接切片写入 —— 内存占用恒定 (一次一只票), 与区间长度无关。

口径: 与 quantqq_5m_sweep_2010.py 完全一致 (QUANTQQ/沪深A股/前复权/
非标准 bar 过滤/涨停过滤/条件单语义在 run 阶段), 区间 2015-01-01~2026-06-30。

产出: output/quantqq_5m_sweep_2010/continuous/cache/{close,high,low,open,
entries,tradable,last_tradable_idx}.npy + meta.json + selections.csv
—— 结构与 sweep 的 _load_cache 完全同构, run 阶段直接复用。

用法: python research/quantqq_sweep/prep_continuous.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

START = "20150101"
END = "20260630"
OUT = Path("output/quantqq_5m_sweep_2010/continuous/cache")
KLINE_5M = Path("data/kline_cache/5m")


def main() -> int:
    from backtest._constants import STD_BAR_TIMES
    from backtest.engine import (ENGINE_VERSION, BacktestEngine,
                                 _build_tradable_from_raw,
                                 recompute_last_tradable_idx)

    OUT.mkdir(parents=True, exist_ok=True)
    os.environ["VERA_KLINE_READONLY"] = "1"   # 防御: 不触网

    # 1. 选股: 复用已生成的全段 CSV, 截到 2015 起 (用户最新口径)
    sel_all = pd.read_csv("output/quantqq_5m_sweep_2010/cache/selections.csv",
                          dtype={"stock_code": str})
    selections = sel_all[sel_all["select_date"].astype(str) >= START].copy()
    selections.to_csv(OUT / "selections.csv", index=False)
    codes = sorted(selections["stock_code"].unique())
    print(f"信号 {len(selections)} 条, 股票 {len(codes)} 只", flush=True)

    # 2. 行索引: 交易日(000001 1d 缓存日期) × 48 个标准 5m bar 时刻
    d1 = pd.read_parquet("data/kline_cache/1d/000001.SZ.parquet",
                         columns=["date"])
    days = pd.to_datetime(d1["date"], format="%Y%m%d")
    days = days[(days >= START) & (days <= END)]
    bar_times = sorted(STD_BAR_TIMES["5m"])
    stamps = pd.DatetimeIndex(
        [pd.Timestamp(f"{d.strftime('%Y-%m-%d')} {t}") for d in days
         for t in bar_times])
    row_of = {t: i for i, t in enumerate(stamps)}
    print(f"行网格: {len(days)} 交易日 × {len(bar_times)} bar = {len(stamps)} 行",
          flush=True)

    # 3. 预分配 mmap 矩阵 (close/high/low/open=float64 NaN 初始化)
    n_r, n_c = len(stamps), len(codes)
    paths = {}
    arrs = {}
    for f in ("close", "high", "low", "open"):
        p = OUT / f"{f}.npy"
        paths[f] = p
        m = np.lib.format.open_memmap(p, mode="w+",
                                      dtype=np.float64, shape=(n_r, n_c))
        m[:] = np.nan
        arrs[f] = m
    print(f"矩阵预分配: {n_r}×{n_c} ×4 ≈ {4 * n_r * n_c * 8 / 1e9:.1f}GB",
          flush=True)

    # 4. 逐只票直填 (内存恒定)
    t0 = time.time()
    n_ok = n_miss = 0
    std = STD_BAR_TIMES["5m"]
    for j, code in enumerate(codes):
        pf = KLINE_5M / f"{code}.parquet"
        if not pf.exists():
            n_miss += 1
            continue
        try:
            df = pd.read_parquet(pf)
        except Exception:
            n_miss += 1
            continue
        ts = pd.to_datetime(df.pop("date"))
        mask = (ts >= stamps[0]) & (ts <= stamps[-1])
        df, ts = df[mask], ts[mask]
        # 非标准时刻 bar 剔除 (与 engine 口径一致)
        keep = ts.dt.strftime("%H:%M").isin(std)
        df, ts = df[keep], ts[keep]
        if len(df) == 0:
            n_miss += 1
            continue
        rows = ts.map(row_of).to_numpy()
        valid = ~np.isnan(rows.astype(np.float64))
        rows = rows[valid].astype(np.int64)
        arrs["close"][rows, j] = df["close"].to_numpy()[valid]
        arrs["high"][rows, j] = df["high"].to_numpy()[valid]
        arrs["low"][rows, j] = df["low"].to_numpy()[valid]
        arrs["open"][rows, j] = df["open"].to_numpy()[valid]
        n_ok += 1
        if (j + 1) % 500 == 0:
            el = time.time() - t0
            print(f"  {j + 1}/{n_c} ({el:.0f}s, ~{el / (j + 1) * (n_c - j - 1):.0f}s 剩)",
                  flush=True)
    for m in arrs.values():
        m.flush()
    print(f"直填完成: 成功 {n_ok}, 无数据 {n_miss}, {time.time() - t0:.0f}s",
          flush=True)

    # 5. entries / tradable (复用 engine helper, 口径与 run 完全一致)
    engine = BacktestEngine({"initial_capital": 1_000_000.0, "period": "5m",
                             "commission": 0.0003, "slippage": 0.001,
                             "stamp_tax": 0.0005, "enable_realistic_costs": True})
    close = pd.DataFrame(np.load(paths["close"], mmap_mode="r"),
                         index=stamps, columns=codes)
    entries = engine._build_entry_signals(selections, close)
    entries = entries.reindex(index=stamps, columns=codes, fill_value=False)
    close_raw = close
    close_ff = close.ffill()
    entries = engine._filter_limit_up(entries, close_ff)  # 涨停过滤 (stop 无关)
    tradable_np, _ = _build_tradable_from_raw(close_raw, close_ff)
    last_tradable_idx = recompute_last_tradable_idx(tradable_np)

    np.save(OUT / "entries.npy", entries.values.astype(bool))
    np.save(OUT / "tradable.npy", tradable_np.astype(bool))
    np.save(OUT / "last_tradable_idx.npy",
            np.asarray(last_tradable_idx, dtype=np.int64))
    meta = {"index": [str(t) for t in stamps], "columns": codes,
            "start": START, "end": END, "formula": "QUANTQQ",
            "period": "5m", "continuous": True,
            "engine_version": ENGINE_VERSION,
            "n_signals": int(entries.values.sum()), "shape": [n_r, n_c]}
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False),
                                   encoding="utf-8")
    print(json.dumps({"status": "ok", "shape": [n_r, n_c],
                      "signals": meta["n_signals"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
