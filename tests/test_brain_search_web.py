"""tests/test_brain_search_web.py — brain/search_web.py 单元测试。"""
from __future__ import annotations

import brain.search_web as sw


class TestFormatResults:
    def test_格式契约(self):
        """构造结果列表，断言输出含序号/URL/摘要。"""
        results = [
            {"title": "测试标题", "url": "https://example.com/1",
             "snippet": "摘要内容", "source": "bing"},
        ]
        out = sw.format_results(results)
        assert "[1]" in out
        assert "测试标题" in out
        assert "https://example.com/1" in out
        assert "摘要内容" in out
        assert "(bing)" in out

    def test_空结果(self):
        assert sw.format_results([]) == ""


class TestSearchWeb:
    def test_异常降级(self, monkeypatch):
        """ddgs 抛异常时 search_web 返 [] 不抛。"""
        def _fail(*a, **kw):
            raise RuntimeError("模拟网络故障")
        monkeypatch.setattr(sw, "_search_with_backend", _fail)
        results = sw.search_web("test")
        assert results == []

    def test_结果透传(self, monkeypatch):
        """_search_with_backend 返回结果被 search_web 正确透传。"""
        def _mock_search(*a, **kw):
            return [{"title": "x", "url": "http://x",
                     "snippet": "安全内容", "source": "mock"}]
        monkeypatch.setattr(sw, "_search_with_backend", _mock_search)
        results = sw.search_web("test", backend="bing")
        assert len(results) == 1
        assert results[0]["snippet"] == "安全内容"
        assert results[0]["source"] == "mock"

    def test_backend_首选可用(self, monkeypatch):
        """第一个 backend 有结果时直接返回，不试后面的。"""
        calls = []
        def _mock_search(query, max_r, backend):
            calls.append(backend)
            if backend == "baidu":
                return [{"title": "ok", "url": "http://x", "snippet": "y", "source": backend}]
            return []
        monkeypatch.setattr(sw, "_search_with_backend", _mock_search)
        results = sw.search_web("test", backend="auto")
        assert len(results) == 1
        assert results[0]["source"] == "baidu"
        assert calls == ["baidu"]  # 只调了百度，没调后面的

    def test_首选失败回退(self, monkeypatch):
        """baidu 空 → bing 空 → duckduckgo 有结果。"""
        calls = []
        def _mock_search(query, max_r, backend):
            calls.append(backend)
            if backend == "duckduckgo":
                return [{"title": "ddg", "url": "http://x", "snippet": "y", "source": backend}]
            return []
        monkeypatch.setattr(sw, "_search_with_backend", _mock_search)
        results = sw.search_web("test", backend="auto")
        assert len(results) == 1
        assert results[0]["source"] == "duckduckgo"
        assert calls == ["baidu", "bing", "duckduckgo"]

    def test_无摘要结果回退(self, monkeypatch):
        """baidu 只抽到标题（snippet 全空）→ 视为无效，回退到带摘要的 bing。

        2026-08-01 审计修复：标题-only 是最弱数据源，LLM 拿不到摘要无法基于内容作答。
        """
        calls = []
        def _mock_search(query, max_r, backend):
            calls.append(backend)
            if backend == "baidu":
                return [{"title": "只有标题", "url": "http://x", "snippet": "", "source": "baidu"}]
            if backend == "bing":
                return [{"title": "bing", "url": "http://y", "snippet": "有摘要", "source": "bing"}]
            return []
        monkeypatch.setattr(sw, "_search_with_backend", _mock_search)
        results = sw.search_web("test", backend="auto")
        assert len(results) == 1
        assert results[0]["source"] == "bing"
        assert calls == ["baidu", "bing"]
