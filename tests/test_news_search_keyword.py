# -*- coding: utf-8 -*-
"""tests/test_news_search_keyword.py — 东财关键词新闻接入 + search_web 降级 (2026-09-20)。

**本文件对应哪件事**：`brain/search_web.py` 的三个 backend 全部失效（baidu snippet
恒空被自家判据拒 / bing 已从 ddgs 移除 / 全程无代理），导致 VERA 三条链路的功能缺失。
用户 2026-09-20 拍板：**接受降级** —— 不用 SearXNG、不修 ddgs，改用**已在用**的 akshare。

**关键前提（本文件为何这样写）**：`data_tools.py` 旧注释记着
「东财个股新闻 stock_news_em 接口崩溃 ArrowInvalid，砍」（2026-08-13）。
但 2026-09-20 在 akshare **1.18.94** 下实测该接口**完全可用**，且**能吃概念词**
（算力租赁 / CPO / 半导体 各返 10 行）—— 旧结论已被版本升级作废。
这与 `ddgs 9.16 把 bing 拿掉` 是同一类病：**版本漂移让旧结论过期，却写成了永久决定**。

mock 边界（TDD 纪律：mock 只隔离外部 IO）：只桩 `_import_akshare`（网络/第三方库），
fetcher 的选取与回退逻辑走真代码。
"""
import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from policy_pipeline.sources import news_search as ns  # noqa: E402


def _imports_module(path: Path, modname: str) -> bool:
    """该文件是否**真的 import** 了 modname（AST 判定，不看注释/docstring）。

    按项目既有纪律：**物理隔离断言用 AST，不用字符串匹配** ——
    "docstring 里讨论 'import trade' 会被字符串匹配误判"是 v1-3 亲历教训
    （见 tests/brain/test_sentiment_acceptance.py 文件头）。
    本条同理：我们要禁的是"又去用它"，不是"在注释里说明为什么不用它"。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == modname or a.name.startswith(modname + ".")
                   for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            m = node.module or ""
            if m == modname or m.startswith(modname + "."):
                return True
    return False


class _FakeAk:
    """伪 akshare：只实现被用到的两个接口。

    stock_news_em 收到 symbol 时返 10 行（模拟实测：概念词也能吃）；
    stock_news_main_cx 返 3 行财新（老源，用于验证回退）。
    """

    def __init__(self, em_fail=False, em_empty=False):
        self.em_fail = em_fail
        self.em_empty = em_empty
        self.em_calls: list[str] = []
        self.cx_calls = 0

    def stock_news_em(self, symbol):
        self.em_calls.append(symbol)
        if self.em_fail:
            raise RuntimeError("模拟 ArrowInvalid")
        if self.em_empty:
            import pandas as pd
            return pd.DataFrame(columns=["关键词", "新闻标题", "新闻内容", "发布时间",
                                         "文章来源", "新闻链接"])
        import pandas as pd
        return pd.DataFrame([
            {"关键词": symbol, "新闻标题": f"{symbol} 概念异动",
             "新闻内容": f"关于 {symbol} 的详细内容", "发布时间": "2026-09-20 08:33:18",
             "文章来源": "财联社", "新闻链接": "http://example.com/a"},
            {"关键词": symbol, "新闻标题": f"{symbol} 第二篇",
             "新闻内容": "内容2", "发布时间": "2026-09-19 10:00:00",
             "文章来源": "证券时报", "新闻链接": "http://example.com/b"},
        ])

    def stock_news_main_cx(self):
        self.cx_calls += 1
        import pandas as pd
        return pd.DataFrame([
            {"tag": "要闻", "summary": "财新摘要甲", "url": "https://x.com/2026-09-18/1"},
            {"tag": "要闻", "summary": "财新摘要乙", "url": "https://x.com/2026-09-18/2"},
        ])


@pytest.fixture
def fake_ak(monkeypatch):
    def _mk(em_fail=False, em_empty=False):
        ak = _FakeAk(em_fail=em_fail, em_empty=em_empty)
        monkeypatch.setattr(ns, "_import_akshare", lambda: ak)
        monkeypatch.setattr(ns, "_HAS_AKSHARE", True)
        return ak
    return _mk


# ── 1. 关键词非空 → 走东财个股/概念新闻 ─────────────────────────────

def test_关键词非空时命中东财新闻(fake_ak):
    ak = fake_ak()
    out = ns.fetch_news("算力租赁", limit=5)
    assert ak.em_calls == ["算力租赁"], "关键词查询必须打东财 stock_news_em"
    assert len(out) == 2
    assert out[0]["title"] == "算力租赁 概念异动"
    assert out[0]["url"] == "http://example.com/a"
    assert "详细内容" in out[0]["text"]


def test_返回字段形状统一(fake_ak):
    """fetch_news 的对外契约是 {title,url,date,text}，新源不许破形状。"""
    fake_ak()
    out = ns.fetch_news("CPO", limit=5)
    assert set(out[0]) == {"title", "url", "date", "text"}


def test_日期取自发布时间而不是从_url_抠(fake_ak):
    """东财有明确的 发布时间 列，比从 URL 抠更可靠。"""
    fake_ak()
    out = ns.fetch_news("半导体", limit=5)
    assert out[0]["date"] == "2026-09-20"


def test_正文过_sanitize_打边界标记(fake_ak):
    """抓回文本是不可信外部内容 —— 必须带防注入边界标记。"""
    fake_ak()
    out = ns.fetch_news("算力租赁", limit=5)
    assert "untrusted" in out[0]["text"], "外部文本必须过 sanitize_untrusted"


# ── 2. 关键词为空 → 不碰东财，保持原行为（这是兼容性锁）────────────

def test_关键词为空时不调东财且仍返财新(fake_ak):
    """`sentiment_pipeline` 源1 用 `fetch_news(limit=15)` 无关键词取财新 —— 行为不许变。"""
    ak = fake_ak()
    out = ns.fetch_news(limit=15)
    assert ak.em_calls == [], "无关键词时不该调用 stock_news_em（它必须给 symbol）"
    assert ak.cx_calls == 1, "应回落到财新要闻"
    assert len(out) == 2 and out[0]["title"].startswith("[要闻]")


# ── 3. 东财挂了 → 回退老源（fail-open，不许把整条链带崩）───────────

def test_东财异常时回退老源并拿得到结果(fake_ak):
    """关键词取"财新"（伪财新数据里含它）—— 东财挂后老源能按词过滤命中。"""
    ak = fake_ak(em_fail=True)
    out = ns.fetch_news("财新", limit=5)
    assert ak.cx_calls == 1, "东财异常后必须继续尝试老源"
    assert out, "老源有该词时不该返空"
    assert out[0]["title"].startswith("[要闻]")


def test_东财返空时回退老源(fake_ak):
    ak = fake_ak(em_empty=True)
    out = ns.fetch_news("财新", limit=5)
    assert out and ak.cx_calls == 1


def test_东财异常且老源也无该词时返空_不编造(fake_ak):
    """**这是本组最重要的一条**：谁都没有这个词就返空。

    降级不等于编 —— 老源的 fetcher 按关键词过滤，命中不了就是命中不了。
    """
    ak = fake_ak(em_fail=True)
    assert ns.fetch_news("这个关键词谁都没有", limit=5) == []
    assert ak.cx_calls == 1, "仍然应该尝试过老源"


def test_akshare_缺失时返空不抛(monkeypatch):
    monkeypatch.setattr(ns, "_import_akshare", lambda: None)
    assert ns.fetch_news("任意", limit=5) == []
    assert ns.fetch_news(limit=5) == []


# ── 4. 两条消费腿必须真的不再依赖 search_web（降级契约，AST 级）────

def test_舆情扫描不再_import_search_web():
    """`sentiment_pipeline` 的关键词腿改走 akshare —— **不许再 import** search_web。

    源码级（AST）而非行为断言：这是**架构决定**（用户拍板接受降级），
    行为断言挡不住"以后有人又把它加回来"。用 AST 而非字符串匹配 ——
    否则本模块里"原用 brain.search_web，因三个 backend 全失效改走 akshare"
    这句**说明为什么不用它**的注释，会被自己误判成违规。
    """
    assert not _imports_module(ROOT / "brain" / "sentiment_pipeline.py",
                               "brain.search_web"), \
        "舆情扫描又 import search_web 了 —— 用户已拍板走 akshare，别再引回来"


def test_个股快路径不再_import_search_web():
    assert not _imports_module(ROOT / "brain" / "fastpath.py",
                               "brain.search_web"), \
        "个股诊断快路径又 import search_web 了 —— 用户已拍板走 akshare"
