# -*- coding: utf-8 -*-
"""tests/test_sentiment_api.py — 舆情页 API 端点 + store 区间查询单测 (2026-09-20)。

覆盖三件事：
  1. `NewsDedup.alerts_between`（区间/筛选/排序/截断/降级）—— store 契约；
  2. `/api/sentiment/*` 三个端点的入参契约与 **fail-soft**（库缺失不许 500，
     也不许"静默空白"——照 2026-09-17 大盘 422 那个坑的教训，错误必须看得见）；
  3. 铁律 1：`sentiment_api.py` 零 import trade（与
     tests/brain/test_sentiment_acceptance.py 的集中断言**互为独立守卫**：
     那边靠名单成员资格，这边自包含，任一处漏了都还能拦住）。
  4. `RULE_NAMES` 键全集 = 4 条规则 —— 防前端/后端出现第二份规则名。

一律用 tmp 库，不碰生产 `data/sentiment/news_seen.db`。
"""
import ast
import datetime as dt
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import sentiment_api as sa  # noqa: E402
from brain.news_dedup import NewsDedup  # noqa: E402

D18 = dt.datetime(2026, 9, 18, 14, 41, 35)
D17 = dt.datetime(2026, 9, 17, 10, 5, 0)


def _alert(code, name, polarity, strength, rule, evidence, when):
    return SimpleNamespace(code=code, name=name, polarity=polarity,
                           strength=strength, rule=rule,
                           evidence_quote=evidence, ts=when.timestamp())


@pytest.fixture
def dedup(tmp_path):
    """真 NewsDedup（tmp 库）+ 3 条跨两天的种子数据。"""
    d = NewsDedup(db_path=tmp_path / "news_seen.db")
    d.log_alert(_alert("sh000688", "科创50", 0.2856, 2, "index_move",
                       "科创50 涨跌幅 +2.86%", D18), ts=D18.timestamp())
    d.log_alert(_alert("sz399006", "创业板指", 0.2201, 1, "index_move",
                       "创业板指 涨跌幅 +2.20%", D18), ts=D18.timestamp() + 60)
    d.log_alert(_alert("算力租赁", "算力租赁", -0.71, 2, "sector_cluster",
                       "3 条新闻情绪一致", D17), ts=D17.timestamp())
    yield d
    d.close()


@pytest.fixture
def client(monkeypatch, dedup):
    """独立 app（只挂舆情路由）+ 打开库走 tmp 实例（不碰生产库）。"""
    @contextmanager
    def _open():
        yield dedup            # 不 close：fixture 负责收尾
    monkeypatch.setattr(sa, "_open_dedup", _open)
    app = FastAPI()
    app.include_router(sa.router)
    return TestClient(app)


# ── 1. store: alerts_between ────────────────────────────────────────

class TestAlertsBetween:
    def test_区间端点包含在内且按时间倒序(self, dedup):
        s = dt.datetime(2026, 9, 18, 0, 0, 0).timestamp()
        e = dt.datetime(2026, 9, 18, 23, 59, 59).timestamp()
        rows = dedup.alerts_between(s, e)
        assert len(rows) == 2
        assert rows[0]["ts"] > rows[1]["ts"], "必须按 ts 倒序（最新在前）"

    def test_区间两端都算闭区间(self, dedup):
        ts = D18.timestamp()
        assert len(dedup.alerts_between(ts, ts)) == 1, "单点区间应含该点"

    def test_跨天区间拿到全部(self, dedup):
        rows = dedup.alerts_between(D17.timestamp(), D18.timestamp() + 999)
        assert len(rows) == 3

    def test_空区间返空(self, dedup):
        assert dedup.alerts_between(D17.timestamp() - 100000, D17.timestamp() - 1) == []

    def test_按规则筛(self, dedup):
        s, e = D17.timestamp(), D18.timestamp() + 999
        assert len(dedup.alerts_between(s, e, rule="index_move")) == 2
        assert len(dedup.alerts_between(s, e, rule="sector_cluster")) == 1
        assert dedup.alerts_between(s, e, rule="volume_anomaly") == []

    def test_按标的筛(self, dedup):
        s, e = D17.timestamp(), D18.timestamp() + 999
        rows = dedup.alerts_between(s, e, code="sh000688")
        assert len(rows) == 1 and rows[0]["name"] == "科创50"

    def test_limit_截断取最新(self, dedup):
        s, e = D17.timestamp(), D18.timestamp() + 999
        rows = dedup.alerts_between(s, e, limit=2)
        assert len(rows) == 2
        assert rows[0]["ts"] > rows[1]["ts"]

    def test_返回字段齐全(self, dedup):
        rows = dedup.alerts_between(D18.timestamp(), D18.timestamp() + 999)
        assert set(rows[0]) == {"ts", "code", "name", "polarity", "strength",
                                "rule", "evidence"}

    def test_库不可用时降级空不抛(self, tmp_path):
        d = NewsDedup(db_path=tmp_path / "x.db")
        d.close()
        d._conn = None                      # 模拟连接已断
        assert d.alerts_between(0, 9_999_999_999) == []


# ── 2. API 端点 ─────────────────────────────────────────────────────

class TestAlertsEndpoint:
    def test_按日查询并带规则中文名(self, client):
        r = client.get("/api/sentiment/alerts", params={"date": "2026-09-18"})
        d = r.json()
        assert r.status_code == 200 and d["success"] is True
        assert d["total"] == 2
        assert d["bullish"] == 2 and d["bearish"] == 0
        assert d["items"][0]["rule_name"] == "大盘指数异动"
        assert d["items"][0]["date"] == "2026-09-18"
        assert "time" in d["items"][0]

    def test_按区间查询(self, client):
        d = client.get("/api/sentiment/alerts",
                       params={"start": "2026-09-17", "end": "2026-09-18"}).json()
        assert d["total"] == 3
        assert "~" in d["range"]

    def test_规则筛选(self, client):
        d = client.get("/api/sentiment/alerts",
                       params={"start": "2026-09-17", "end": "2026-09-18",
                               "rule": "sector_cluster"}).json()
        assert d["total"] == 1
        assert d["items"][0]["rule_name"] == "板块新闻密集"

    def test_标的筛选(self, client):
        d = client.get("/api/sentiment/alerts",
                       params={"start": "2026-09-17", "end": "2026-09-18",
                               "code": "sh000688"}).json()
        assert d["total"] == 1

    def test_坏日期不静默_返可读错误(self, client):
        d = client.get("/api/sentiment/alerts", params={"date": "2026/09/18"}).json()
        assert d["success"] is False and "YYYY-MM-DD" in d["error"]

    def test_库挂掉时降级空且给出note(self, monkeypatch):
        @contextmanager
        def _boom():
            raise RuntimeError("库文件被移走了")
            yield
        monkeypatch.setattr(sa, "_open_dedup", _boom)
        app = FastAPI()
        app.include_router(sa.router)
        d = TestClient(app).get("/api/sentiment/alerts",
                                params={"date": "2026-09-18"}).json()
        assert d["success"] is True and d["items"] == [] and d["total"] == 0
        assert "note" in d, "降级必须留下 note —— 不许静默空白"

    def test_rule_names_随响应下发(self, client):
        d = client.get("/api/sentiment/alerts", params={"date": "2026-09-18"}).json()
        assert set(d["rule_names"]) == {"stock_sentiment", "sector_cluster",
                                        "index_move", "volume_anomaly"}


class TestSummaryEndpoint:
    def test_按日与按规则计数(self, client):
        d = client.get("/api/sentiment/summary", params={"days": 365}).json()
        assert d["success"] is True and d["total"] == 3
        by_day = {x["date"]: x["count"] for x in d["by_day"]}
        assert by_day["2026-09-18"] == 2 and by_day["2026-09-17"] == 1
        by_rule = {x["rule"]: x["count"] for x in d["by_rule"]}
        assert by_rule["index_move"] == 2 and by_rule["sector_cluster"] == 1
        assert d["by_rule"][0]["name"], "规则必须带中文名"

    def test_标的_top_带净极性(self, client):
        d = client.get("/api/sentiment/summary", params={"days": 365}).json()
        top = {x["code"]: x for x in d["top_codes"]}
        assert top["sh000688"]["count"] == 1
        assert top["sh000688"]["net"] == pytest.approx(0.2856)
        assert top["算力租赁"]["net"] == pytest.approx(-0.71)

    def test_不返回任何评分字段(self, client):
        """口径纪律：本页是台账不是指标 —— 响应里不许出现 score/weight/total_score。"""
        d = client.get("/api/sentiment/summary", params={"days": 365}).json()
        assert not any(k in d for k in ("score", "weight", "total_score", "signal"))

    def test_超范围入参的形状必须可辨认(self, client):
        """`days` 超 `le` 上限 → FastAPI 422 且 body 是 {"detail": ...}。

        2026-09-20：本用例是**故意锁住这个形状**的 —— FastAPI 的 422 不是业务形状，
        前端若直接读 `d.success` 会得到 undefined，把"入参错"显示成"没数据"
        （2026-09-17 大盘箱线图 422 静默空白同源）。sentiment.js 的 `_get` 已把
        `detail` 提成可读错误；这条锁住"后端确实会 422"这一前提，免得哪天
        上限被悄悄放开、前端那段防护变成死代码而无人知。
        """
        r = client.get("/api/sentiment/summary", params={"days": 99999})
        assert r.status_code == 422
        assert "detail" in r.json()


class TestSgpjbgEndpoint:
    @pytest.fixture
    def reports_dir(self, monkeypatch, tmp_path):
        d = tmp_path / "reports"
        d.mkdir()
        (d / "sgpjbg_研究雷达周报_2026-09-20.md").write_text("# 周报\n本周 404 篇",
                                                            encoding="utf-8")
        (d / "sgpjbg_研究雷达周报_2026-09-13.md").write_text("# 旧周报", encoding="utf-8")
        (d / "随便一个文件.md").write_text("x", encoding="utf-8")
        monkeypatch.setattr(sa, "_REPORTS_DIR", d)
        return d

    def test_列周报只认白名单且按日期倒序(self, client, reports_dir, monkeypatch):
        monkeypatch.setattr("brain.sgpjbg_radar.recent_reports", lambda **kw: [])
        d = client.get("/api/sentiment/sgpjbg").json()
        assert d["success"] is True
        assert [r["date"] for r in d["reports"]] == ["2026-09-20", "2026-09-13"]
        assert all(r["name"].endswith(".md") for r in d["reports"])

    def test_取正文(self, client, reports_dir):
        d = client.get("/api/sentiment/sgpjbg",
                       params={"name": "sgpjbg_研究雷达周报_2026-09-20.md"}).json()
        assert d["success"] is True and "本周 404 篇" in d["markdown"]

    def test_非白名单文件名被拒(self, client, reports_dir):
        d = client.get("/api/sentiment/sgpjbg", params={"name": "随便一个文件.md"}).json()
        assert d["success"] is False and "不合法" in d["error"]

    def test_路径穿越被拒(self, client, reports_dir):
        for bad in ("../config/config.yaml", "..\\config\\config.yaml",
                    "sgpjbg_研究雷达周报_../../x.md"):
            d = client.get("/api/sentiment/sgpjbg", params={"name": bad}).json()
            assert d["success"] is False, f"路径穿越未被拦: {bad}"

    def test_近期报告透传(self, client, reports_dir, monkeypatch):
        monkeypatch.setattr("brain.sgpjbg_radar.recent_reports",
                            lambda **kw: [{"title": "某报告", "heat": 53}])
        d = client.get("/api/sentiment/sgpjbg").json()
        assert d["recent"][0]["title"] == "某报告"


# ── 3. 铁律 1 与规则名单 ────────────────────────────────────────────

def test_ast_sentiment_api_has_no_trade_import():
    """sentiment_api.py 零 import trade（自包含守卫，与 acceptance 的名单互补）。"""
    with open(sa.__file__, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert not a.name.startswith("trade"), f"铁律 1 破坏: import {a.name}"
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("trade"), \
                f"铁律 1 破坏: from {node.module}"


def test_ast_sentiment_api_is_read_only():
    """只读守卫：本模块不许出现任何写端点装饰器。"""
    src = Path(sa.__file__).read_text(encoding="utf-8")
    assert ".post(" not in src and ".put(" not in src and ".delete(" not in src, \
        "舆情页是只读台账，不许有写端点"
    assert ".get(" in src


def test_rule_names_covers_all_four_rules():
    """RULE_NAMES 键全集 = 4 条规则（防前后端出现第二份规则名）。"""
    from brain.alert_rules import RULE_NAMES
    assert set(RULE_NAMES) == {"stock_sentiment", "sector_cluster",
                               "index_move", "volume_anomaly"}
    assert all(v.strip() for v in RULE_NAMES.values()), "中文名不许为空"
