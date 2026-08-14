"""tests/brain/test_alert_rules.py — 规则判定 + 行情解析 + 推送抑制单测。

纯函数测试 (无 LLM 无网络); apply_push_suppression 用 tmp_path 隔离的真 NewsDedup。
物理隔离自检: AST 断言本模块无 import trade。
"""
from __future__ import annotations

import ast

import brain.alert_rules as ar
from brain.alert_rules import (
    Alert, DEFAULT_CONFIG, _safe_float, parse_market_snapshot,
    rule_stock_sentiment, rule_sector_cluster, rule_index_move,
    rule_volume_anomaly, apply_push_suppression, load_config,
)
from brain.news_dedup import NewsDedup


# ── 物理隔离自检 ───────────────────────────────────────────────────

def test_no_trade_import():
    with open(ar.__file__, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert not a.name.startswith("trade"), f"物理隔离破坏: import {a.name}"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith("trade"), f"物理隔离破坏: from {mod}"


# ── _safe_float ────────────────────────────────────────────────────

def test_safe_float_formats():
    assert _safe_float("+2.30") == 2.30
    assert _safe_float("1.23%") == 1.23
    assert _safe_float("4,500") == 4500.0
    assert _safe_float("1.2e10") == 1.2e10
    assert _safe_float("--") is None
    assert _safe_float("") is None
    assert _safe_float(None) is None
    assert _safe_float("-1.5") == -1.5


# ── parse_market_snapshot (基于 data_tools to_string 格式造样本) ───

_SAMPLE_SNAPSHOT = """# 市场快照数据包

## A股主要指数

      代码      名称   最新价  涨跌幅  成交额
 sh000001  上证综指  3200.5   +2.30  520000000000
 sz399001  深证成指  10500.3  +1.80  620000000000
 sz399006  创业板指  2100.0   -0.50  180000000000

## 涨停池

日期 20260813，涨停 45 家（前 25 条）

 名称  涨跌幅  连板数  所属行业  封板资金
 中芯国际  +10.0  2  半导体  120000000
 北方华创  +10.0  1  半导体  80000000
 寒武纪  +20.0  3  半导体  200000000
 浪潮信息  +10.0  1  算力  50000000
"""


def test_parse_indices():
    parsed = parse_market_snapshot(_SAMPLE_SNAPSHOT)
    names = [i["name"] for i in parsed["indices"]]
    assert "上证综指" in names
    sh = next(i for i in parsed["indices"] if "上证" in i["name"])
    assert sh["pct"] == 2.30
    assert sh["amount"] == 520000000000


def test_parse_zt_count():
    assert parse_market_snapshot(_SAMPLE_SNAPSHOT)["zt_count"] == 45


def test_parse_zt_by_industry():
    ind = parse_market_snapshot(_SAMPLE_SNAPSHOT)["zt_by_industry"]
    assert ind.get("半导体") == 3
    assert ind.get("算力") == 1


def test_parse_empty_snapshot():
    r = parse_market_snapshot("")
    assert r == {"indices": [], "zt_count": 0, "zt_by_industry": {}}


# ── 规则1: 个股情绪突变 ───────────────────────────────────────────

def test_rule_stock_sentiment_triggers():
    news = [{"text": "被立案", "polarity": -0.8, "strength": 2,
             "confidence": 0.9, "evidence_quote": "立案调查",
             "hit_pool": [{"type": "stock", "value": "300687"}]}]
    alerts = rule_stock_sentiment(news, DEFAULT_CONFIG)
    assert len(alerts) == 1
    assert alerts[0].code == "300687"
    assert alerts[0].rule == "stock_sentiment"


def test_rule_stock_sentiment_below_threshold():
    news = [{"text": "小利好", "polarity": 0.4, "strength": 2,
             "hit_pool": [{"value": "300687"}]}]   # polarity 0.4 < 0.6
    assert rule_stock_sentiment(news, DEFAULT_CONFIG) == []


def test_rule_stock_sentiment_weak_strength():
    news = [{"text": "利好", "polarity": 0.8, "strength": 1,   # strength 1 < 2
             "hit_pool": [{"value": "300687"}]}]
    assert rule_stock_sentiment(news, DEFAULT_CONFIG) == []


def test_rule_stock_sentiment_disabled():
    cfg = {"rules": {"stock_sentiment": {"enabled": False}}}
    news = [{"polarity": 0.9, "strength": 2, "hit_pool": [{"value": "X"}]}]
    assert rule_stock_sentiment(news, cfg) == []


# ── 规则2: 板块新闻密集 ───────────────────────────────────────────

def test_rule_sector_cluster_dense():
    # 半导体命中 3 条, 情绪一致偏多
    news = [
        {"text": "半导体涨价", "polarity": 0.7, "evidence_quote": ""},
        {"text": "半导体产能满载", "polarity": 0.6, "evidence_quote": ""},
        {"text": "半导体需求爆发", "polarity": 0.8, "evidence_quote": ""},
    ]
    alerts = rule_sector_cluster(news, DEFAULT_CONFIG)
    assert any(a.code == "半导体" for a in alerts)


def test_rule_sector_cluster_sparse():
    # 仅 1 条半导体, 不足 min_hits=3
    news = [{"text": "半导体利好", "polarity": 0.8, "evidence_quote": ""}]
    assert rule_sector_cluster(news, DEFAULT_CONFIG) == []


# ── 规则3: 大盘指数阈值 ───────────────────────────────────────────

def test_rule_index_move_triggers():
    # 上证 +2.30% / 深证 +1.80% 均 ≥ 1.5 → 触发; 创业 -0.50% 不触发
    alerts = rule_index_move(_SAMPLE_SNAPSHOT, DEFAULT_CONFIG, ts=1000.0)
    assert any("000001" in a.code or "上证" in a.name for a in alerts)
    assert all(a.ts == 1000.0 for a in alerts)
    # 创业板 -0.5 不应触发
    assert not any("创业" in a.name for a in alerts)


def test_rule_index_move_below_threshold():
    cfg = {"rules": {"index_move": {"enabled": True, "threshold_pct": 3.0,
                                    "watch_indices": ["上证"]}}}
    # 上证 +2.30 < 3.0 → 不触发
    assert rule_index_move(_SAMPLE_SNAPSHOT, cfg) == []


# ── 规则4: 量能异常 (v1 默认关) ────────────────────────────────────

def test_rule_volume_anomaly_disabled_by_default():
    assert rule_volume_anomaly(_SAMPLE_SNAPSHOT, DEFAULT_CONFIG) == []


def test_rule_volume_anomaly_enabled_triggers():
    cfg = {"rules": {"volume_anomaly": {"enabled": True, "threshold_x": 1.5}}}
    alerts = rule_volume_anomaly(_SAMPLE_SNAPSHOT, cfg)
    # 上证成交 5200亿 < 8000亿粗阈值 → v1 占位不触发
    # (这条测试锁: v1 占位阈值 8000亿, 样本 5200亿不触发)
    assert alerts == []


# ── 推送抑制 ──────────────────────────────────────────────────────

def _make_alerts() -> list[Alert]:
    return [
        Alert("stock_sentiment", "A", "A票", 0.9, 2, 0.9, "e1", {}, 1.0),
        Alert("stock_sentiment", "B", "B票", 0.7, 2, 0.8, "e2", {}, 1.0),
        Alert("stock_sentiment", "C", "C票", 0.5, 1, 0.7, "e3", {}, 1.0),
        Alert("stock_sentiment", "D", "D票", 0.3, 1, 0.6, "e4", {}, 1.0),
    ]


def test_apply_push_suppression_max_per_tick(tmp_path):
    db = NewsDedup(tmp_path / "t.db")
    try:
        out = apply_push_suppression(_make_alerts(), db, DEFAULT_CONFIG)
        assert len(out) == 3   # max_per_tick=3
        # 按热度降序: A(|.9|*2=1.8) > B(1.4) > C(.5) > D(.3)
        assert [a.code for a in out] == ["A", "B", "C"]
    finally:
        db.close()


def test_apply_push_suppression_window(tmp_path):
    db = NewsDedup(tmp_path / "t.db")
    try:
        # 第一轮推 A
        first = apply_push_suppression(_make_alerts()[:1], db, DEFAULT_CONFIG)
        assert len(first) == 1 and first[0].code == "A"
        # 30min 内再推 A → 被抑制
        again = apply_push_suppression(_make_alerts()[:1], db, DEFAULT_CONFIG)
        assert again == []
    finally:
        db.close()


# ── load_config ───────────────────────────────────────────────────

def test_load_config_missing_file_uses_default(tmp_path):
    cfg = load_config(tmp_path / "nonexistent.yaml")
    assert cfg["rules"]["stock_sentiment"]["polarity_min"] == 0.6


def test_load_config_override(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("rules:\n  stock_sentiment:\n    polarity_min: 0.8\n",
                 encoding="utf-8")
    cfg = load_config(p)
    assert cfg["rules"]["stock_sentiment"]["polarity_min"] == 0.8
    # 未覆盖的字段保留默认
    assert cfg["rules"]["stock_sentiment"]["strength_min"] == 2
    assert cfg["suppression"]["max_per_tick"] == 3
