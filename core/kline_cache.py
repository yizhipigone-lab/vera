# -*- coding: utf-8 -*-
"""本地 K 线 parquet 缓存 (Phase 1: 1d + gap 检测).

回测优先读本地 parquet, miss-fetch 增量补 TDX, 1d 缺日检测 + 自动补拉 + 告警。
治 002008 类"运行时 TDX 临时缺数据"问题 —— 数据完整性在缓存层兜底。

设计见 docs/plan/2026-07-17_本地K线parquet缓存_计划书.md。
"""
from __future__ import annotations

import sqlite3
import threading
import os
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from core import progress as _progress
from core.dividend_type import to_tdx_str

# 2026-07-18: 协作式停止 (web「停止回测」按钮)。批量脚本从不置位, 行为不变。
from core.stop_flag import raise_if_stopped
from utils import parquet_cache as pcu
from utils.code_normalizer import normalize_list
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

    def __init__(self, cache_dir, tdx_fetcher: Callable, calendar_fetcher: Callable,
                 *, probe_hours: float = 12.0):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "calendar").mkdir(exist_ok=True)
        (self.cache_dir / "1d").mkdir(exist_ok=True)
        (self.cache_dir / "5m").mkdir(exist_ok=True)
        (self.cache_dir / "1m").mkdir(exist_ok=True)  # 2026-07-26
        self.db_path = self.cache_dir / "manifest.db"
        self.tdx_fetcher = tdx_fetcher
        self.calendar_fetcher = calendar_fetcher
        # 2026-09-16 C5: Lock → RLock。manifest 写方法 (_mark_refetch/_mark_probe/
        # _manifest_set_intact/force_invalidate/_manifest_upsert) 内部已补
        # `with self._lock:`, 而 _refresh_manifest → _mark_probe/_manifest_upsert
        # 是在 _ensure/_probe_shift 已持锁的路径里被调 (锁内调锁), 必须可重入。
        self._lock = threading.RLock()
        # 2026-07-18: 复权因子漂移探针间隔 (0 = 禁用)。除权后前复权历史价整体
        # 平移, F6 只在增量扩展时检测, 区间已覆盖时靠探针自愈。
        self._probe_interval = pd.Timedelta(hours=float(probe_hours))
        # 2026-08-16 Fix A: 持久连接 — 原 _conn() 每次查询新开连接 + 两条 PRAGMA,
        # 每只股每次 get 开 2~3 个新连接 (实测 nt.stat 上万次)。改为单条持久连接
        # (check_same_thread=False), 写路径已有 self._lock 串行, 读路径单线程。
        self._db = self._open_db()
        self._init_db()

    def _open_db(self):
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ───────────────────── sqlite manifest ─────────────────────

    def _conn(self):
        # 2026-08-16 Fix A: 返回持久连接 (不再每次新建)。调用方 `with self._conn()
        # as c:` 语义不变 — with 管的是事务 commit/rollback, 不是连接生命周期。
        return self._db

    def close(self):
        """关闭持久 sqlite 连接 (2026-09-16 C2 — 原全类无 close, 句柄泄漏)。
        重复调用安全; 关闭后实例不可再用。"""
        db = getattr(self, "_db", None)
        if db is not None:
            self._db = None
            try:
                db.close()
            except Exception:
                pass

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
            # 迁移: 2026-07-18 复权因子漂移探针
            try:
                c.execute("ALTER TABLE manifest ADD COLUMN last_probe_at TEXT")
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
        # 2026-09-16 C5: manifest 写收口进锁 (RLock — 本方法经 _refresh_manifest
        # 在 _ensure/_probe_shift 已持锁的路径里被调, 可重入)
        with self._lock:
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

    # ───────────────────── 只读统计 (治理III W3-schema) ───────────
    # 维护工具 (kline_cache_maintenance) 经本接口访问, 不再裸开 manifest.db ——
    # 列名/schema 知识只在 kline_cache.py 定义 (DDL 在 _init_db, 读查询在此)。

    def cached_last_date(self, period: str) -> Optional[str]:
        """该 period 的缓存 MAX(last_date) ('YYYYMMDD'), 无记录返 None。"""
        with self._conn() as c:
            row = c.execute(
                "SELECT MAX(last_date) FROM manifest WHERE period=?",
                (period,)).fetchone()
        return row[0] if row and row[0] else None

    def manifest_stats(self, period: str) -> dict:
        """单个 period 概览 {stocks, first_date, last_date, not_intact}。"""
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*), MIN(first_date), MAX(last_date), "
                "SUM(CASE WHEN intact=0 THEN 1 ELSE 0 END) "
                "FROM manifest WHERE period=?", (period,)).fetchone()
        return {"stocks": row[0] or 0, "first_date": row[1],
                "last_date": row[2], "not_intact": row[3] or 0}

    def last_bar_traded_ratio(self, period: str = "1d",
                              sample: int = 50) -> Optional[float]:
        """抽检「最后一根 bar 有没有真成交」的比例 —— 判空壳 bar (2026-09-17)。

        为什么需要它: `MAX(last_date)` 只说明"这一行存在", 说明不了"这一行有数据"。
        实测 (2026-09-17): 2026-09-16 沪深 5204 只票**都有该日行**, 但只有 **10 只**
        成交量 > 0 (0.2%) —— 那是盘前抓数留下的**空壳 bar**。若只用日期判新鲜,
        空壳会被当成好数据, 补拉永远不触发, 它就永远卡在缓存里
        (实证: 大盘位置体温表一直显示"数据滞后", 而 `stale_periods` 却说 1d 新鲜)。

        做法: 从 manifest 取该 period 的代码列表, 按固定步长**确定性抽样** sample 只
        (同输入同结果, 便于测试), 读各自最后一根 bar 的 volume, 统计 >0 的比例。

        Returns:
            float 0~1; **None = 查不了** (无记录/无末根/读盘异常)。
            调用方拿到 None 必须按"查不了"处理 —— **不得**据此判陈旧,
            否则读盘抖动会误触发全量补拉 (那是小时级代价)。
        """
        try:
            last = self.cached_last_date(period)
            if not last:
                return None
            with self._conn() as c:
                rows = c.execute(
                    "SELECT stock_code FROM manifest WHERE period=? "
                    "ORDER BY stock_code", (period,)).fetchall()
            codes = [r[0] for r in rows]
            if not codes:
                return None
            ts = pd.Timestamp(str(last))
            step = max(1, len(codes) // max(1, int(sample)))
            picked = codes[::step][:max(1, int(sample))]
            traded = total = 0
            for code in picked:
                try:
                    df = self._read_parquet(code, period, ts, ts)
                except Exception:      # 单只读失败只少一个样本, 不推翻结论
                    continue
                if df is None or len(df) == 0 or "volume" not in df.columns:
                    continue
                total += 1
                if float(df["volume"].iloc[-1]) > 0:
                    traded += 1
            return (traded / total) if total else None
        except Exception as e:         # 任何意外都退成"查不了", 不改变旧行为
            logger.warning("last_bar_traded_ratio 异常 (按查不了处理): %s", e)
            return None

    # ───────────────────── trading calendar ─────────────────────

    def _calendar_path(self) -> Path:
        return self.cache_dir / "calendar" / "trading_days.parquet"

    def _get_calendar(self) -> set:
        """返回交易日集合 (str YYYYMMDD)。命中 parquet 直接读, 否则拉取落盘。

        2026-08-16 Fix C: 实例级 memoize — 交易日历一天内不变, 原实现每次调用
        (缺口检测每只股一次) 都重读 parquet。"""
        cached = getattr(self, "_calendar_cache", None)
        if cached is not None:
            return cached
        p = self._calendar_path()
        if p.exists():
            df = pq.read_table(p).to_pandas()
            result = set(df["date"].astype(str).tolist())
            self._calendar_cache = result
            return result
        dates = [str(d) for d in self.calendar_fetcher()]
        df = pd.DataFrame({"date": dates})
        # 2026-08-01: 收编 pcu 原语 (原固定 .tmp 名是最后一个没收编点) —
        # pid+uuid 独立 tmp + Windows 退避 atomic replace, 同 _write_merge 范式
        tmp = pcu.tmp_path_for(p)
        table = pa.Table.from_pandas(df)
        pq.write_table(table, tmp)
        pcu.atomic_replace(tmp, p,
                           rewrite=lambda t: pq.write_table(table, t))
        self._calendar_cache = set(dates)
        return self._calendar_cache

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
        _n = len(stock_list)
        for _i, code in enumerate(stock_list, 1):
            # 2026-07-18: 停止回测按钮 — 缓存 miss 逐只拉网是长耗时点, 逐只检查
            raise_if_stopped()
            self._ensure(code, period, start_ts, end_ts, dividend_type)
            if _i % 200 == 0 or _i == _n:  # 2026-07-26: 细粒度进度
                _progress.report("fetch", _i / _n, f"{_i}/{_n} 只", _i, _n)
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
        # 只读模式 (VERA_KLINE_READONLY=1, 批量历史回测取数专用): 有缓存记录就
        # 直接用现状, 零网络动作 —— 不增量/不探针/不补缺口/不因 intact=false
        # 全量重拉。2026-08-14 601888.SH 事件: intact=false 的 3343 只票在 16 年
        # 5m 寻优 prep 里逐只触发全史分块重拉 (几百次 TDX 请求/只), prep 被打爆。
        # 这些票的"缺口"多为远古停牌日, 补不补对回测零影响。
        # 无记录 (rec is None) 仍走正常逻辑做首次拉取 (否则该票完全无数据)。
        if rec is not None and os.environ.get("VERA_KLINE_READONLY") == "1":
            return
        need_fetch: Optional[tuple] = None
        staleness_check: Optional[tuple] = None  # (last_d, old_last_close)
        is_full_fetch = False
        skip_gap_detection = False
        intact = False  # 仅在 rec is not None 分支被赋真值; 用于 Fix D 判断
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
                fetch_ok = self._fetch_and_store(code, period, need_fetch[0], need_fetch[1], dividend_type)
            if is_full_fetch and fetch_ok:
                # 2026-09-16 C3: 拉取失败不盖 F5 冷却戳 — 瞬时故障也盖戳会让
                # intact=False 的票被 24h 冷却吞掉重试
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
                        refetch_ok = self._fetch_and_store(code, period, start_ts, end_ts, dividend_type)
                    if refetch_ok:  # 2026-09-16 C3: 失败不盖戳
                        self._mark_refetch(code, period)
        # 2026-08-14: 只读模式 (VERA_KLINE_READONLY=1) — 批量历史回测取数专用。
        # 跳过两类网络校验: 复权因子探针 + 缺口补拉。动机: 16 年长区间 5m 寻优
        # prep 实测, 缺口补拉对远古停牌日逐只发 TDX 请求 (永远无数据, 每次 ~1s),
        # 几千只票把取数从分钟级拖到 50 小时级; 而这些校验对"读历史"零价值。
        # 缺数据段 (need_fetch) 的正常拉取不受此开关影响。
        if os.environ.get("VERA_KLINE_READONLY") == "1":
            return
        probe_ran = False
        if need_fetch is None and self._probe_due(code, period):
            # 2026-07-18: 复权因子漂移探针。F6 只在增量扩展时检测, 区间已覆盖
            # (含 intact=false 冷却期内) 的因子漂移靠探针自愈 — 600000.SH 事件
            # 里浦发 1d 缓存 intact=false, 探针挂在 intact 分支后永远到不了。
            probe_ran = True
            self._probe_shift(code, period, dividend_type)
        # 2026-08-16 Fix D: manifest 已确认 intact=True 且本轮无取数 (区间完全被
        # 缓存覆盖) 时, 跳过缺口检测 —— 缺口不会凭空出现, 上次已查过无缺; 重复
        # 检测是每只股每次 get 的第二次 parquet 读 + 日历读 + strftime 的元凶。
        # 任何区间扩展 / intact=False / 本轮有取数 (need_fetch 非 None) 一律照查,
        # 缺口兜底逻辑不丢; 部分 bar 告警在首次取数时已发过, 不再逐次重发。
        # 审计补充: 探针可能因复权漂移触发全量重拉 (数据变了), 此时本地 intact
        # 已陈旧 → 必须照查缺口, 不能跳过 (probe_ran 兜底)。
        intact_covered = (rec is not None) and intact and (need_fetch is None) \
            and not probe_ran
        if (period in ("1d", "5m", "1m") and not skip_gap_detection
                and not intact_covered):
            self._detect_and_fill_gaps(code, period, start_ts, end_ts, dividend_type)

    # ── F5 冷却 / F6 重叠 bar ──

    _REFETCH_COOLDOWN = pd.Timedelta(hours=24)

    def _cooldown_elapsed(self, code: str, period: str, column: str,
                          interval: pd.Timedelta) -> bool:
        """manifest.column 记录时间距现在超过 interval → True; 从未记录 → True。

        F5 全量重拉冷却与复权因子探针共用的"冷却是否已过"判定 (2026-08-01 提取)。
        column 只传本类内部常量, 非外部输入。
        """
        with self._conn() as c:
            r = c.execute(
                f"SELECT {column} FROM manifest WHERE stock_code=? AND period=?",
                (code, period)).fetchone()
        if not r or not r[0]:
            return True  # 从未记录 → 冷却已过
        return (pd.Timestamp.now() - pd.Timestamp(r[0])) > interval

    def _refetch_cooled_down(self, code: str, period: str) -> bool:
        """距上次全量拉取 > 24h 才允许再全量拉 (F5 停牌 thrash 冷却)。"""
        return self._cooldown_elapsed(code, period, "last_full_refetch_at",
                                      self._REFETCH_COOLDOWN)

    def _mark_refetch(self, code: str, period: str):
        # 2026-09-16 C5: manifest 写收口进锁 (RLock, _probe_shift 等持锁路径可重入)
        with self._lock:
            with self._conn() as c:
                c.execute("UPDATE manifest SET last_full_refetch_at=? WHERE stock_code=? AND period=?",
                          (datetime.now().isoformat(), code, period))

    # ── 复权因子漂移探针 (2026-07-18, 600000.SH 事件) ──

    def _mark_probe(self, code: str, period: str):
        # 2026-09-16 C5: manifest 写收口进锁 (RLock, _refresh_manifest 持锁路径可重入)
        with self._lock:
            with self._conn() as c:
                c.execute("UPDATE manifest SET last_probe_at=? WHERE stock_code=? AND period=?",
                          (datetime.now().isoformat(), code, period))

    def _probe_due(self, code: str, period: str) -> bool:
        """距上次探测超过间隔 → 该探。从未探过 (存量缓存) → 首次接触自愈。"""
        if self._probe_interval <= pd.Timedelta(0):
            return False
        return self._cooldown_elapsed(code, period, "last_probe_at",
                                      self._probe_interval)

    def _probe_shift(self, code: str, period: str, dividend_type: str):
        """复权因子漂移探针: 比对缓存末日 close 与 TDX 现值, 漂移 → 全量重拉。

        背景 (600000.SH 事件): 除权后前复权历史价整体平移, F6 重叠 bar 检测只在
        增量扩展时触发, 区间已覆盖时无任何检测 — 5m 缓存 9.26 / 1d 缓存 8.86 /
        TDX 直拉 9.53 三基准并存。漂移确诊后照 F6 模式直接全量重拉
        (绕 F5 冷却 — 冷却防的是停牌 thrash, 不是确诊漂移)。
        探针异常 (TDX 不可用) → 吞掉, 用缓存现状, 不阻塞服务。
        """
        try:
            rec = self._manifest_get(code, period)
            if rec is None:
                return
            last_d = pd.Timestamp(rec[1])
            df_day = self._read_parquet(code, period, last_d, last_d + pd.Timedelta(days=1))
            old_close = None
            if not df_day.empty and "close" in df_day.columns:
                s = df_day["close"].dropna()
                if not s.empty:
                    old_close = float(s.iloc[-1])  # 末日最后一根 bar (5m 也正确)
            raw = self.tdx_fetcher(
                [code], (last_d - pd.Timedelta(days=10)).strftime("%Y%m%d"),
                pd.Timestamp.now().strftime("%Y%m%d"),
                period=period, dividend_type=dividend_type)
            self._mark_probe(code, period)
            new_close = None
            if raw and "Close" in raw and code in raw["Close"].columns:
                s = raw["Close"][code].dropna()
                if not s.empty:
                    if not isinstance(s.index, pd.DatetimeIndex):
                        s.index = pd.to_datetime(s.index)
                    same = s[s.index.normalize() == last_d.normalize()]
                    if not same.empty:
                        new_close = float(same.iloc[-1])
            if (old_close is not None and new_close is not None
                    and abs(new_close - old_close) > 1e-6):
                logger.warning(
                    "kline_factor_shift: %s %s 探针发现 %s close 缓存 %s vs TDX %s "
                    "(复权因子漂移), 全量重拉",
                    code, period, last_d.strftime("%Y-%m-%d"), old_close, new_close)
                first_d = pd.Timestamp(rec[0])
                with self._lock:
                    refetch_ok = self._fetch_and_store(code, period, first_d,
                                                       pd.Timestamp.now(), dividend_type)
                if refetch_ok:  # 2026-09-16 C3: 失败不盖 F5 冷却戳
                    self._mark_refetch(code, period)
        except Exception:
            logger.debug("kline_probe: %s %s 探针失败 (TDX 不可用?), 用缓存现状",
                         code, period, exc_info=True)

    def force_invalidate(self, code: str, period: str):
        """显式强制失效 (force_refresh): intact=False + 清 F5 冷却标记。

        F5 冷却防的是自动路径的停牌 thrash; 用户显式 force_refresh 意图优先,
        下次 get 必全量重拉 (此前 force_refresh 被 24h 冷却静默吞掉)。
        """
        self._manifest_set_intact(code, period, False)
        # 2026-09-16 C5: manifest 写收口进锁
        with self._lock:
            with self._conn() as c:
                c.execute("UPDATE manifest SET last_full_refetch_at=NULL "
                          "WHERE stock_code=? AND period=?", (code, period))

    def _close_at(self, code: str, period: str, date: pd.Timestamp) -> Optional[float]:
        """读 parquet 在 date 当日的 close (F6 重叠 bar 比对用)。"""
        df = self._read_parquet(code, period, date, date + pd.Timedelta(days=1))
        if df.empty or "close" not in df.columns:
            return None
        s = df["close"].dropna()
        # 2026-09-16 P0-2: 取当日**最后一根** bar, 与 manifest last_close
        # (_refresh_manifest 存全文件末根) 口径对齐。原 iloc[0] 取首根,
        # 5m/1m 每日增量比对必然超阈 → 误报分红 shift 全量重拉。
        return float(s.iloc[-1]) if not s.empty else None

    def _fetch_and_store(self, code: str, period: str, fstart: pd.Timestamp,
                         fend: pd.Timestamp, dividend_type: str) -> bool:
        """拉取并落盘。返回 True=有数据落库, False=拉取失败/无数据 (2026-09-16 C3 —
        调用方据此决定是否盖 F5 全量重拉冷却戳: 失败不盖, 瞬时故障下次可重试)。"""
        # 2026-07-26: TDX 单次 ~24000 根上限守卫 (1m 长窗口防前段静默截断)。
        # 分钟级且跨度 >80 交易日时按 ≤80 交易日分段递归拉取; _write_merge 去重合并。
        _bpday = self._INTRADAY_BARS_PER_DAY.get(period)
        if _bpday:
            # _get_calendar() 返回 set (成员运算设计), 必须先排序再分段 —
            # 乱序 chunk 的 (chunk[0], chunk[-1]) 会拼出倒置/错误范围 (实测:
            # 全市场回填出现 [06-12~05-12] 倒置拉取失败)
            _seg = sorted(d for d in self._get_calendar()
                          if fstart.strftime("%Y%m%d") <= d <= fend.strftime("%Y%m%d"))
            if len(_seg) > 80:
                _ok = True
                for _s in range(0, len(_seg), 80):
                    _chunk = _seg[_s:_s + 80]
                    _ok = self._fetch_and_store(code, period,
                                                pd.Timestamp(_chunk[0]),
                                                pd.Timestamp(_chunk[-1]), dividend_type) and _ok
                return _ok
        raw = self.tdx_fetcher([code], fstart.strftime("%Y%m%d"),
                               fend.strftime("%Y%m%d"), period=period,
                               dividend_type=dividend_type)
        if not raw or "Close" not in raw or code not in raw["Close"].columns:
            logger.warning("kline_fetch_fail: %s %s [%s~%s] 拉取无数据",
                           code, period, fstart.date(), fend.date())
            return False
        close_s = raw["Close"][code].dropna()
        if close_s.empty:
            return False
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
        return True

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
        # 2026-08-01: 收编 pcu 原语 (同 _get_calendar) — pid+uuid 独立 tmp
        # (并发写同 stock 互不踩踏/移走) + Windows 退避 atomic replace
        # (杀软/索引器短暂锁 pfile → PermissionError(WinError 5) 重试; tmp 偶删
        # → rewrite 重写再试)。行为与 2026-07-21/23 手写版一致。
        tmp = pcu.tmp_path_for(pfile)
        table = pa.Table.from_pandas(out, preserve_index=False)
        pq.write_table(table, tmp)
        pcu.atomic_replace(tmp, pfile,
                           rewrite=lambda t: pq.write_table(table, t))

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
        # 新拉的数据天然与 TDX 同步, 探测时间一并刷新 (12h 内不再探)
        self._mark_probe(code, period)

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
        # 2026-08-16 Fix B: 逐 bar strftime 改向量化 (原列表推导每根 bar 一次 strftime)
        cached_str = set(df.index.strftime("%Y%m%d"))
        lo = max(df.index.min().strftime("%Y%m%d"), start_ts.strftime("%Y%m%d"))
        hi = min(df.index.max().strftime("%Y%m%d"), end_ts.strftime("%Y%m%d"))
        expected = {d for d in cal if lo <= d <= hi}
        # 分钟级部分 bar 检测 (整天有 bar 但 < 预期根数, 如半日或盘中缺段)
        if period in ("5m", "1m"):
            self._warn_partial_bars_intraday(code, period, df)
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
        cached_str2 = set(df2.index.strftime("%Y%m%d"))
        still = sorted(expected - cached_str2)
        if still:
            logger.warning("kline_gap: %s %s 补拉后仍缺 %s, 标记 intact=false",
                           code, period, still)
            self._manifest_set_intact(code, period, False)
        else:
            self._manifest_set_intact(code, period, True)

    # 分钟级每日 bar 数 (局部表; 不 import backtest._constants 防包循环:
    # backtest/__init__ → engine → core.data_fetcher → core.kline_cache → backtest)
    _INTRADAY_BARS_PER_DAY = {"5m": 48, "1m": 240}

    def _warn_partial_bars_intraday(self, code: str, period: str, df: pd.DataFrame):
        """分钟级按日 bar 数检测部分缺口 (整天有 bar 但 < 预期根数)。只告警不置 intact。"""
        if df.empty:
            return
        expected = self._INTRADAY_BARS_PER_DAY.get(period)
        if not expected:
            return
        per_day = df.groupby(df.index.normalize()).size()
        partial = {d.strftime("%Y-%m-%d"): int(n)
                   for d, n in per_day.items() if 0 < n < expected}
        if partial:
            logger.warning("kline_gap_%s: %s %s 部分缺 bar (预期 %d 根/日): %s",
                           period, code, period, expected, partial)

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
        # 2026-09-16 C5: manifest 写收口进锁 (RLock, 持锁路径可重入)
        with self._lock:
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
        # 2026-08-16 Fix B: normalize() 原被调两次 (每次全索引遍历), 算一次复用。
        start_norm = start_ts.normalize()
        end_norm = end_ts.normalize()
        norm = df.index.normalize()
        return df.loc[(norm >= start_norm) & (norm <= end_norm)]
