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
拉取无数据 (上市前/已退市) → 记 no_data, 重跑不再尝试 (--retry-no-data 强制)。

失败治理 (2026-09-05 体检 P1, 判定逻辑在 utils/kline_backfill_policy.py):
  - 停滞 vs 无数据: 有旧缓存的公司延伸拉取没进展 ≠ "整段无数据" —— 原先
    manifest 存在即算成功, 永不进 no_data, 每轮调度刷新重复打同一批失败
    (refresh.log 7250 条 ERROR 主因)。现延伸无进展记 stalled {code: ts},
    冷却 72h 内跳过, 到期自动再试; 无记录却拉空仍走 no_data。
  - 熔断: 最近 80 次有判定里失败占比 ≥60% (源站整体故障, 如 8/26 一轮 5303
    次) → 提前中止, 不硬刷, 不把"源站故障"误记成各股停滞。
  - 锁心跳: 由 scheduler/maintenance 启动时, 每段进度 touch refresh.lock
    mtime —— 补拉是子进程, 原锁只有 4h TTL 且从不续命, 长任务会过期被
    误收尸导致双补拉 (见 kline_cache_maintenance._lock_held)。

用法:
    python tools/backfill_kline_cache.py --period 1d --start 20150101
    python tools/backfill_kline_cache.py --period 5m --start 20240627
    python tools/backfill_kline_cache.py --period 1d --start 20150101 --limit 20
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from core.kline_cache import KlineCache
from utils.kline_backfill_policy import (
    BREAK_FAIL_RATIO,
    BREAK_MIN_TRIED,
    breaker_tripped,
    manifest_advanced,
    stall_due,
)

# 由 scheduler/maintenance 启动时它先写好的锁; 直接 CLI 跑可能不存在 → 跳过心跳
_LOCK = (Path(__file__).resolve().parent.parent
         / "data" / "kline_cache" / "refresh.lock")


def _heartbeat_lock() -> None:
    """续命 refresh.lock mtime (存在才 touch): 让长补拉不会被 TTL 误收尸。"""
    try:
        if _LOCK.exists():
            now = time.time()
            os.utime(_LOCK, (now, now))
    except OSError:
        pass  # 心跳失败不影响回填本身


def _build_cache() -> KlineCache:
    cache_dir = str(Path(__file__).resolve().parent.parent / "data" / "kline_cache")

    def _tdx_fetcher(sl, s, e, period="1d", dividend_type="front"):
        return DataFetcher._get_kline_from_tdx(sl, s, e, period=period,
                                               dividend_type=dividend_type, fill_data=False)

    def _calendar_fetcher():
        return DataFetcher.get_calendar_days("SH", "20100101", "20991231")

    return KlineCache(cache_dir, tdx_fetcher=_tdx_fetcher,
                      calendar_fetcher=_calendar_fetcher)


def main() -> int:
    ap = argparse.ArgumentParser(description="K线缓存回填 (补前段, 跳过已有)")
    ap.add_argument("--period", required=True, choices=["1d", "5m", "1m"])
    ap.add_argument("--start", required=True, help="回填目标起点 YYYYMMDD")
    ap.add_argument("--end", default="", help="结束 YYYYMMDD (默认今天)")
    ap.add_argument("--universe", default="5", help="股票池 list_type (默认 5=全部A股)")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只 (测试)")
    ap.add_argument("--retry-no-data", action="store_true",
                    help="重试上次拉取无数据/停滞的股票 (默认跳过)")
    args = ap.parse_args()

    start_ts = pd.Timestamp(args.start)
    end_ts = pd.Timestamp(args.end) if args.end else pd.Timestamp.now().normalize()
    cache = _build_cache()
    state_path = Path(cache.cache_dir) / f"backfill_state_{args.period}_{args.start}.json"
    state = {"no_data": []}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    no_data = set(state.get("no_data", []))
    stalled: dict[str, dict] = dict(state.get("stalled", {}))

    TdxConnector.initialize()
    aborted = False
    try:
        stocks = DataFetcher.get_stock_universe(args.universe)
        if args.limit:
            stocks = stocks[:args.limit]
        total = len(stocks)
        print(f"[回填] {total} 只股 | {args.period} | 目标 {start_ts.date()}~{end_ts.date()}"
              f" | no_data={len(no_data)} stalled={len(stalled)}"
              + (" (--retry-no-data 全量重试)" if args.retry_no_data else ""),
              flush=True)

        n_skip = n_front = n_tail = n_full = n_nodata = 0
        n_stall_new = n_stall_skip = 0
        window: deque = deque(maxlen=BREAK_MIN_TRIED)  # True=停滞(源站疑似故障)
        new_stalled: dict[str, dict] = {}   # 本轮新增停滞 (熔断中止时回滚)
        t0 = time.time()

        def _persist() -> None:
            state_path.write_text(
                json.dumps({"no_data": sorted(no_data),
                            "stalled": dict(stalled)}, ensure_ascii=False),
                encoding="utf-8")

        for i, code in enumerate(stocks, 1):
            if code in no_data and not args.retry_no_data:
                n_nodata += 1
                continue
            if not args.retry_no_data and not stall_due(stalled.get(code)):
                n_stall_skip += 1
                continue

            rec_before = cache._manifest_get(code, args.period)
            fetches = []
            if rec_before is None:
                fetches.append((start_ts, end_ts))
                n_full += 1
            else:
                first_d, last_d = pd.Timestamp(rec_before[0]), pd.Timestamp(rec_before[1])
                if first_d > start_ts:
                    fetches.append((start_ts, first_d))
                    n_front += 1
                if last_d < end_ts:
                    fetches.append((last_d, end_ts))
                    n_tail += 1
            if not fetches:
                n_skip += 1
                continue

            last_err = ""
            for fs, fe in fetches:
                try:
                    cache._fetch_and_store(code, args.period, fs, fe,
                                           dividend_type="front")
                except Exception as e:
                    last_err = f"{type(e).__name__}: {e}"
            rec_after = cache._manifest_get(code, args.period)

            if manifest_advanced(rec_before, rec_after):
                window.append(False)  # 有进展 = 成功 (熔断样本)
            elif rec_before is None:
                # 无记录且拉空: 上市前/已退市/数据源确无 → 记 no_data, 不视为故障
                no_data.add(code)
                n_nodata += 1
            else:
                # 有旧缓存但延伸无进展 → 停滞 (区别于 no_data: 到期再试)
                err_note = last_err or "空返回(无进展)"
                stalled[code] = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
                                 "err": err_note[:200]}
                new_stalled[code] = stalled[code]
                n_stall_new += 1
                window.append(True)

            if i % 100 == 0 or i == total:
                _heartbeat_lock()
                el = time.time() - t0
                print(f"[回填] {i}/{total} ({i * 100 // total}%) "
                      f"跳过{n_skip} 前段{n_front} 尾段{n_tail} 全段{n_full} "
                      f"无数据{n_nodata} 停滞新{n_stall_new} 停滞跳{n_stall_skip} "
                      f"| {el:.0f}s", flush=True)
                _persist()  # 定期落状态 (中断不丢)

            if breaker_tripped(list(window)):
                aborted = True
                el = time.time() - t0
                print(f"[回填] 熔断中止: 最近 {len(window)} 次停滞 ≥"
                      f"{int(BREAK_FAIL_RATIO * 100)}% (疑似源站整体故障), "
                      f"处理到 {i}/{total}, 耗时 {el:.0f}s。"
                      f"本轮新增停滞 {len(new_stalled)} 条已回滚, 不污染状态。",
                      flush=True)
                break

        if not aborted:
            _persist()
            el = time.time() - t0
            print(f"[回填] 完成: 跳过{n_skip} 前段{n_front} 尾段{n_tail} 全段{n_full} "
                  f"无数据{n_nodata} 停滞新{n_stall_new} 停滞跳{n_stall_skip} "
                  f"| 总耗时 {el:.0f}s", flush=True)
        else:
            # 熔断: 回滚本轮新增停滞, 保留 no_data (空区间是事实), 只写稳定状态
            for c in new_stalled:
                stalled.pop(c, None)
            state_path.write_text(
                json.dumps({"no_data": sorted(no_data),
                            "stalled": dict(stalled)}, ensure_ascii=False),
                encoding="utf-8")
        return 0
    finally:
        TdxConnector.close()


if __name__ == "__main__":
    sys.exit(main())
