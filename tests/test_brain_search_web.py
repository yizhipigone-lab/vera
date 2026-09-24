"""tests/test_brain_search_web.py — brain/search_web.py 单元测试。"""
from __future__ import annotations

import pytest

import brain.search_web as sw


@pytest.fixture(autouse=True)
def _禁真实网络(monkeypatch):
    """兜底：任何测试忘了 mock 后端时，调用直接炸，绝不落到真实网络。

    各测试自己的 monkeypatch.setattr 会覆盖这里的默认桩（后设的生效）。
    """
    def _boom(*a, **kw):
        raise AssertionError("测试禁止真实联网：请 mock _search_with_backend")
    monkeypatch.setattr(sw, "_search_with_backend", _boom)
    monkeypatch.setattr(sw, "_search_baidu", _boom)
    monkeypatch.setattr(sw, "fetch_url", _boom)
    sw.clear_search_cache()  # 短缓存隔离：防上一测试的缓存污染本测试的 mock


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
        def _mock_search(query, max_r, backend, timelimit=None, **kw):
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
        def _mock_search(query, max_r, backend, timelimit=None, **kw):
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
        def _mock_search(query, max_r, backend, timelimit=None, **kw):
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


class TestSearchCache:
    """短缓存（2026-08-13 提速三轮 ③）：命中不再调后端；空/弱结果不缓存。"""

    def _no_rate_limit(self, monkeypatch):
        monkeypatch.setattr(sw, "_rate_limit", lambda: None)  # 测试不走 2s 限流

    def test_命中缓存不再调后端(self, monkeypatch):
        self._no_rate_limit(monkeypatch)
        calls = []
        def _mock_search(query, max_r, backend, timelimit=None, **kw):
            calls.append(backend)
            return [{"title": "x", "url": "http://x", "snippet": "s", "source": "mock"}]
        monkeypatch.setattr(sw, "_search_with_backend", _mock_search)
        sw.search_web("缓存测试", backend="bing")
        sw.search_web("缓存测试", backend="bing")
        assert len(calls) == 1  # 第二次命中缓存，跳过限流+后端

    def test_空结果不缓存(self, monkeypatch):
        self._no_rate_limit(monkeypatch)
        calls = []
        def _mock_search(query, max_r, backend, timelimit=None, **kw):
            calls.append(backend)
            return []
        monkeypatch.setattr(sw, "_search_with_backend", _mock_search)
        sw.search_web("空结果测试", backend="bing")
        sw.search_web("空结果测试", backend="bing")
        assert len(calls) == 2  # 空结果不缓存，每次都重试（不掩盖瞬时失败）

    def test_不同参数不串缓存(self, monkeypatch):
        self._no_rate_limit(monkeypatch)
        def _mock_search(query, max_r, backend, timelimit=None, **kw):
            return [{"title": query, "url": "http://x", "snippet": "s", "source": "mock"}]
        monkeypatch.setattr(sw, "_search_with_backend", _mock_search)
        r1 = sw.search_web("A", backend="bing")
        r2 = sw.search_web("B", backend="bing")
        assert r1[0]["title"] == "A"
        assert r2[0]["title"] == "B"
