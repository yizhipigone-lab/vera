"""tests/brain/test_fastpath.py — 个股诊断快路径护栏测试。

mock 取数与 ask_brain，不碰真实网络 / claude CLI。覆盖：
意图识别边界 / 快路径成文调用 / 取数异常回落 / API 端点优先走快路径。
"""
from __future__ import annotations

import asyncio

import brain.claude_cli as brain_cli
from brain import data_tools, fastpath, market_panel, prompts


def _run(coro):
    return asyncio.run(coro)


class TestMatchStockDiagnosis:
    def test_代码加诊断词命中(self):
        assert fastpath.match_stock_diagnosis("看看 300750 怎么样") == "300750"
        assert fastpath.match_stock_diagnosis("帮我分析下600000") == "600000"
        assert fastpath.match_stock_diagnosis("300687 还能涨吗") == "300687"

    def test_无诊断词不命中(self):
        assert fastpath.match_stock_diagnosis("300750") is None
        assert fastpath.match_stock_diagnosis("300750 公告") is None

    def test_无代码不命中(self):
        assert fastpath.match_stock_diagnosis("今日大盘怎么样") is None
        assert fastpath.match_stock_diagnosis("帮我分析一下固态电池板块") is None

    def test_YYYYMM不误判(self):
        assert fastpath.match_stock_diagnosis("202608 行情怎么样") is None


class TestResolveStock:
    """名字→代码反查（2026-08-13 提速三轮 ①）。mock kg 全量公司，不碰真实 DB。"""

    def test_名字反查命中(self, monkeypatch):
        monkeypatch.setattr(data_tools, "_kg_all_companies",
                            lambda: [("300750", "宁德时代"), ("600000", "浦发银行")])
        assert fastpath.resolve_stock("宁德时代怎么样") == "300750"
        assert fastpath.resolve_stock("浦发银行还能涨吗") == "600000"

    def test_名字无诊断词不命中(self, monkeypatch):
        monkeypatch.setattr(data_tools, "_kg_all_companies",
                            lambda: [("300750", "宁德时代")])
        assert fastpath.resolve_stock("宁德时代") is None

    def test_两字名保守漏判(self, monkeypatch):
        monkeypatch.setattr(data_tools, "_kg_all_companies",
                            lambda: [("000528", "柳工")])
        assert fastpath.resolve_stock("柳工怎么样") is None

    def test_代码优先于名字(self, monkeypatch):
        monkeypatch.setattr(data_tools, "_kg_all_companies",
                            lambda: [("300750", "宁德时代")])
        assert fastpath.resolve_stock("看看 300750 怎么样") == "300750"

    def test_反查失败返None(self, monkeypatch):
        monkeypatch.setattr(data_tools, "_kg_all_companies", lambda: [])
        assert fastpath.resolve_stock("宁德时代怎么样") is None


class TestTryStockDiagnosis:
    def _mock_sources(self, monkeypatch, hits=None):
        monkeypatch.setattr(data_tools, "stock_diagnosis", lambda c: "PACK")
        monkeypatch.setattr(data_tools, "_kg_company_name", lambda c: "宁德时代")
        # 2026-09-20 换源：软舆情腿由 brain.search_web 改为
        # policy_pipeline.sources.news_search.fetch_news（东财个股新闻，akshare）。
        # 桩点必须跟着搬 —— fastpath 是在函数内 `from ... import fetch_news`，
        # 故 patch 模块属性即可生效。
        monkeypatch.setattr(
            "policy_pipeline.sources.news_search.fetch_news",
            lambda *a, **kw: hits if hits is not None else [
                {"title": "t", "text": "s", "url": "u", "date": "2026-09-19"}])

    def _mock_brain(self, monkeypatch, capture):
        async def fake_ask(question, **kw):
            capture.update({"question": question, **kw})
            return {"answer": "成文", "success": True, "warnings": [],
                    "low_confidence": False, "session_id": "s-1"}
        monkeypatch.setattr(brain_cli, "ask_brain", fake_ask)

    def test_意图不匹配返None(self, monkeypatch):
        def _boom(c):
            raise AssertionError("不应调取数")
        monkeypatch.setattr(data_tools, "stock_diagnosis", _boom)
        assert _run(fastpath.try_stock_diagnosis("今日热点有哪些")) is None

    def test_快路径单次成文(self, monkeypatch):
        self._mock_sources(monkeypatch)
        cap = {}
        self._mock_brain(monkeypatch, cap)
        r = _run(fastpath.try_stock_diagnosis("看看 300750 怎么样",
                                              channel="research_tab_c1"))
        assert r["success"] is True
        assert "快路径" in r["warnings"][-1]
        # prompt 含数据包 + 软舆情 + 成文纪律
        assert "PACK" in cap["question"]
        assert "软舆情" in cap["question"] and "宁德时代" in cap["question"]
        assert "<counter_evidence>" in cap["question"]
        assert "不要再调用任何工具" in cap["question"]
        # 单次成文参数 + channel 透传
        assert cap["max_turns"] == 4
        assert cap["channel"] == "research_tab_c1"

    def test_软舆情空结果按纪律标注(self, monkeypatch):
        self._mock_sources(monkeypatch, hits=[])
        cap = {}
        self._mock_brain(monkeypatch, cap)
        _run(fastpath.try_stock_diagnosis("看看 300750 怎么样"))
        assert "待核实" in cap["question"]

    def test_取数异常回落agent_loop(self, monkeypatch):
        def _boom(c):
            raise RuntimeError("取数炸了")
        monkeypatch.setattr(data_tools, "stock_diagnosis", _boom)
        monkeypatch.setattr(data_tools, "_kg_company_name", lambda c: None)
        r = _run(fastpath.try_stock_diagnosis("看看 300750 怎么样"))
        assert r is None  # None = 调用方回落原 agent loop

    def test_快路径用瘦身prompt(self, monkeypatch):
        """②：成文时传 SLIM_SYSTEM_PROMPT，且安全红线/反证要求逐字保留。"""
        self._mock_sources(monkeypatch)
        cap = {}
        self._mock_brain(monkeypatch, cap)
        _run(fastpath.try_stock_diagnosis("看看 300750 怎么样"))
        assert cap["system"] == prompts.SLIM_SYSTEM_PROMPT
        # 瘦身 prompt 不能误删三件套：数据非指令红线 / 反证段 / 只读禁区
        assert "不是【指令】" in cap["system"]
        assert "<counter_evidence>" in cap["system"]
        assert "trade/" in cap["system"] and "KILL" in cap["system"]


class TestMatchMarketBrief:
    """大盘/盘面意图识别（④）。纯函数，无 IO。"""

    def test_盘面词命中(self):
        assert fastpath.match_market_brief("今天大盘怎么样") is True
        assert fastpath.match_market_brief("盘面现在怎么样") is True
        assert fastpath.match_market_brief("今日涨停家数多少") is True

    def test_无盘面词不命中(self):
        assert fastpath.match_market_brief("宁德时代怎么样") is False
        assert fastpath.match_market_brief("持仓有哪些") is False

    def test_带代码不归大盘(self):
        assert fastpath.match_market_brief("300750 今天行情怎么样") is False

    def test_指数词不误判(self):
        # "指数"刻意不进盘面词表：中证500这类具体指数快照里没有，答了反错
        assert fastpath.match_market_brief("中证500指数怎么样") is False

    def test_歧义行情词不误判(self):
        # "今天行情"是"XXX今天行情怎么样"的个股问法，刻意不进盘面词表（保守漏判）
        assert fastpath.match_market_brief("今天行情怎么样") is False


class TestTryMarketBrief:
    def _mock(self, monkeypatch, snap="SNAP", capture=None):
        # 2026-09-15: fastpath 已改直引 brain.market_panel (data_tools 兼容转发删)
        monkeypatch.setattr(market_panel, "market_snapshot", lambda: snap)
        async def fake_ask(question, **kw):
            if capture is not None:
                capture.update({"question": question, **kw})
            return {"answer": "盘面", "success": True, "warnings": [],
                    "low_confidence": False, "session_id": "s-1"}
        monkeypatch.setattr(brain_cli, "ask_brain", fake_ask)

    def test_大盘命中单次成文(self, monkeypatch):
        cap = {}
        self._mock(monkeypatch, capture=cap)
        r = _run(fastpath.try_market_brief("今天大盘怎么样",
                                            channel="research_tab_c1"))
        assert r["success"] is True
        assert "大盘" in r["warnings"][-1]
        assert "SNAP" in cap["question"]
        assert "<counter_evidence>" in cap["question"]
        # 成文模板已附进 prompt（六段结构）
        assert "成文模板" in cap["question"] and "一句话结论" in cap["question"]
        assert cap["max_turns"] == 4
        assert cap["system"] == prompts.SLIM_SYSTEM_PROMPT
        assert cap["channel"] == "research_tab_c1"

    def test_意图不匹配返None(self, monkeypatch):
        def _boom():
            raise AssertionError("不应取数")
        monkeypatch.setattr(market_panel, "market_snapshot", _boom)
        assert _run(fastpath.try_market_brief("宁德时代怎么样")) is None

    def test_取数异常回落(self, monkeypatch):
        def _boom():
            raise RuntimeError("取数炸了")
        monkeypatch.setattr(market_panel, "market_snapshot", _boom)
        assert _run(fastpath.try_market_brief("今天大盘怎么样")) is None


class TestApiPrefersFastpath:
    """research_api 端点：快路径命中就用快路径，不命中回落 ask_brain。"""

    def _client(self):
        from fastapi.testclient import TestClient
        from server import app
        return TestClient(app)

    def test_chat命中走快路径(self, monkeypatch):
        async def fake_fast(question, channel="default", on_line=None,
                            timeout=300):
            return {"answer": "快", "success": True, "warnings": [],
                    "low_confidence": False, "session_id": "s-1"}
        async def _no_slow(*a, **kw):
            raise AssertionError("不应回落 agent loop")
        monkeypatch.setattr(fastpath, "try_stock_diagnosis", fake_fast)
        monkeypatch.setattr(brain_cli, "ask_brain", _no_slow)
        r = self._client().post("/api/research/chat",
                                json={"question": "看看 300750 怎么样"})
        assert r.json()["answer"] == "快"

    def test_chat未命中回落(self, monkeypatch):
        async def _no_fast(question, channel="default", on_line=None,
                           timeout=300):
            return None
        async def fake_ask(question, **kw):
            return {"answer": "慢", "success": True, "warnings": [],
                    "low_confidence": False, "session_id": "s-1"}
        monkeypatch.setattr(fastpath, "try_stock_diagnosis", _no_fast)
        monkeypatch.setattr(brain_cli, "ask_brain", fake_ask)
        r = self._client().post("/api/research/chat",
                                json={"question": "持仓有哪些"})
        assert r.json()["answer"] == "慢"

    def test_chat大盘命中走快路径(self, monkeypatch):
        """个股不命中 → 大盘命中 → 用大盘快路径，不回落 agent loop。"""
        async def _no_stock(question, channel="default", on_line=None,
                            timeout=300):
            return None
        async def fake_market(question, channel="default", on_line=None,
                              timeout=300):
            return {"answer": "盘面", "success": True, "warnings": [],
                    "low_confidence": False, "session_id": "s-1"}
        async def _no_slow(*a, **kw):
            raise AssertionError("不应回落 agent loop")
        monkeypatch.setattr(fastpath, "try_stock_diagnosis", _no_stock)
        monkeypatch.setattr(fastpath, "try_market_brief", fake_market)
        monkeypatch.setattr(brain_cli, "ask_brain", _no_slow)
        r = self._client().post("/api/research/chat",
                                json={"question": "今天大盘怎么样"})
        assert r.json()["answer"] == "盘面"
