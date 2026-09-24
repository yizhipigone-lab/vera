"""创业板50 vs 纳指 滚动相关性 —— 验证"2025年起是否同频" (2026-08-20)。

算两腿日收益的滚动 120 日相关系数, 按年分段看相关性怎么漂移。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np
import pandas as pd

from core.data_fetcher import DataFetcher

NAS = "513100.SH"


def main():
    kl = DataFetcher.get_kline([NAS, "159949.SZ"], "20130101", "20260819",
                               period="1d", dividend_type="front",
                               use_cache=True, force_refresh=True)
    close = kl["Close"]
    import akshare as ak
    idx = ak.stock_zh_index_daily_tx(symbol="sz399673")
    idx = idx.set_index("date")
    idx.index = pd.to_datetime(idx.index)
    nas_c = close[NAS].dropna()
    dfc = pd.concat([idx["close"].rename("cyb"), nas_c], axis=1, join="inner").dropna()

    ret = dfc.pct_change().dropna()
    roll = ret["cyb"].rolling(120).corr(ret[NAS])

    print("=== 分年段平均相关性 (日收益, 120日滚动) ===")
    bins = [("2014-2016", "2014", "2016"), ("2017-2019", "2017", "2019"),
            ("2020-2021", "2020", "2021"), ("2022-2023", "2022", "2023"),
            ("2024", "2024", "2024"), ("2025-至今", "2025", "2026")]
    for label, y0, y1 in bins:
        seg = roll[(roll.index.year >= int(y0)) & (roll.index.year <= int(y1))]
        if len(seg):
            print(f"  {label:<12} 均值 {seg.mean():+.3f}  范围 [{seg.min():+.2f}, {seg.max():+.2f}]")

    print("\n=== 2025 年至今, 逐月滚动相关 ===")
    recent = roll[roll.index >= "2025-01-01"]
    monthly = recent.resample("ME").last()
    for d, v in monthly.items():
        print(f"  {d.strftime('%Y-%m')}: {v:+.3f}")

    print(f"\n最新 120 日相关: {roll.iloc[-1]:+.3f}")


if __name__ == "__main__":
    main()
