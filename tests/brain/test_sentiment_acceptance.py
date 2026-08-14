"""tests/brain/test_sentiment_acceptance.py — v1 验收: 业务铁律 1 的硬证明。

情绪/舆情**只报告显示, 绝不影响任何买卖决策**。本文件用三道独立证据锁死这条铁律:
  1. AST 静态扫描: 4 个舆情模块源码无任何 import trade / from trade。
  2. 结构扫描: trade_main.py (实盘入口) 不引用任何舆情模块 (未嵌入)。
  3. 运行时文件证据: 跑一轮真实 tick (mock 外部), data/trade/ 下文件清单不变。
另加规则集成测试: 一轮 tick 同时命中规则 1/2/3 (规则 4 v1 默认关)。

物理隔离断言用 AST (ast.walk Import/ImportFrom), 不用字符串匹配 ——
docstring 里讨论 "import trade" 会被字符串匹配误判 (v1-3 亲历教训)。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import brain.alert_rules as ar
import brain.news_dedup as nd
import brain.sentiment_pipeline as sp
import research.sentiment_notifier as sn

# 4 个舆情模块 (全量物理隔离对象)
_SENTIMENT_MODULES = [ar, nd, sp, sn]


class FakeNotifier:
    def __init__(self):
        self.pushed = []
        self.summaries = []

    def notify_alert(self, payload, title="", color="red"):
        self.pushed.append((payload, title, color))

    def notify_summary(self, payload, title="舆情日报"):
        self.summaries.append((payload, title))


def _mock_empty_snapshot(monkeypatch):
    monkeypatch.setattr(sp, "_fetch_snapshot", lambda: "")


# ── 证据 1: AST 静态扫描 4 模块无 import trade ──────────────────────

def test_ast_no_trade_import_all_sentiment_modules():
    """4 个舆情模块源码均无 import trade / from trade (集中断言)。"""
    for mod in _SENTIMENT_MODULES:
        with open(mod.__file__, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert not a.name.startswith("trade"), \
                        f"{mod.__name__} 物理隔离破坏: import {a.name}"
            elif isinstance(node, ast.ImportFrom):
                modname = node.module or ""
                assert not modname.startswith("trade"), \
                    f"{mod.__name__} 物理隔离破坏: from {modname}"


# ── 证据 2: trade_main.py 不引用舆情模块 (未嵌入实盘入口) ───────────

def test_trade_main_does_not_reference_sentiment():
    """实盘入口 trade_main.py 不 import / 不调用任何舆情模块。"""
    src = Path("trade_main.py").read_text(encoding="utf-8")
    # 只禁舆情特征词 (trade_main 可自由用其它 brain 模块做研究)
    for needle in ("sentiment", "alert_rules", "news_dedup",
                   "sentiment_pipeline", "run_sentiment_tick", "run_daily_report"):
        assert needle not in src, f"trade_main.py 引用了舆情特征 {needle!r} (违反未嵌入)"


# ── 证据 3: 跑一轮 tick, data/trade/ 文件清单不变 ───────────────────

def _list_trade_files() -> list[str]:
    """data/trade/ 下所有文件的相对路径 (不存在则空)。只看清单不看内容 ——
    实盘进程追加写 trade.db 不改清单, 而舆情若误建 trade 文件会让清单变长。"""
    root = Path("data/trade")
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)).replace("\\", "/")
                  for p in root.rglob("*") if p.is_file())


def test_tick_does_not_touch_trade_dir(monkeypatch, tmp_path):
    """一轮真实 tick (mock 外部 + dedup 隔离 tmp) 不在 data/trade/ 增删文件。"""
    monkeypatch.setattr(sp, "_fetch_watch_news", lambda cfg: [
        {"text": "半导体大爆发", "url": "http://a"},
    ])
    monkeypatch.setattr(sp, "judge_batch", lambda items: [
        {"polarity": 0.8, "strength": 2, "confidence": 0.9,
         "evidence_quote": "爆发", "hit_pool": [{"value": "300687"}]},
    ])
    _mock_empty_snapshot(monkeypatch)
    before = _list_trade_files()
    dedup = nd.NewsDedup(tmp_path / "t.db")
    try:
        sp.run_sentiment_tick(dedup=dedup, notifier=FakeNotifier())
    finally:
        dedup.close()
    after = _list_trade_files()
    assert before == after, \
        f"舆情 tick 改动了 data/trade/ (before={before} after={after})"


# ── 证据 4: 一轮 tick 同时命中规则 1/2/3 (规则 4 v1 默认关) ─────────

def test_full_tick_fires_all_enabled_rules(monkeypatch, tmp_path):
    """端到端: 同一轮 tick 命中 个股情绪 / 板块密集 / 大盘指数 三条规则。"""
    monkeypatch.setattr(sp, "_fetch_watch_news", lambda cfg: [
        {"text": "某票业绩大爆发", "url": "http://1"},     # → 规则1
        {"text": "半导体涨价", "url": "http://2"},         # → 规则2 (3条聚集)
        {"text": "半导体产能满载", "url": "http://3"},
        {"text": "半导体需求旺盛", "url": "http://4"},
    ])
    monkeypatch.setattr(sp, "judge_batch", lambda items: [
        {"polarity": 0.8, "strength": 2, "confidence": 0.9,
         "evidence_quote": "爆发", "hit_pool": [{"value": "300687"}]},
        {"polarity": 0.6, "strength": 2, "confidence": 0.8,
         "evidence_quote": "", "hit_pool": []},
        {"polarity": 0.7, "strength": 2, "confidence": 0.8,
         "evidence_quote": "", "hit_pool": []},
        {"polarity": 0.5, "strength": 1, "confidence": 0.7,
         "evidence_quote": "", "hit_pool": []},
    ])
    # 行情快照: 上证 +2.30% → 规则3
    monkeypatch.setattr(sp, "_fetch_snapshot", lambda: _SNAPSHOT_INDEX_UP)

    dedup = nd.NewsDedup(tmp_path / "t.db")
    notifier = FakeNotifier()
    try:
        stats = sp.run_sentiment_tick(dedup=dedup, notifier=notifier)
    finally:
        dedup.close()

    assert stats["alerts_fired"] == 3   # 规则 1+2+3 各一
    rules_pushed = {p[0]["rule"] for p in notifier.pushed}
    assert rules_pushed == {"stock_sentiment", "sector_cluster", "index_move"}, \
        f"应命中三条规则, 实际推送: {rules_pushed}"
    # 规则 4 (量能异常) v1 默认关: 不在推送里
    assert "volume_anomaly" not in rules_pushed


_SNAPSHOT_INDEX_UP = (
    "## A股主要指数\n\n"
    "      代码      名称   最新价  涨跌幅  成交额\n"
    " sh000001  上证综指  3200.5   +2.30  520000000000\n"
)
