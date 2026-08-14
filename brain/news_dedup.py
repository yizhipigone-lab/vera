"""brain/news_dedup.py — 新闻增量去重 + 推送抑制 (SQLite)。

两层职责:
1. 新闻级去重: 同一条新闻 (content_hash) 不重复打分/处理。盘中每 10min
   全量拉取, 没这层会重复喂 LLM (费钱) + 重复推送 (骚扰)。
2. 标的级推送抑制: 同一股票代码 N 分钟内只推一次 (alert_rules 调用)。

SCHEMA_VERSION (memory 铁律: 改缓存逻辑必 bump): 当前 v1。

fail-soft: DB 异常不阻塞 (返 False/空, 降级为"全当新新闻"——宁可重复处理
也不卡住 sentiment 流水线)。永不碰 data/trade/ (守业务铁律 1)。

线程安全: scheduler worker 单线程调, 但 _run_coro_sync 可能开新线程跑
LLM, 故 SQLite check_same_thread=False + 内部 Lock 双保险。
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

from utils.logger import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 2
DEFAULT_DB_PATH = Path("data/sentiment/news_seen.db")

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS news_seen (
    content_hash TEXT PRIMARY KEY,
    url TEXT,
    code TEXT,
    polarity REAL,
    ts INTEGER NOT NULL,
    pushed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS push_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_push_log_code_ts ON push_log(code, ts);
CREATE TABLE IF NOT EXISTS alert_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT,
    name TEXT,
    polarity REAL,
    strength INTEGER,
    rule TEXT,
    evidence TEXT,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_log_ts ON alert_log(ts);
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
PRAGMA user_version = {SCHEMA_VERSION};
"""


def content_hash(text: str, url: str | None = None) -> str:
    """新闻指纹: 优先 url (url 是新闻唯一标识), 否则 normalized text 的 sha256。"""
    src = url if url else (text or "").strip()
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:32]


class NewsDedup:
    """线程安全的去重库 (SQLite WAL + 内部 Lock)。"""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH,
                 clock: Callable[[], float] = time.time):
        self._path = Path(db_path)
        self._clock = clock
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._connect()

    def _connect(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
            (str(SCHEMA_VERSION),))

    # ── 新闻级去重 ─────────────────────────────────────────────────

    def is_news_seen(self, chash: str) -> bool:
        try:
            with self._lock:
                cur = self._conn.execute(
                    "SELECT 1 FROM news_seen WHERE content_hash=?", (chash,))
                return cur.fetchone() is not None
        except sqlite3.Error as e:
            logger.warning(f"is_news_seen DB 异常 (降级未见): {e}")
            return False

    def filter_unseen(self, items: list[dict]) -> list[dict]:
        """items: [{text, url, ...}], 返回 content_hash 未在库的子集。
        给每条附 _hash 字段供后续 mark_news_seen。"""
        out = []
        for it in items:
            chash = it.get("_hash") or content_hash(it.get("text", ""), it.get("url"))
            it["_hash"] = chash
            if not self.is_news_seen(chash):
                out.append(it)
        return out

    def mark_news_seen(self, chash: str, polarity: float | None = None,
                       url: str | None = None, code: str | None = None) -> None:
        ts = int(self._clock())
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO news_seen(content_hash, url, code, polarity, ts, pushed) "
                    "VALUES (?,?,?,?,?,0)",
                    (chash, url, code, polarity, ts))
        except sqlite3.Error as e:
            logger.warning(f"mark_news_seen DB 异常 (跳过): {e}")

    # ── 标的级推送抑制 ──────────────────────────────────────────────

    def is_push_suppressed(self, code: str, within_min: int = 30) -> bool:
        """同一 code 在最近 within_min 分钟内推过 → True (抑制)。"""
        cutoff = int(self._clock()) - within_min * 60
        try:
            with self._lock:
                cur = self._conn.execute(
                    "SELECT 1 FROM push_log WHERE code=? AND ts>=? LIMIT 1",
                    (code, cutoff))
                return cur.fetchone() is not None
        except sqlite3.Error as e:
            logger.warning(f"is_push_suppressed DB 异常 (降级不抑制): {e}")
            return False

    def mark_pushed(self, code: str) -> None:
        ts = int(self._clock())
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO push_log(code, ts) VALUES (?,?)", (code, ts))
        except sqlite3.Error as e:
            logger.warning(f"mark_pushed DB 异常 (跳过): {e}")

    # ── 维护 ───────────────────────────────────────────────────────

    def purge_old(self, keep_days: int = 30) -> int:
        """清理 keep_days 天前的记录 (防无限膨胀)。返 news_seen 删除条数。"""
        cutoff = int(self._clock()) - keep_days * 86400
        try:
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM news_seen WHERE ts<?", (cutoff,))
                self._conn.execute("DELETE FROM push_log WHERE ts<?", (cutoff,))
                self._conn.execute("DELETE FROM alert_log WHERE ts<?", (cutoff,))
                return cur.rowcount or 0
        except sqlite3.Error as e:
            logger.warning(f"purge_old DB 异常: {e}")
            return 0

    # ── 日报聚合 (v1-6) ────────────────────────────────────────────

    def log_alert(self, alert, ts: float | None = None) -> None:
        """记录一条已推送的异动 (盘后日报聚合用)。fail-soft。

        alert 鸭子类型: 只读 .code/.name/.polarity/.strength/.rule/.evidence_quote
        属性 (alert_rules.Alert 满足); 本模块不 import alert_rules 以免耦合。
        """
        t = int(ts if ts is not None else self._clock())
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO alert_log(code,name,polarity,strength,rule,evidence,ts) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (getattr(alert, "code", None), getattr(alert, "name", None),
                     getattr(alert, "polarity", None), getattr(alert, "strength", None),
                     getattr(alert, "rule", None),
                     getattr(alert, "evidence_quote", None), t))
        except sqlite3.Error as e:
            logger.warning(f"log_alert DB 异常 (跳过): {e}")

    def daily_alert_summary(self, since_ts: float) -> dict:
        """聚合 since_ts 之后已推送的异动 (盘后日报用)。fail-soft 返空结构。

        返 {alerts:[{code,name,polarity,strength,rule,evidence}],
             bullish:int, bearish:int, total:int}。
        alerts 按 ts 升序 (写入顺序)。
        """
        empty = {"alerts": [], "bullish": 0, "bearish": 0, "total": 0}
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT code,name,polarity,strength,rule,evidence FROM alert_log "
                    "WHERE ts>=? ORDER BY ts ASC",
                    (int(since_ts),)).fetchall()
        except sqlite3.Error as e:
            logger.warning(f"daily_alert_summary DB 异常: {e}")
            return empty
        alerts = [{"code": r[0], "name": r[1], "polarity": r[2],
                   "strength": r[3], "rule": r[4], "evidence": r[5]} for r in rows]
        bullish = sum(1 for a in alerts if (a["polarity"] or 0) > 0)
        bearish = sum(1 for a in alerts if (a["polarity"] or 0) < 0)
        return {"alerts": alerts, "bullish": bullish,
                "bearish": bearish, "total": len(alerts)}

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
