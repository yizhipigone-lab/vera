"""tests/brain/test_sentiment_judge.py — 情绪量化器单测。

只测纯函数 (parse_sentiment_output 边界 + judge_sentiment fail-soft);
不调真实 LLM (慢且贵), 真实链路靠集成测试。
"""
from __future__ import annotations

from brain.sentiment_judge import (
    parse_sentiment_output,
    judge_sentiment,
    judge_batch,
    DEFAULT_MAX_BATCH,
)


# ── parse_sentiment_output: 正常解析 ──────────────────────────────

def test_parse_plain_json():
    """裸 JSON 正常解析。"""
    text = '{"polarity": -0.8, "strength": 2, "confidence": 0.85, "evidence_quote": "被立案调查", "hit_pool": [{"type":"stock","value":"300687"}]}'
    out = parse_sentiment_output(text)
    assert out is not None
    assert out["polarity"] == -0.8
    assert out["strength"] == 2
    assert out["confidence"] == 0.85
    assert "立案" in out["evidence_quote"]


def test_parse_json_with_fence():
    """```json 围栏 + 前后杂波能提取。"""
    text = '分析结果如下:\n```json\n{"polarity": 0.6, "strength": 1, "confidence": 0.7, "evidence_quote": "业绩预增", "hit_pool": []}\n```\n以上。'
    out = parse_sentiment_output(text)
    assert out is not None
    assert out["polarity"] == 0.6
    assert out["strength"] == 1


# ── parse_sentiment_output: schema 不符返 None ─────────────────────

def test_parse_missing_field_returns_none():
    """缺 strength 字段 → None (不许部分通过)。"""
    text = '{"polarity": 0.6, "confidence": 0.7}'
    assert parse_sentiment_output(text) is None


def test_parse_polarity_out_of_range():
    """polarity > 1 → None。"""
    text = '{"polarity": 1.5, "strength": 1, "confidence": 0.7, "evidence_quote":"", "hit_pool":[]}'
    assert parse_sentiment_output(text) is None


def test_parse_polarity_below_minus_one():
    """polarity < -1 → None。"""
    text = '{"polarity": -1.2, "strength": 2, "confidence": 0.9, "evidence_quote":"", "hit_pool":[]}'
    assert parse_sentiment_output(text) is None


def test_parse_strength_invalid():
    """strength=3 (非 0/1/2) → None。"""
    text = '{"polarity": 0.5, "strength": 3, "confidence": 0.7, "evidence_quote":"", "hit_pool":[]}'
    assert parse_sentiment_output(text) is None


def test_parse_confidence_out_of_range():
    """confidence > 1 → None。"""
    text = '{"polarity": 0.5, "strength": 1, "confidence": 1.5, "evidence_quote":"", "hit_pool":[]}'
    assert parse_sentiment_output(text) is None


def test_parse_not_json():
    """非 JSON 文本 → None。"""
    assert parse_sentiment_output("这不是 JSON") is None


def test_parse_empty():
    """空文本 → None。"""
    assert parse_sentiment_output("") is None


def test_parse_strength_bool_rejected():
    """strength=True (bool 是 int 子类) → None (不许 bool 蒙混)。"""
    text = '{"polarity": 0.5, "strength": true, "confidence": 0.7, "evidence_quote":"", "hit_pool":[]}'
    assert parse_sentiment_output(text) is None


# ── judge_sentiment: fail-soft ─────────────────────────────────────

def test_judge_empty_text_returns_error():
    """空文本不调 LLM, 直接返 error。"""
    out = judge_sentiment("")
    assert "error" in out
    assert "为空" in out["error"]


def test_judge_whitespace_only_returns_error():
    """纯空白不调 LLM。"""
    out = judge_sentiment("   \n  ")
    assert "error" in out


# ── judge_batch: 截断 + 不中断 ─────────────────────────────────────

def test_batch_truncates_to_max():
    """超过 max_batch 的截断 (审计 M2: ≤10)。"""
    items = [{"text": f"新闻{i}", "id": i} for i in range(20)]
    # 空/CLI缺失都会快速返 error, 不真调 LLM; 但这里文本非空会尝试调 CLI。
    # 为避免测试依赖 CLI, 用空文本让 judge_sentiment 早返。
    items = [{"text": "", "id": i} for i in range(20)]
    out = judge_batch(items, max_batch=5)
    assert len(out) == 5  # 截断到 5


def test_batch_default_max_is_10():
    """默认上限 10。"""
    assert DEFAULT_MAX_BATCH == 10
