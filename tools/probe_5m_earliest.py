# -*- coding: utf-8 -*-
"""探测 TDX 5m 数据最早可用日期 (2026-08-02)。

方法: 选几只上市很久的老股 (平安银行/浦发银行/万科/上证大盘),
分段请求很早的区间 (2005~2015), 看 TDX 实际返回的最早 bar 是哪一天。
若 2005 起点返回的最早 bar 晚于请求起点, 说明 TDX 服务端只存到那天。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from core.connector import TdxConnector
from core.data_fetcher import DataFetcher

PROBES = ["000001.SZ", "600000.SH", "000002.SZ", "600036.SH", "999999.SH"]


def earliest_bar(code: str, start: str, end: str) -> pd.Timestamp | None:
    data = DataFetcher._get_kline_from_tdx(
        [code], start, end, period="5m", dividend_type="none", fill_data=False)
    if not data or "Close" not in data or data["Close"].empty:
        return None
    idx = pd.to_datetime(data["Close"].index)
    return idx.min()


def main():
    TdxConnector.initialize()
    try:
        # 第一轮: 整个 2005~2015 大区间, 看最早 bar
        print("=== 探测1: 请求 20050101~20150101 ===", flush=True)
        for code in PROBES:
            eb = earliest_bar(code, "20050101", "20150101")
            print(f"  {code}: 最早 bar = {eb}", flush=True)

        # 第二轮: 若最早 bar 在 2014~2015, 再按月细分确认拐点
        print("=== 探测2: 逐半年细分 2013~2016 ===", flush=True)
        halves = [("20130101", "20130630"), ("20130701", "20131231"),
                  ("20140101", "20140630"), ("20140701", "20141231"),
                  ("20150101", "20150630"), ("20150701", "20151231"),
                  ("20160101", "20160630")]
        code = "000001.SZ"
        for s, e in halves:
            eb = earliest_bar(code, s, e)
            print(f"  {code} {s}~{e}: 最早 bar = {eb}", flush=True)
    finally:
        TdxConnector.close()


if __name__ == "__main__":
    main()
