# -*- coding: utf-8 -*-
"""tests/brain/test_sentiment_shape_contract.py — 打分层 ↔ 规则层 的形状契约 (2026-09-20)。

**本文件是哪条 bug 的复现测试**：`judge_batch` 返回**嵌套**结构
`{"id":…, "sentiment":{polarity,…}}`（它自己的 docstring 就是这么写的），
而消费方按**扁平**读 `d["polarity"]`。于是分数恒为 None/0：

    brain/sentiment_judge.py::judge_batch  →  {"id":…, "sentiment":{polarity:0.8,…}}
    brain/sentiment_pipeline.py:145        →  r.get("polarity")            → None
    brain/alert_rules.py::rule_stock_sentiment → news.get("polarity", 0.0) → 0.0
                                              → abs(0.0) < 0.6 → 永远 continue

**后果（生产实证）**：规则 1（个股情绪）与规则 2（板块聚集）**自上线起从未触发过**；
`news_seen` 360 条全部 `polarity=NULL`；全量 179 条异动 **100% 是 `index_move`**
（那条规则只看指数涨跌，不依赖新闻打分）。

**为什么原来的测试没抓到（本文件存在的核心理由）**
`test_sentiment_acceptance.py::test_full_tick_fires_all_enabled_rules` 把
`judge_batch` **整个 mock 掉、并手写了扁平形状**：

    monkeypatch.setattr(sp, "judge_batch", lambda items: [{"polarity": 0.8, ...}])

测试锁的是**假设**，不是**契约** —— 真实形状差异被 mock 挡在门外，永远走不到，
于是全程绿灯、生产静默失效 5 周。

**故本文件刻意不 patch `judge_batch`**，只桩掉最内层的 LLM IO（`judge_sentiment`），
让**真实的包装函数**参与进来。分数层的返回形状一旦变化，这里必红。
（纪律依据：mock 只能用来隔离外部 IO —— TDX / QMT / 网络 / 时钟 / LLM 进程。）
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import brain.news_dedup as nd            # noqa: E402
import brain.sentiment_judge as sj       # noqa: E402
import brain.sentiment_pipeline as sp    # noqa: E402

# 显式配置：本文件只验形状契约，不受 config/sentiment.yaml 现状影响
CFG = {
    "news": {"max_per_scan": 15},
    "rules": {
        "stock_sentiment": {"enabled": True, "polarity_min": 0.6, "strength_min": 2},
        "sector_cluster": {"enabled": False},
        "index_move": {"enabled": False},
        "volume_anomaly": {"enabled": False},
    },
    "watch": {"concepts": []},
    "suppression": {"push_window_min": 30, "max_per_tick": 3},
}

_NEWS = [{"text": "某票业绩大爆发，订单排到明年", "url": "http://example.com/1"}]

# 真实 judge_sentiment 成功时的返回形状（见其 docstring 与 parse_sentiment_output）
_SENT_OK = {"polarity": 0.8, "strength": 2, "confidence": 0.9,
            "evidence_quote": "订单排到明年", "hit_pool": [{"value": "300687"}]}


class FakeNotifier:
    def __init__(self):
        self.pushed = []

    def notify_alert(self, payload, title="", color="red"):
        self.pushed.append((payload, title, color))

    def notify_summary(self, payload, title="舆情日报"):
        pass


def _tick(monkeypatch, tmp_path, sent_ret, news=None):
    """跑一轮真实 tick：**只桩 LLM IO**，judge_batch / 规则 / 编排全走真代码。"""
    monkeypatch.setattr(sj, "judge_sentiment",
                        lambda text, timeout=None: dict(sent_ret))
    monkeypatch.setattr(sp, "_fetch_watch_news", lambda cfg: list(news or _NEWS))
    monkeypatch.setattr(sp, "_fetch_snapshot", lambda: "")
    dedup = nd.NewsDedup(tmp_path / "t.db")
    notifier = FakeNotifier()
    try:
        stats = sp.run_sentiment_tick(cfg=CFG, dedup=dedup, notifier=notifier)
        pushed = [p[0] for p in notifier.pushed]
    finally:
        dedup.close()
    return stats, pushed


# ── 契约 1: 真实包装函数产出的形状，规则必须能消费 ────────────────────

def test_真实judge_batch的输出必须能被规则消费(monkeypatch, tmp_path):
    """**本条就是那个 bug 的复现测试。**

    分数层真实返回 `{"id":…, "sentiment":{…}}`；规则层期望扁平。
    两者对不上 → 规则 1 永远不触发。修好后本测试绿。
    """
    stats, pushed = _tick(monkeypatch, tmp_path, _SENT_OK)
    rules = {a.get("rule") for a in pushed}
    assert "stock_sentiment" in rules, (
        "真实 judge_batch 的形状喂进去后规则 1 没触发 —— "
        f"分数没被拆包。stats={stats}, pushed_rules={rules}"
    )
    assert stats["scored"] == 1


def test_拆包后极性真的传到告警里(monkeypatch, tmp_path):
    """不只是"触发了"，极性/强度/证据都要是对的。"""
    _, pushed = _tick(monkeypatch, tmp_path, _SENT_OK)
    a = next(x for x in pushed if x.get("rule") == "stock_sentiment")
    assert a["polarity"] == pytest.approx(0.8)
    assert a["strength"] == 2
    assert a["code"] == "300687"
    assert a["evidence_quote"] == "订单排到明年"


def test_告警时间戳不是_epoch0(monkeypatch, tmp_path):
    """规则从分数里读 ts 构造 Alert；拆包时若不把 ts 带进去就是 1970 年，
    飞书卡片与日报的时间字段会印错。"""
    _, pushed = _tick(monkeypatch, tmp_path, _SENT_OK)
    a = next(x for x in pushed if x.get("rule") == "stock_sentiment")
    assert a["ts"] > 1_600_000_000, f"告警 ts 看起来是 epoch 0: {a['ts']}"


def test_告警带上了新闻正文(monkeypatch, tmp_path):
    """规则把 news['text'] 塞进 detail 供人核对；拆包时不补 text 就丢了。"""
    _, pushed = _tick(monkeypatch, tmp_path, _SENT_OK)
    a = next(x for x in pushed if x.get("rule") == "stock_sentiment")
    assert "业绩大爆发" in str(a.get("detail", ""))


# ── 契约 2: 打分失败项不许被当成已打分 ───────────────────────────────

def test_打分失败的项不计入scored(monkeypatch, tmp_path):
    """`judge_batch` 把失败包成 `{"sentiment": {"error": ...}}`；
    原来在**顶层**判 `"error" not in r`，恒为真 → 失败项被当成打分成功
    （日志里 `scored` 恒等于 `new` 就是这个原因）。"""
    stats, pushed = _tick(monkeypatch, tmp_path, {"error": "情绪量化超时"})
    assert stats["scored"] == 0, "打分失败项被误计为 scored"
    assert stats["new"] == 1, "去重统计不该受影响"
    assert pushed == [], "失败项不该产生告警"


def test_打分失败的项也以_polarity_None_落去重库(monkeypatch, tmp_path):
    """失败仍要标记已见（否则同一新闻每轮重试），但 polarity 是 None。"""
    _, _ = _tick(monkeypatch, tmp_path, {"error": "x"})
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    try:
        rows = conn.execute("select polarity from news_seen").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1 and rows[0][0] is None


# ── 契约 3: 极性为 0 的新闻不该触发（防止反向修成"凡打分必触发"）──────

def test_极性不够的新闻不触发规则1(monkeypatch, tmp_path):
    weak = dict(_SENT_OK, polarity=0.3, strength=1)
    stats, pushed = _tick(monkeypatch, tmp_path, weak)
    assert stats["scored"] == 1, "低分也算打分成功"
    assert pushed == [], "低于阈值却触发了 —— 阈值失效"
