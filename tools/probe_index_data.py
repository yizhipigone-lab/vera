# -*- coding: utf-8 -*-
"""一次性只读探针: 验证 TDX 直拉指数 vs A股 的最后日期, 定位 9.30 后指数缺失真因。

不改任何数据, 不走 parquet 缓存 (use_cache=False 全程), 纯诊断。
跑完可删。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.connector import TdxConnector
from core.data_fetcher import DataFetcher


def main():
    today = time.strftime("%Y%m%d")
    print(f"[probe] today={today} start=20250101 use_cache=False (TDX 直拉)")
    TdxConnector.initialize()
    try:
        print("---- 指数 (dividend_type=none) ----")
        for name, label in [("shanghai", "上证指数"),
                            ("chuangyeban", "创业板指"),
                            ("hs300", "沪深300")]:
            try:
                df = DataFetcher.get_index_data(
                    name, "20250101", today,
                    dividend_type="none", period="1d")
                if df is None or df.empty:
                    print(f"  [{label}] EMPTY")
                else:
                    print(f"  [{label}] rows={len(df)} "
                          f"first={df.index[0].date()} last={df.index[-1].date()}")
            except Exception as e:
                print(f"  [{label}] ERROR: {e}")

        print("---- A股对比 (dividend_type=front) ----")
        for code in ["000001.SZ", "600000.SH"]:
            try:
                df = DataFetcher.get_kline_single(
                    code, "20250101", today,
                    dividend_type="front", period="1d")
                if df is None or df.empty:
                    print(f"  [{code}] EMPTY")
                else:
                    print(f"  [{code}] rows={len(df)} "
                          f"first={df.index[0].date()} last={df.index[-1].date()}")
            except Exception as e:
                print(f"  [{code}] ERROR: {e}")
    finally:
        TdxConnector.close()


if __name__ == "__main__":
    main()
