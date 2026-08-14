"""tests/brain/test_sentiment_pipeline.py — 编排层单测 (全 mock, 无真实网络/LLM)。

monkeypatch 替换外部 (_fetch_watch_news / judge_batch / _fetch_snapshot),
tmp_path 隔离 dedup, FakeNotifier 收集推送。验证 fail-soft + 端到端数据流。
"""
from __future__ import annotations

import ast

import brain.sentiment_pipeline as sp
from brain.news_dedup import NewsDedup


class FakeNotifier:
    def __init__(self):
        self.pushed = []
        self.summaries = []

    def notify_alert(self, payload, title="", color="red"):
        self.pushed.append((payload, title, color))

    def notify_summary(self, payload, title="舆情日报"):
        self.summaries.append((payload, title))


def _mock_empty_snapshot(monkeypatch):
    """所有测试默认把行情快照 mock 成空 (避免真调 akshare)。"""
    monkeypatch.setattr(sp, "_fetch_snapshot", lambda: "")


# ── 物理隔离自检 ───────────────────────────────────────────────────

def test_no_trade_import():
    with open(sp.__file__, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert not a.name.startswith("trade"), f"物理隔离破坏: import {a.name}"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith("trade"), f"物理隔离破坏: from {mod}"


# ── 端到端 (mock 外部) ─────────────────────────────────────────────

def test_run_tick_end_to_end_mocked(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "_fetch_watch_news", lambda cfg: [
        {"text": "半导体大爆发", "url": "http://a"},
    ])
    monkeypatch.setattr(sp, "judge_batch", lambda items: [
        {"polarity": 0.8, "strength": 2, "confidence": 0.9,
         "evidence_quote": "爆发", "hit_pool": [{"value": "300687"}]},
    ])
    _mock_empty_snapshot(monkeypatch)
    dedup = NewsDedup(tmp_path / "t.db")
    notifier = FakeNotifier()
    try:
        stats = sp.run_sentiment_tick(dedup=dedup, notifier=notifier)
        assert stats["scanned"] == 1
        assert stats["new"] == 1
        assert stats["scored"] == 1
        assert stats["alerts_fired"] == 1
        assert stats["alerts_pushed"] == 1
        assert notifier.pushed[0][0]["code"] == "300687"
        assert notifier.pushed[0][2] == "green"   # polarity 0.8 → 绿
    finally:
        dedup.close()


def test_run_tick_no_news(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "_fetch_watch_news", lambda cfg: [])
    _mock_empty_snapshot(monkeypatch)
    dedup = NewsDedup(tmp_path / "t.db")
    notifier = FakeNotifier()
    try:
        stats = sp.run_sentiment_tick(dedup=dedup, notifier=notifier)
        assert stats["scanned"] == 0
        assert stats["alerts_pushed"] == 0
        assert notifier.pushed == []
    finally:
        dedup.close()


# ── fail-soft: 各环节挂了不崩 ──────────────────────────────────────

def test_run_tick_fetch_news_raises(monkeypatch, tmp_path):
    def boom(cfg):
        raise RuntimeError("网络炸")
    monkeypatch.setattr(sp, "_fetch_watch_news", boom)
    _mock_empty_snapshot(monkeypatch)
    dedup = NewsDedup(tmp_path / "t.db")
    try:
        stats = sp.run_sentiment_tick(dedup=dedup, notifier=FakeNotifier())
        assert stats["scanned"] == 0
        assert stats["alerts_pushed"] == 0
    finally:
        dedup.close()


def test_run_tick_judge_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "_fetch_watch_news", lambda cfg: [{"text": "x", "url": "u"}])

    def judge_boom(items):
        raise RuntimeError("LLM 炸")
    monkeypatch.setattr(sp, "judge_batch", judge_boom)
    _mock_empty_snapshot(monkeypatch)
    dedup = NewsDedup(tmp_path / "t.db")
    try:
        stats = sp.run_sentiment_tick(dedup=dedup, notifier=FakeNotifier())
        assert stats["scored"] == 0
        assert stats["alerts_pushed"] == 0
    finally:
        dedup.close()


def test_run_tick_snapshot_raises(monkeypatch, tmp_path):
    """行情快照抛异常, 外层 try/except 兜底, tick 不崩 (规则3/4 降级跳过)。"""
    monkeypatch.setattr(sp, "_fetch_watch_news", lambda cfg: [])

    def snapshot_boom():
        raise RuntimeError("akshare 炸")
    monkeypatch.setattr(sp, "_fetch_snapshot", snapshot_boom)
    dedup = NewsDedup(tmp_path / "t.db")
    try:
        stats = sp.run_sentiment_tick(dedup=dedup, notifier=FakeNotifier())
        assert stats["alerts_pushed"] == 0
    finally:
        dedup.close()


# ── 推送抑制在 tick 内生效 ─────────────────────────────────────────

def test_run_tick_push_suppression(monkeypatch, tmp_path):
    """同 url 新闻第二轮 tick 被 dedup 命中 → 不重复打分/推送。"""
    monkeypatch.setattr(sp, "_fetch_watch_news", lambda cfg: [
        {"text": "利空", "url": "http://a"},
    ])
    monkeypatch.setattr(sp, "judge_batch", lambda items: [
        {"polarity": -0.9, "strength": 2, "confidence": 0.9,
         "evidence_quote": "崩", "hit_pool": [{"value": "300687"}]},
    ])
    _mock_empty_snapshot(monkeypatch)
    dedup = NewsDedup(tmp_path / "t.db")
    notifier = FakeNotifier()
    try:
        sp.run_sentiment_tick(dedup=dedup, notifier=notifier)
        # 第二轮: 同 url → dedup 命中 → fresh 空
        sp.run_sentiment_tick(dedup=dedup, notifier=notifier)
        assert len(notifier.pushed) == 1   # 只第一轮推了
    finally:
        dedup.close()


# ── tick 落库异动明细 (供日报聚合) ─────────────────────────────────

def test_tick_logs_alert_to_db(monkeypatch, tmp_path):
    """tick 推送后, alert_log 库里留下异动明细 (polarity/rule)。"""
    monkeypatch.setattr(sp, "_fetch_watch_news", lambda cfg: [
        {"text": "半导体大爆发", "url": "http://a"},
    ])
    monkeypatch.setattr(sp, "judge_batch", lambda items: [
        {"polarity": 0.8, "strength": 2, "confidence": 0.9,
         "evidence_quote": "爆发", "hit_pool": [{"value": "300687"}]},
    ])
    _mock_empty_snapshot(monkeypatch)
    dedup = NewsDedup(tmp_path / "t.db")
    try:
        sp.run_sentiment_tick(dedup=dedup, notifier=FakeNotifier())
        s = dedup.daily_alert_summary(since_ts=0)
        assert s["total"] == 1
        assert s["alerts"][0]["code"] == "300687"
        assert s["alerts"][0]["polarity"] == 0.8
        assert s["alerts"][0]["rule"] == "stock_sentiment"
    finally:
        dedup.close()


# ── 盘后日报 (v1-6) ────────────────────────────────────────────────

def test_daily_report_aggregates_and_pushes(tmp_path):
    """日报聚合 alert_log → notify_summary 收到 top_movers + sector_heat。"""
    import time as _time
    from brain.alert_rules import Alert
    dedup = NewsDedup(tmp_path / "t.db")
    notifier = FakeNotifier()
    try:
        now = _time.time()
        dedup.log_alert(Alert("stock_sentiment", "300687", "某票", 0.8, 2,
                              0.9, "爆发", {}, now))
        dedup.log_alert(Alert("sector_cluster", "半导体", "半导体", 0.6, 2,
                              0.8, "", {}, now))
        stats = sp.run_daily_report(dedup=dedup, notifier=notifier)
        assert stats["alerts"] == 2
        assert stats["bullish"] == 2
        assert len(notifier.summaries) == 1
        payload = notifier.summaries[0][0]
        codes = [m["code"] for m in payload["top_movers"]]
        assert "300687" in codes
        sectors = [s["name"] for s in payload["sector_heat"]]
        assert "半导体" in sectors
    finally:
        dedup.close()


def test_daily_report_empty_still_pushes(tmp_path):
    """无异动也推一张日报 (证监控存活; notifier 收到空 top_movers)。"""
    dedup = NewsDedup(tmp_path / "t.db")
    notifier = FakeNotifier()
    try:
        stats = sp.run_daily_report(dedup=dedup, notifier=notifier)
        assert stats["alerts"] == 0
        assert len(notifier.summaries) == 1
        assert notifier.summaries[0][0]["top_movers"] == []
    finally:
        dedup.close()


def test_daily_report_fail_soft_on_db_error(tmp_path):
    """daily_alert_summary 抛异常 → 日报不崩 (降级空, 仍推一张)。"""
    dedup = NewsDedup(tmp_path / "t.db")
    notifier = FakeNotifier()

    def boom(since):
        raise RuntimeError("DB 炸")
    dedup.daily_alert_summary = boom
    try:
        stats = sp.run_daily_report(dedup=dedup, notifier=notifier)
        assert stats["alerts"] == 0
        assert len(notifier.summaries) == 1   # 仍推 (空卡)
    finally:
        dedup.close()
