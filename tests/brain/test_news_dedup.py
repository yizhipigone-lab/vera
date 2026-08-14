"""tests/brain/test_news_dedup.py — 增量去重 + 推送抑制单测。

用 tmp_path 隔离 db, 不碰生产 data/sentiment/news_seen.db。
"""
from __future__ import annotations

import time

from brain.news_dedup import NewsDedup, content_hash, SCHEMA_VERSION


# ── content_hash ──────────────────────────────────────────────────

def test_hash_stable():
    """同文本同 hash。"""
    assert content_hash("新闻A") == content_hash("新闻A")


def test_hash_different_text():
    """不同文本不同 hash。"""
    assert content_hash("新闻A") != content_hash("新闻B")


def test_hash_url_preferred():
    """有 url 时用 url 做 hash (同 url 不同 text 视为同一条)。"""
    h1 = content_hash("文本A", url="http://x/1")
    h2 = content_hash("文本B", url="http://x/1")
    assert h1 == h2


def test_hash_trims_whitespace():
    """空白差异不产生不同 hash (normalized)。"""
    assert content_hash("新闻") == content_hash("  新闻  ")


# ── 新闻级去重往返 ─────────────────────────────────────────────────

def test_news_seen_roundtrip(tmp_path):
    db = NewsDedup(tmp_path / "t.db")
    try:
        h = content_hash("某利好新闻")
        assert not db.is_news_seen(h)
        db.mark_news_seen(h, polarity=0.8, code="300687")
        assert db.is_news_seen(h)
    finally:
        db.close()


def test_filter_unseen_keeps_only_new(tmp_path):
    db = NewsDedup(tmp_path / "t.db")
    try:
        items = [
            {"text": "新闻A", "url": "http://a"},
            {"text": "新闻B", "url": "http://b"},
            {"text": "新闻C", "url": "http://c"},
        ]
        # 先标记 A 已见
        db.mark_news_seen(content_hash("新闻A", "http://a"))
        new = db.filter_unseen(items)
        urls = sorted(it["url"] for it in new)
        assert urls == ["http://b", "http://c"]
        # filter 给每条附了 _hash
        assert all("_hash" in it for it in new)
    finally:
        db.close()


def test_mark_news_seen_idempotent(tmp_path):
    """重复 mark 同一 hash 不报错 (INSERT OR REPLACE)。"""
    db = NewsDedup(tmp_path / "t.db")
    try:
        h = content_hash("新闻")
        db.mark_news_seen(h)
        db.mark_news_seen(h)  # 不抛
        assert db.is_news_seen(h)
    finally:
        db.close()


# ── 推送抑制时间窗 ─────────────────────────────────────────────────

def test_push_suppression_within_window(tmp_path):
    """推过后 30min 内同 code 抑制。"""
    t = [1000.0]
    db = NewsDedup(tmp_path / "t.db", clock=lambda: t[0])
    try:
        assert not db.is_push_suppressed("300687")
        db.mark_pushed("300687")
        assert db.is_push_suppressed("300687")  # 刚推过, 抑制
        # 31 分钟后不再抑制
        t[0] = 1000.0 + 31 * 60
        assert not db.is_push_suppressed("300687")
    finally:
        db.close()


def test_push_suppression_per_code(tmp_path):
    """抑制按 code 隔离 (A 推过不影响 B)。"""
    db = NewsDedup(tmp_path / "t.db")
    try:
        db.mark_pushed("300687")
        assert db.is_push_suppressed("300687")
        assert not db.is_push_suppressed("000001")  # 另一票不抑制
    finally:
        db.close()


# ── purge_old ─────────────────────────────────────────────────────

def test_purge_old_removes_aged(tmp_path):
    """超过 keep_days 的记录被清。"""
    now = time.time()
    db = NewsDedup(tmp_path / "t.db", clock=lambda: now)
    try:
        # 插一条 40 天前的
        old_h = content_hash("旧闻")
        db._conn.execute(
            "INSERT OR REPLACE INTO news_seen(content_hash, ts) VALUES (?,?)",
            (old_h, int(now) - 40 * 86400))
        assert db.is_news_seen(old_h)
        n = db.purge_old(keep_days=30)
        assert n >= 1
        assert not db.is_news_seen(old_h)
    finally:
        db.close()


# ── schema 版本 ────────────────────────────────────────────────────

def test_schema_version_recorded(tmp_path):
    """schema_meta 表记了 SCHEMA_VERSION (改逻辑必 bump 的依据)。"""
    db = NewsDedup(tmp_path / "t.db")
    try:
        cur = db._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'")
        row = cur.fetchone()
        assert row is not None
        assert int(row[0]) == SCHEMA_VERSION
    finally:
        db.close()


def test_db_file_under_data_sentiment(tmp_path):
    """DEFAULT_DB_PATH 指向 data/sentiment/ (不进 data/trade/)。"""
    from brain.news_dedup import DEFAULT_DB_PATH
    s = str(DEFAULT_DB_PATH).replace("\\", "/")
    assert "data/sentiment" in s
    assert "trade" not in s


# ── alert_log (v1-6 日报聚合落库) ──────────────────────────────────

class _FakeAlert:
    """鸭子类型 alert: 验 log_alert 不硬耦合 alert_rules.Alert。"""

    def __init__(self, code, name, polarity, strength=2, rule="stock_sentiment"):
        self.code = code
        self.name = name
        self.polarity = polarity
        self.strength = strength
        self.rule = rule
        self.evidence_quote = "依据"


def test_alert_log_table_exists(tmp_path):
    """v1-6: alert_log 表已建。"""
    db = NewsDedup(tmp_path / "t.db")
    try:
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alert_log'")
        assert cur.fetchone() is not None
    finally:
        db.close()


def test_log_alert_roundtrip(tmp_path):
    db = NewsDedup(tmp_path / "t.db", clock=lambda: 1000.0)
    try:
        db.log_alert(_FakeAlert("300687", "某票", 0.8))
        s = db.daily_alert_summary(since_ts=0)
        assert s["total"] == 1
        assert s["alerts"][0]["code"] == "300687"
        assert s["alerts"][0]["polarity"] == 0.8
        assert s["alerts"][0]["rule"] == "stock_sentiment"
        assert s["bullish"] == 1 and s["bearish"] == 0
    finally:
        db.close()


def test_daily_summary_since_filter(tmp_path):
    """since_ts 之前的异动不计入日报。"""
    db = NewsDedup(tmp_path / "t.db")
    try:
        db.log_alert(_FakeAlert("A", "a票", 0.7), ts=1000.0)
        db.log_alert(_FakeAlert("B", "b票", -0.6), ts=1500.0)
        db.log_alert(_FakeAlert("C", "c票", 0.9), ts=2500.0)
        s = db.daily_alert_summary(since_ts=2000)   # 只 C
        assert s["total"] == 1
        assert s["alerts"][0]["code"] == "C"
        assert s["bearish"] == 0
    finally:
        db.close()


def test_daily_summary_bullish_bearish(tmp_path):
    db = NewsDedup(tmp_path / "t.db")
    try:
        db.log_alert(_FakeAlert("A", "a", 0.8), ts=1.0)
        db.log_alert(_FakeAlert("B", "b", 0.5), ts=2.0)
        db.log_alert(_FakeAlert("C", "c", -0.7), ts=3.0)
        db.log_alert(_FakeAlert("D", "d", 0.0), ts=4.0)  # 中性不计入多/空
        s = db.daily_alert_summary(since_ts=0)
        assert s["total"] == 4
        assert s["bullish"] == 2
        assert s["bearish"] == 1
    finally:
        db.close()


def test_daily_summary_empty(tmp_path):
    db = NewsDedup(tmp_path / "t.db")
    try:
        assert db.daily_alert_summary(since_ts=0) == \
            {"alerts": [], "bullish": 0, "bearish": 0, "total": 0}
    finally:
        db.close()


def test_log_alert_duck_typed_missing_attrs(tmp_path):
    """缺属性的 alert 不崩 (getattr 兜底 None)。"""
    db = NewsDedup(tmp_path / "t.db", clock=lambda: 1000.0)
    try:
        class Bare:
            code = "X"
        db.log_alert(Bare())
        s = db.daily_alert_summary(since_ts=0)
        assert s["total"] == 1
        assert s["alerts"][0]["code"] == "X"
        assert s["alerts"][0]["polarity"] is None
        assert s["bullish"] == 0 and s["bearish"] == 0
    finally:
        db.close()
