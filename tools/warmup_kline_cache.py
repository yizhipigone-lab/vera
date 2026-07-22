# -*- coding: utf-8 -*-
"""全 A K线缓存预热工具 (Phase 4)。

一次性把股票池的 K 线全搬进本地缓存 (data/kline_cache/), 之后回测读本地。
可恢复: 缓存 miss-fetch 天然断点续传 — 中断后重跑, 已缓存的股直接跳过。

用法:
    python tools/warmup_kline_cache.py                        # 全部A股 1d 全历史
    python tools/warmup_kline_cache.py --period 5m            # 5m (数小时)
    python tools/warmup_kline_cache.py --limit 20             # 只预热前 20 只 (测试)
    python tools/warmup_kline_cache.py --universe 50          # 沪深A股 (默认 5=全部A股)
    python tools/warmup_kline_cache.py --start 20240101 --end 20260717
"""
import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.connector import TdxConnector
from core.data_fetcher import DataFetcher


def main():
    parser = argparse.ArgumentParser(description="全 A K线缓存预热 (可恢复)")
    parser.add_argument("--period", default="1d", choices=["1d", "5m"],
                        help="K线周期 (默认 1d; 5m 耗时数小时)")
    parser.add_argument("--start", default="20200101", help="起始 YYYYMMDD (默认 20200101)")
    parser.add_argument("--end", default="", help="结束 YYYYMMDD (默认今天)")
    parser.add_argument("--universe", default="5",
                        help="股票池 list_type: 5=全部A股(默认), 50=沪深A股, 23=沪深300")
    parser.add_argument("--limit", type=int, default=0, help="只预热前 N 只 (测试用)")
    parser.add_argument("--batch", type=int, default=100, help="每批只数 (默认 100)")
    args = parser.parse_args()

    # 防系统休眠 (预热数小时, 跑期间不休眠). 2026-07-19
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    except Exception:
        pass

    end = args.end or time.strftime("%Y%m%d")
    TdxConnector.initialize()
    try:
        stocks = DataFetcher.get_stock_universe(args.universe)
        if args.limit:
            stocks = stocks[:args.limit]
        total = len(stocks)
        print(f"[预热] {total} 只股 | period={args.period} | {args.start}~{end} | universe={args.universe}")
        t0 = time.time()
        for i in range(0, total, args.batch):
            chunk = stocks[i:i + args.batch]
            DataFetcher.get_kline(chunk, args.start, end, period=args.period,
                                  dividend_type="front", fill_data=False, use_cache=True)
            done = min(i + args.batch, total)
            print(f"[预热] {done}/{total} ({done * 100 // total}%) 耗时 {time.time() - t0:.0f}s",
                  flush=True)
        print(f"[预热] 完成, 总耗时 {time.time() - t0:.0f}s")
    finally:
        TdxConnector.close()


if __name__ == "__main__":
    main()
