# -*- coding: utf-8 -*-
"""冷缓存字节级 parity 验证 (计划书 2026-07-18 Task 4, 一次性, 不进回归)。

对同一选股集合, use_cache=False (TDX 直拉) vs use_cache=True (KlineCache)
跑 get_kline_windowed, 先断言 index/columns 完全相等再比值 (审计 M2:
reindex_like 会静默丢差异, 禁止用)。

用法: python tools/verify_5m_cache_parity.py [--codes 000001.SZ,600000.SH,002008.SZ] [--date 20260623]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from core.connector import TdxConnector
from core.data_fetcher import DataFetcher


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="000001.SZ,600000.SH,002008.SZ")
    ap.add_argument("--date", default="20260623", help="信号日 YYYYMMDD")
    args = ap.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    selections = pd.DataFrame(
        {"stock_code": codes, "select_date": [pd.Timestamp(args.date)] * len(codes)})

    TdxConnector.initialize()
    try:
        k_tdx, _ = DataFetcher.get_kline_windowed(
            selections, period="5m", dividend_type="front", fill_data=False,
            use_cache=False)
        k_cache, _ = DataFetcher.get_kline_windowed(
            selections, period="5m", dividend_type="front", fill_data=False,
            use_cache=True)
    finally:
        TdxConnector.close()

    ok_all = True
    for field in ("Open", "High", "Low", "Close", "Volume", "Amount"):
        if field not in k_tdx and field not in k_cache:
            continue
        ct, cc = k_tdx[field], k_cache[field]
        if not ct.index.equals(cc.index):
            diff = ct.index.symmetric_difference(cc.index)
            print(f"[parity] FAIL {field}: index 不一致 (差 {len(diff)} 个, 首 {list(diff[:3])})")
            ok_all = False
            continue
        if not ct.columns.equals(cc.columns):
            print(f"[parity] FAIL {field}: columns 不一致 {set(ct.columns) ^ set(cc.columns)}")
            ok_all = False
            continue
        a = ct.values.astype(np.float64)
        b = cc.values.astype(np.float64)
        ok = np.allclose(a, b, equal_nan=True, atol=1e-9)
        n_diff = int((~np.isclose(a, b, equal_nan=True, atol=1e-9)).sum())
        print(f"[parity] {field}: shape={a.shape} {'PASS' if ok else f'FAIL ({n_diff} 值不同)'}")
        ok_all = ok_all and ok

    print(f"[parity] 总结: {'PASS' if ok_all else 'FAIL'}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
