# -*- coding: utf-8 -*-
"""K线缓存回填工具 (2026-07-18): 补齐前段缺失, 跳过已有数据。

与 tools/warmup_kline_cache.py 的区别: warmup 走 cache.get → _ensure,
_ensure 对"请求起点早于缓存起点"只告警不拉取 (F4 设计), 无法回填前段。
本工具直接调 cache._fetch_and_store (维护工具直调私有 API, 故意为之),
_write_merge 合并去重, 已缓存部分零重拉。

每只股票决策:
  - 无缓存记录           → 拉 [start, end] 全段
  - first_date > start   → 只拉 [start, first_date] 前段 (上市日晚于 start 的股
                           TDX 自然只返回上市后的, 不浪费)
  - last_date < end      → 只拉 [last_date, end] 尾段
  - 已覆盖               → 跳过
拉取无数据 (上市前/已退市) → 记状态文件, 重跑不再尝试 (--retry-no-data 强制重试)。

用法:
    python tools/backfill_kline_cache.py --period 1d --start 20150101
    python tools/backfill_kline_cache.py --period 5m --start 20240627
    python tools/backfill_kline_cache.py --period 1d --start 20150101 --limit 20
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from core.kline_cache import KlineCache


def _build_cache() -> KlineCache:
    cache_dir = str(Path(__file__).resolve().parent.parent / "data" / "kline_cache")

    def _tdx_fetcher(sl, s, e, period="1d", dividend_type="front"):
        return DataFetcher._get_kline_from_tdx(sl, s, e, period=period,
                                               dividend_type=dividend_type, fill_data=False)

    def _calendar_fetcher():
        return DataFetcher.get_trading_dates("SH", "20100101", "20991231")

    return KlineCache(cache_dir, tdx_fetcher=_tdx_fetcher,
                      calendar_fetcher=_calendar_fetcher)


def main() -> int:
    ap = argparse.ArgumentParser(description="K线缓存回填 (补前段, 跳过已有)")
    ap.add_argument("--period", required=True, choices=["1d", "5m"])
    ap.add_argument("--start", required=True, help="回填目标起点 YYYYMMDD")
    ap.add_argument("--end", default="", help="结束 YYYYMMDD (默认今天)")
    ap.add_argument("--universe", default="5", help="股票池 list_type (默认 5=全部A股)")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只 (测试)")
    ap.add_argument("--retry-no-data", action="store_true",
                    help="重试上次拉取无数据的股票 (默认跳过)")
    args = ap.parse_args()

    start_ts = pd.Timestamp(args.start)
    end_ts = pd.Timestamp(args.end) if args.end else pd.Timestamp.now().normalize()
    cache = _build_cache()
    state_path = Path(cache.cache_dir) / f"backfill_state_{args.period}_{args.start}.json"
    state = {"no_data": []}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    no_data = set(state.get("no_data", []))

    TdxConnector.initialize()
    try:
        stocks = DataFetcher.get_stock_universe(args.universe)
        if args.limit:
            stocks = stocks[:args.limit]
        total = len(stocks)
        print(f"[回填] {total} 只股 | {args.period} | 目标 {start_ts.date()}~{end_ts.date()}",
              flush=True)

        n_skip = n_front = n_tail = n_full = n_nodata = 0
        t0 = time.time()
        for i, code in enumerate(stocks, 1):
            if code in no_data and not args.retry_no_data:
                n_nodata += 1
                continue
            rec = cache._manifest_get(code, args.period)
            fetches = []
            if rec is None:
                fetches.append((start_ts, end_ts))
                n_full += 1
            else:
                first_d, last_d = pd.Timestamp(rec[0]), pd.Timestamp(rec[1])
                if first_d > start_ts:
                    fetches.append((start_ts, first_d))
                    n_front += 1
                if last_d < end_ts:
                    fetches.append((last_d, end_ts))
                    n_tail += 1
            if not fetches:
                n_skip += 1
                continue

            got_data = False
            for fs, fe in fetches:
                try:
                    cache._fetch_and_store(code, args.period, fs, fe,
                                           dividend_type="front")
                except Exception as e:
                    print(f"[回填] {code} 拉取异常 {fs.date()}~{fe.date()}: {e}",
                          flush=True)
                rec2 = cache._manifest_get(code, args.period)
                if rec2 is not None:
                    got_data = True
            if not got_data:
                # 拉不到: 上市前/已退市/数据源缺 → 记状态, 重跑跳过
                no_data.add(code)
                n_nodata += 1

            if i % 100 == 0 or i == total:
                el = time.time() - t0
                print(f"[回填] {i}/{total} ({i * 100 // total}%) "
                      f"跳过{n_skip} 前段{n_front} 尾段{n_tail} 全段{n_full} "
                      f"无数据{n_nodata} | {el:.0f}s", flush=True)
                # 定期落状态文件 (中断不丢)
                state_path.write_text(
                    json.dumps({"no_data": sorted(no_data)}, ensure_ascii=False),
                    encoding="utf-8")

        state_path.write_text(
            json.dumps({"no_data": sorted(no_data)}, ensure_ascii=False),
            encoding="utf-8")
        el = time.time() - t0
        print(f"[回填] 完成: 跳过{n_skip} 前段{n_front} 尾段{n_tail} 全段{n_full} "
              f"无数据{n_nodata} | 总耗时 {el:.0f}s", flush=True)
        return 0
    finally:
        TdxConnector.close()


if __name__ == "__main__":
    sys.exit(main())
