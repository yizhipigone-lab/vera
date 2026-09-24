"""tests/brain/test_sentiment_notifier.py — 独立推送器单测 (不真发 webhook)。

物理隔离自检: 断言本模块源码不含 import trade / from trade。
"""
from __future__ import annotations

import ast
import time

import research.sentiment_notifier as sn_module
from research.sentiment_notifier import SentimentNotifier, polarity_emoji


# ── 物理隔离自检 (守业务铁律 1) ────────────────────────────────────

def test_source_has_no_trade_import():
    """本模块 AST 无 import trade / from trade (审计 HIGH-2 物理隔离)。
    用 AST 解析而非朴素字符串匹配——后者会把 docstring 里讨论 'import trade'
    的说明文字误判成真 import。AST 只看真正的 import 语句节点。"""
    with open(sn_module.__file__, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("trade"), \
                    f"物理隔离破坏: import {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith("trade"), \
                f"物理隔离破坏: from {mod} import"


# ── polarity_emoji 阈值 ───────────────────────────────────────────

def test_emoji_strong_positive():
    assert polarity_emoji(0.8) == "利好"

def test_emoji_strong_negative():
    assert polarity_emoji(-0.8) == "利空"

def test_emoji_neutral():
    assert polarity_emoji(0.0) == "中性"

def test_emoji_mild_positive():
    assert polarity_emoji(0.3) == "偏多"

def test_emoji_mild_negative():
    assert polarity_emoji(-0.3) == "偏空"


# ── notify_alert 入队 / no-op ─────────────────────────────────────

def test_notify_alert_noop_when_disabled():
    n = SentimentNotifier(lambda: False, lambda: "http://x")
    n.notify_alert({"code": "300687", "polarity": 0.8}, "测试")
    assert n._queue.qsize() == 0

def test_notify_alert_noop_when_no_webhook():
    n = SentimentNotifier(lambda: True, lambda: None)
    n.notify_alert({"code": "300687", "polarity": 0.8}, "测试")
    assert n._queue.qsize() == 0

def test_notify_alert_enqueues():
    n = SentimentNotifier(lambda: True, lambda: "http://x")
    n.notify_alert({"code": "300687", "polarity": 0.8, "name": "赛意信息"},
                   "测试", color="red")
    assert n._queue.qsize() == 1
    job = n._queue.get_nowait()
    assert job["kind"] == "alert"
    assert job["data"]["code"] == "300687"
    assert job["color"] == "red"


# ── 卡片构建 ──────────────────────────────────────────────────────

def test_build_alert_card_structure():
    n = SentimentNotifier(lambda: True, lambda: "http://x")
    card = n._build_alert_card(
        {"code": "300687", "name": "赛意信息", "polarity": -0.8,
         "strength": 2, "confidence": 0.9, "evidence_quote": "被立案",
         "hit_pool": [{"type": "stock", "value": "300687"}],
         "ts": 1700000000},
        title="舆情异动", color="red")
    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["template"] == "red"
    content = card["card"]["elements"][0]["text"]["content"]
    assert "300687" in content
    assert "赛意信息" in content
    assert "利空" in content   # polarity -0.8
    assert "被立案" in content


def test_build_alert_card_missing_fields_fail_soft():
    """缺 name/evidence/hit_pool 不崩, 用默认值。"""
    n = SentimentNotifier(lambda: True, lambda: "http://x")
    card = n._build_alert_card({"code": "000001", "polarity": 0.0}, "测试", "orange")
    content = card["card"]["elements"][0]["text"]["content"]
    assert "000001" in content
    assert "中性" in content


def test_build_summary_card_empty():
    n = SentimentNotifier(lambda: True, lambda: "http://x")
    card = n._build_summary_card({"date": "2026-08-13"}, "舆情日报")
    content = card["card"]["elements"][0]["text"]["content"]
    assert "无显著异动" in content


def test_build_summary_card_with_movers():
    n = SentimentNotifier(lambda: True, lambda: "http://x")
    card = n._build_summary_card(
        {"date": "2026-08-13",
         "top_movers": [{"code": "300687", "name": "赛意", "polarity": 0.7}],
         "sector_heat": [{"name": "半导体", "score": 0.5}]},
        "舆情日报")
    elements = card["card"]["elements"]
    assert len(elements) >= 2  # movers + hr + heat
    assert "赛意" in elements[0]["text"]["content"]


# ── worker 端到端 (mock _post) ────────────────────────────────────

def test_worker_posts_alert():
    """start → notify → stop, _post 被调且参数对。"""
    posted = []
    n = SentimentNotifier(lambda: True, lambda: "http://x")
    n._post = lambda webhook, body: posted.append((webhook, body))  # mock
    n.start()
    try:
        n.notify_alert({"code": "300687", "polarity": 0.8, "name": "赛意"}, "测试")
        time.sleep(0.6)  # 给 worker 处理
    finally:
        n.stop()
    assert len(posted) == 1
    webhook, body = posted[0]
    assert webhook == "http://x"
    assert body["msg_type"] == "interactive"


def test_worker_exception_does_not_crash():
    """_post 抛异常, worker 不崩 (fail-soft), 后续 job 仍处理。"""
    posted = []
    n = SentimentNotifier(lambda: True, lambda: "http://x")
    call_count = [0]

    def flaky_post(webhook, body):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("模拟飞书故障")
        posted.append(body)

    n._post = flaky_post
    n.start()
    try:
        n.notify_alert({"code": "A", "polarity": 0.5}, "1")
        n.notify_alert({"code": "B", "polarity": -0.5}, "2")
        time.sleep(0.8)
    finally:
        n.stop()
    # 第一条抛异常被吞, 第二条仍处理
    assert len(posted) == 1
