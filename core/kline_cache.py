# -*- coding: utf-8 -*-
"""本地 K 线 parquet 缓存 (Phase 1: 1d + gap 检测).

回测优先读本地 parquet, miss-fetch 增量补 TDX, 1d 缺日检测 + 自动补拉 + 告警。
治 002008 类"运行时 TDX 临时缺数据"问题 —— 数据完整性在缓存层兜底。

设计见 docs/plan/2026-07-17_本地K线parquet缓存_计划书.md。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from utils.logger import get_logger

logger = get_logger(__name__)

_FIELDS = ["Open", "High", "Low", "Close", "Volume", "Amount"]
_FIELD_LOWER = {"Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume", "Amount": "amount"}


class KlineCache:
    """per-stock parquet + sqlite manifest (WAL) 的 K 线缓存。

    tdx_fetcher: callable(stock_list, start, end, period, dividend_type) -> dict[str, DataFrame]
        (与 DataFetcher.get_kline 同结构), 用于 miss-fetch。
    calendar_fetcher: callable() -> list[str YYYYMMDD], 交易日历源。
    """

    MANIFEST_COLUMNS = ["stock_code", "period", "first_date", "last_date",
                        "last_close", "rows", "fetched_at", "intact"]

    def __init__(self, cache_dir, tdx_fetcher: Callable, calendar_fetcher: Callable):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "calendar").mkdir(exist_ok=True)
        (self.cache_dir / "1d").mkdir(exist_ok=True)
        (self.cache_dir / "5m").mkdir(exist_ok=True)
        self.db_path = self.cache_dir / "manifest.db"
        self.tdx_fetcher = tdx_fetcher
        self.calendar_fetcher = calendar_fetcher
        self._lock = threading.Lock()
        self._init_db()

    # ───────────────────── sqlite manifest ─────────────────────

    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS manifest(
                stock_code TEXT, period TEXT, first_date TEXT, last_date TEXT,
                last_close REAL, rows INTEGER, fetched_at TEXT, intact INTEGER,
                PRIMARY KEY(stock_code, period))""")

    def _manifest_all(self) -> List[tuple]:
        with self._conn() as c:
            return c.execute(
                "SELECT stock_code, period, first_date, last_date, last_close, "
                "rows, fetched_at, intact FROM manifest"
            ).fetchall()

    def _manifest_get(self, code: str, period: str) -> Optional[tuple]:
        with self._conn() as c:
            return c.execute(
                "SELECT first_date, last_date, last_close, intact FROM manifest "
                "WHERE stock_code=? AND period=?", (code, period)
            ).fetchone()

    def _manifest_upsert(self, code: str, period: str, first_date: str, last_date: str,
                         last_close: Optional[float], rows: int, intact: bool):
        with self._conn() as c:
            c.execute(
                """INSERT INTO manifest(stock_code, period, first_date, last_date,
                   last_close, rows, fetched_at, intact)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(stock_code, period) DO UPDATE SET
                   first_date=excluded.first_date, last_date=excluded.last_date,
                   last_close=excluded.last_close, rows=excluded.rows,
                   fetched_at=excluded.fetched_at, intact=excluded.intact""",
                (code, period, first_date, last_date, last_close, rows,
                 datetime.now().isoformat(), 1 if intact else 0))

    # ───────────────────── trading calendar ─────────────────────

    def _calendar_path(self) -> Path:
        return self.cache_dir / "calendar" / "trading_days.parquet"

    def _get_calendar(self) -> set:
        """返回交易日集合 (str YYYYMMDD)。命中 parquet 直接读, 否则拉取落盘。"""
        p = self._calendar_path()
        if p.exists():
            df = pq.read_table(p).to_pandas()
            return set(df["date"].astype(str).tolist())
        dates = [str(d) for d in self.calendar_fetcher()]
        df = pd.DataFrame({"date": dates})
        tmp = p.with_suffix(".tmp")
        pq.write_table(pa.Table.from_pandas(df), tmp)
        os.replace(tmp, p)
        return set(dates)

    # ───────────────────── public: get ─────────────────────

    def get(self, stock_list, start, end, period="1d", dividend_type="front") -> Dict[str, pd.DataFrame]:
        """取 K 线, 返回 {Open,High,Low,Close,Volume,Amount: DataFrame}, 列=股票、行=date。
        与 DataFetcher.get_kline 返回结构一致。"""
        stock_list = list(stock_list)
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        for code in stock_list:
            self._ensure(code, period, start_ts, end_ts, dividend_type)
        # 读盘 + 拼宽表
        per_stock = {code: self._read_parquet(code, period, start_ts, end_ts)
                     for code in stock_list}
        result: Dict[str, pd.DataFrame] = {}
        all_dates = sorted(set().union(*(df.index for df in per_stock.values()))) if per_stock else []
        idx = pd.DatetimeIndex(all_dates)
        for field in _FIELDS:
            col = _FIELD_LOWER[field]
            data = {}
            for code, df in per_stock.items():
                if col in df.columns:
                    data[code] = df[col].reindex(idx)
            result[field] = pd.DataFrame(data, index=idx)
        return result

    # ───────────────────── ensure / fetch ─────────────────────

    def _ensure(self, code: str, period: str, start_ts: pd.Timestamp,
                end_ts: pd.Timestamp, dividend_type: str):
        """确保 [start, end] 已缓存; miss-fetch 增量; 1d gap 检测。"""
        rec = self._manifest_get(code, period)
        need_fetch: Optional[tuple] = None
        if rec is None:
            need_fetch = (start_ts, end_ts)
        else:
            first_d = pd.Timestamp(rec[0])
            last_d = pd.Timestamp(rec[1])
            intact = bool(rec[3])
            if not intact:
                need_fetch = (start_ts, end_ts)  # 之前不完整 → 全量重拉
            elif last_d < end_ts:
                need_fetch = (last_d + pd.Timedelta(days=1), end_ts)  # 增量
            # first_d > start_ts 的向后扩展 Phase 1 暂不处理 (rare)
        if need_fetch:
            with self._lock:
                self._fetch_and_store(code, period, need_fetch[0], need_fetch[1], dividend_type)
        if period == "1d":
            self._detect_and_fill_gaps_1d(code, period, start_ts, end_ts, dividend_type)

    def _fetch_and_store(self, code: str, period: str, fstart: pd.Timestamp,
                         fend: pd.Timestamp, dividend_type: str):
        raw = self.tdx_fetcher([code], fstart.strftime("%Y%m%d"),
                               fend.strftime("%Y%m%d"), period=period,
                               dividend_type=dividend_type)
        if not raw or "Close" not in raw or code not in raw["Close"].columns:
            logger.warning("kline_fetch_fail: %s %s [%s~%s] 拉取无数据",
                           code, period, fstart.date(), fend.date())
            return
        close_s = raw["Close"][code].dropna()
        if close_s.empty:
            return
        df = pd.DataFrame(index=close_s.index)
        for field in _FIELDS:
            col = _FIELD_LOWER[field]
            if field in raw and code in raw[field].columns:
                df[col] = raw[field][code]
            elif col == "close":
                df[col] = close_s
            else:
                df[col] = np.nan
        df.index.name = "date"
        self._write_merge(code, period, df)
        self._refresh_manifest(code, period)

    def _write_merge(self, code: str, period: str, new_df: pd.DataFrame):
        """合并写: 读旧 parquet (若有) → concat → 去重(keep last) → 排序 → 原子写。"""
        pfile = self._parquet_path(code, period)
        if pfile.exists():
            old = pq.read_table(pfile).to_pandas()
            old = old.set_index("date") if "date" in old.columns else old
            combined = pd.concat([old, new_df])
        else:
            combined = new_df
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        combined.index.name = "date"
        out = combined.reset_index()
        tmp = pfile.with_suffix(".parquet.tmp")
        pq.write_table(pa.Table.from_pandas(out, preserve_index=False), tmp)
        os.replace(tmp, pfile)

    def _refresh_manifest(self, code: str, period: str):
        """读 parquet 实际内容回填 manifest (first/last/rows/last_close/intact)。"""
        df = self._read_parquet(code, period, pd.Timestamp("1900-01-01"), pd.Timestamp("2099-12-31"))
        if df.empty:
            return
        intact = self._check_gaps_1d(code, period, df.index)
        first_d = df.index[0]
        last_d = df.index[-1]
        last_close = float(df["close"].iloc[-1]) if "close" in df.columns else None
        self._manifest_upsert(code, period, first_d.strftime("%Y%m%d"),
                              last_d.strftime("%Y%m%d"), last_close, len(df), intact)

    # ───────────────────── gap 检测 (1d) ─────────────────────

    def _check_gaps_1d(self, code: str, period: str, cached_dates: pd.DatetimeIndex) -> bool:
        """校验 cached_dates vs 交易日历 (在 cached 范围内)。返回 intact=True 无缺日。"""
        if period != "1d" or len(cached_dates) == 0:
            return True
        cal = self._get_calendar()
        cached_str = {d.strftime("%Y%m%d") for d in cached_dates}
        lo = cached_dates.min().strftime("%Y%m%d")
        hi = cached_dates.max().strftime("%Y%m%d")
        expected = {d for d in cal if lo <= d <= hi}
        gaps = expected - cached_str
        if gaps:
            logger.warning("kline_gap: %s %s 缺日 %s", code, period, sorted(gaps))
            return False
        return True

    def _detect_and_fill_gaps_1d(self, code: str, period: str, start_ts: pd.Timestamp,
                                 end_ts: pd.Timestamp, dividend_type: str):
        """对 [start, end] 做 gap 检测, 缺日 → 自动补拉, 仍缺 → intact=false 告警。"""
        df = self._read_parquet(code, period, start_ts, end_ts)
        if df.empty:
            return
        cal = self._get_calendar()
        cached_str = {d.strftime("%Y%m%d") for d in df.index}
        lo = max(df.index.min().strftime("%Y%m%d"), start_ts.strftime("%Y%m%d"))
        hi = min(df.index.max().strftime("%Y%m%d"), end_ts.strftime("%Y%m%d"))
        expected = {d for d in cal if lo <= d <= hi}
        gaps = sorted(expected - cached_str)
        if not gaps:
            return
        logger.warning("kline_gap: %s %s [%s~%s] 缺日 %s, 尝试补拉",
                       code, period, lo, hi, gaps)
        # 逐段补拉 (连续日合并)
        for seg_start, seg_end in self._contiguous_segments(gaps):
            self._fetch_and_store(code, period,
                                  pd.Timestamp(seg_start), pd.Timestamp(seg_end),
                                  dividend_type)
        # 复检
        df2 = self._read_parquet(code, period, start_ts, end_ts)
        cached_str2 = {d.strftime("%Y%m%d") for d in df2.index}
        still = sorted(expected - cached_str2)
        if still:
            logger.warning("kline_gap: %s %s 补拉后仍缺 %s, 标记 intact=false",
                           code, period, still)
            self._manifest_set_intact(code, period, False)
        else:
            self._manifest_set_intact(code, period, True)

    @staticmethod
    def _contiguous_segments(dates: List[str]) -> List[tuple]:
        """把连续日期 (YYYYMMDD) 合并成 (start, end) 段。"""
        if not dates:
            return []
        segs = []
        s = dates[0]
        prev = pd.Timestamp(s)
        for d in dates[1:]:
            cur = pd.Timestamp(d)
            if (cur - prev).days > 1:
                segs.append((s, prev.strftime("%Y%m%d")))
                s = d
            prev = cur
        segs.append((s, prev.strftime("%Y%m%d")))
        return segs

    def _manifest_set_intact(self, code: str, period: str, intact: bool):
        with self._conn() as c:
            c.execute("UPDATE manifest SET intact=? WHERE stock_code=? AND period=?",
                      (1 if intact else 0, code, period))

    # ───────────────────── read ─────────────────────

    def _parquet_path(self, code: str, period: str) -> Path:
        return self.cache_dir / period / f"{code}.parquet"

    def _read_parquet(self, code: str, period: str,
                      start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
        pfile = self._parquet_path(code, period)
        if not pfile.exists():
            return pd.DataFrame()
        df = pq.read_table(pfile).to_pandas()
        if "date" in df.columns:
            df = df.set_index("date")
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        return df.loc[(df.index >= start_ts) & (df.index <= end_ts)]
