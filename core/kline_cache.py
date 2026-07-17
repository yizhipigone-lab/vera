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
from utils.code_normalizer import normalize_list
from core.dividend_type import to_tdx_str
# 2026-07-18: 协作式停止 (web「停止回测」按钮)。批量脚本从不置位, 行为不变。
from core.stop_flag import raise_if_stopped

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
                last_full_refetch_at TEXT,
                PRIMARY KEY(stock_code, period))""")
            # 迁移: 旧库无 last_full_refetch_at 列时补上 (F5 冷却)
            try:
                c.execute("ALTER TABLE manifest ADD COLUMN last_full_refetch_at TEXT")
            except sqlite3.OperationalError:
                pass  # 列已存在

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
        # F1 [C1]: 只存前复权 (决策), 混合口径 fail-fast, 杜绝静默错数
        if to_tdx_str(dividend_type) != "front":
            raise ValueError(
                f"KlineCache 只支持前复权 (dividend_type=front), 收到 {dividend_type!r}. "
                f"如需其他复权口径请用 use_cache=False 直拉 TDX。"
            )
        # F3 [M2]: 归一化股票代码 (与 TDX 路径一致, 杜绝非标准代码静默消失 + 缓存碎片)
        stock_list = normalize_list(stock_list)
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        for code in stock_list:
            # 2026-07-18: 停止回测按钮 — 缓存 miss 逐只拉网是长耗时点, 逐只检查
            raise_if_stopped()
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
        """确保 [start, end] 已缓存; miss-fetch 增量; 1d gap 检测。

        F4 [M1] 向后扩展静默截断 → 告警
        F5 [H1] intact=false 全量重拉 24h 冷却 → 杜绝停牌 thrash
        F6 [H2] 增量含重叠 bar 比对 last_close → 前复权分红 shift 检测
        """
        rec = self._manifest_get(code, period)
        need_fetch: Optional[tuple] = None
        staleness_check: Optional[tuple] = None  # (last_d, old_last_close)
        is_full_fetch = False
        skip_gap_detection = False
        if rec is None:
            need_fetch = (start_ts, end_ts)
            is_full_fetch = True
        else:
            first_d = pd.Timestamp(rec[0])
            last_d = pd.Timestamp(rec[1])
            last_close = rec[2]
            intact = bool(rec[3])
            # F4: 请求起点早于缓存起点 → 告警 (前段缺失, 不静默截断)
            if first_d > start_ts:
                logger.warning(
                    "kline_truncated: %s %s 请求起点 %s 早于缓存起点 %s, 前段数据缺失 (向后扩展未拉取)",
                    code, period, start_ts.strftime("%Y-%m-%d"), first_d.strftime("%Y-%m-%d"))
            if not intact:
                # F5: 全量重拉冷却 (24h), 杜绝停牌股每次调用全量重拉死循环
                if self._refetch_cooled_down(code, period):
                    need_fetch = (start_ts, end_ts)
                    is_full_fetch = True
                else:
                    # 冷却期内: 用缓存现状, 连 gap 补拉也跳过 (不 thrash)
                    skip_gap_detection = True
            elif last_d < end_ts:
                # F6: 增量含重叠 bar (last_d), 绕开"TDX 是否返回重叠"未验证假设
                need_fetch = (last_d, end_ts)
                staleness_check = (last_d, last_close)
        if need_fetch:
            with self._lock:
                self._fetch_and_store(code, period, need_fetch[0], need_fetch[1], dividend_type)
            if is_full_fetch:
                self._mark_refetch(code, period)  # F5: 记录全量拉取时间
            # F6: 比对重叠 bar close, 不一致 → 分红 shift → 全量重拉
            if staleness_check is not None:
                last_d, old_close = staleness_check
                new_close = self._close_at(code, period, last_d)
                if (old_close is not None and new_close is not None
                        and abs(new_close - old_close) > 1e-6):
                    logger.warning(
                        "kline_staleness: %s %s 重叠 bar %s close 由 %s 变为 %s (分红 shift), 全量重拉",
                        code, period, last_d.strftime("%Y-%m-%d"), old_close, new_close)
                    with self._lock:
                        self._fetch_and_store(code, period, start_ts, end_ts, dividend_type)
                    self._mark_refetch(code, period)
        if period in ("1d", "5m") and not skip_gap_detection:
            self._detect_and_fill_gaps(code, period, start_ts, end_ts, dividend_type)

    # ── F5 冷却 / F6 重叠 bar ──

    _REFETCH_COOLDOWN = pd.Timedelta(hours=24)

    def _refetch_cooled_down(self, code: str, period: str) -> bool:
        """距上次全量拉取 > 24h 才允许再全量拉 (F5 停牌 thrash 冷却)。"""
        with self._conn() as c:
            r = c.execute(
                "SELECT last_full_refetch_at FROM manifest WHERE stock_code=? AND period=?",
                (code, period)).fetchone()
        if not r or not r[0]:
            return True  # 从未全量拉过 → 允许
        last = pd.Timestamp(r[0])
        return (pd.Timestamp.now() - last) > self._REFETCH_COOLDOWN

    def _mark_refetch(self, code: str, period: str):
        with self._conn() as c:
            c.execute("UPDATE manifest SET last_full_refetch_at=? WHERE stock_code=? AND period=?",
                      (datetime.now().isoformat(), code, period))

    def _close_at(self, code: str, period: str, date: pd.Timestamp) -> Optional[float]:
        """读 parquet 在 date 当日的 close (F6 重叠 bar 比对用)。"""
        df = self._read_parquet(code, period, date, date + pd.Timedelta(days=1))
        if df.empty or "close" not in df.columns:
            return None
        s = df["close"].dropna()
        return float(s.iloc[0]) if not s.empty else None

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
        intact = self._check_gaps(code, period, df.index)
        first_d = df.index[0]
        last_d = df.index[-1]
        last_close = float(df["close"].iloc[-1]) if "close" in df.columns else None
        self._manifest_upsert(code, period, first_d.strftime("%Y%m%d"),
                              last_d.strftime("%Y%m%d"), last_close, len(df), intact)

    # ───────────────────── gap 检测 (1d 缺日 + 5m 缺 bar) ─────────────────────

    def _check_gaps(self, code: str, period: str, cached_dates: pd.DatetimeIndex) -> bool:
        """day级: 校验 cached_dates 覆盖的交易日 vs 交易日历 (在 cached 范围内)。
        1d/5m 通用 — 某交易日 0 根 bar 即缺 (002008 5m 6.23-6.29 那种整天缺)。返回 intact=True 无缺。"""
        if len(cached_dates) == 0:
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

    def _detect_and_fill_gaps(self, code: str, period: str, start_ts: pd.Timestamp,
                              end_ts: pd.Timestamp, dividend_type: str):
        """对 [start, end] 做 day级 gap 检测 + 5m 部分 bar 检测, 缺 → 自动补拉, 仍缺 → intact=false 告警。"""
        df = self._read_parquet(code, period, start_ts, end_ts)
        if df.empty:
            return
        cal = self._get_calendar()
        cached_str = {d.strftime("%Y%m%d") for d in df.index}
        lo = max(df.index.min().strftime("%Y%m%d"), start_ts.strftime("%Y%m%d"))
        hi = min(df.index.max().strftime("%Y%m%d"), end_ts.strftime("%Y%m%d"))
        expected = {d for d in cal if lo <= d <= hi}
        # 5m 部分 bar 检测 (整天有 bar 但 < 48, 如半日或盘中缺段)
        if period == "5m":
            self._warn_partial_bars_5m(code, df)
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

    _BARS_PER_DAY_5M = 48  # A股 9:35-15:00 每 5min 一根

    def _warn_partial_bars_5m(self, code: str, df: pd.DataFrame):
        """5m 按日 bar 数检测部分缺口 (整天有 bar 但 < 48)。只告警不置 intact (半日/盘后可能正常)。"""
        if df.empty:
            return
        per_day = df.groupby(df.index.normalize()).size()
        partial = {d.strftime("%Y-%m-%d"): int(n)
                   for d, n in per_day.items() if 0 < n < self._BARS_PER_DAY_5M}
        if partial:
            logger.warning("kline_gap_5m: %s 5m 部分缺 bar (预期 %d 根/日): %s",
                           code, self._BARS_PER_DAY_5M, partial)

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
        # F2 [H3]: 按日期比较, 1d (00:00) 与 5m (09:35-15:00) 都含首末日全天。
        # 直接 <= end_ts (end=00:00) 会把 5m 区间末日 48 根 bar 全切掉。
        start_norm = start_ts.normalize()
        end_norm = end_ts.normalize()
        return df.loc[(df.index.normalize() >= start_norm) & (df.index.normalize() <= end_norm)]
