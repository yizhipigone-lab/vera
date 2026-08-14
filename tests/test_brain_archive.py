# -*- coding: utf-8 -*-
"""brain/archive.py 对话归档测试 — 全程 tmp 目录, 不碰真实 vault。

覆盖: 首问建文件(frontmatter+问答段)、多轮追加、channel→文件名映射复用、
失败/低置信/警告标注、文件名清洗、归档异常松耦合(返 None 不抛)、
ask_brain 集成归档(含 archive=False 豁免)。
"""
from __future__ import annotations

import asyncio

import pytest

import brain.archive as arch
import brain.claude_cli as cli


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(arch, "ARCHIVE_DIR", tmp_path / "arch")
    monkeypatch.setattr(arch, "_MAP_PATH", tmp_path / "map.json")
    return tmp_path


_OK = {"answer": "答: 据 `kg/graph.db` 共 3 条", "success": True,
       "low_confidence": False, "warnings": []}


class TestArchiveExchange:
    def test_first_creates_file_with_frontmatter(self, tmp_path):
        f = arch.archive_exchange("c1", "今天A股为什么跌?", _OK)
        text = f.read_text(encoding="utf-8")
        assert 'conv: "c1"' in text and "created:" in text
        assert "你问" in text and "今天A股为什么跌?" in text
        assert "大脑答" in text and "kg/graph.db" in text

    def test_second_appends_same_file(self, tmp_path):
        arch.archive_exchange("c1", "第一问", _OK)
        f = arch.archive_exchange("c1", "第二问", _OK)
        text = f.read_text(encoding="utf-8")
        assert text.count("你问") == 2 and "第一问" in text and "第二问" in text
        # 同 channel 复用同一文件名 (映射)
        assert len(list((tmp_path / "arch").glob("*.md"))) == 1

    def test_filename_uses_first_question(self, tmp_path):
        f = arch.archive_exchange("c1", "持仓哪些在AVOID档?", _OK)
        assert f.name.startswith("20") and "持仓哪些在AVOID档" in f.name

    def test_filename_sanitized(self, tmp_path):
        f = arch.archive_exchange("c1", 'a/b\\c:d*e?f"g<h>i|j\n换行', _OK)
        assert "/" not in f.name and ":" not in f.name and "\n" not in f.name

    def test_failure_and_lowconf_and_warnings_tagged(self, tmp_path):
        f1 = arch.archive_exchange("c1", "q", {**_OK, "success": False,
                                               "answer": "大脑超时"})
        assert "[失败]" in f1.read_text(encoding="utf-8")
        f2 = arch.archive_exchange("c2", "q", {**_OK, "low_confidence": True,
                                               "warnings": ["provider 不是 DeepSeek"]})
        text = f2.read_text(encoding="utf-8")
        assert "[低置信]" in text and "⚠ provider 不是 DeepSeek" in text

    def test_exception_returns_none_no_raise(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("我是文件不是目录")  # mkdir 必失败
        assert arch.archive_exchange("c1", "q", _OK, out_dir=blocker / "sub") is None


class TestAutoLinks:
    """第二大脑: 归档即扫描, 提到的行业/公司自动打 [[]] 双链。"""

    def _fake_cands(self, monkeypatch):
        monkeypatch.setattr(arch, "_CANDIDATES_CACHE", None)
        monkeypatch.setattr(arch, "_link_candidates", lambda db_path=None: [
            ("半导体", "industry_tdx_881001.SH", "半导体", "行业(通达信)"),
            ("农林牧渔", "industry_110000", "农林牧渔", "行业(产业链)"),
            ("宁德时代", "company_300750", "宁德时代", "公司"),
            ("300750", "company_300750", "宁德时代", "公司"),
            ("202607", "company_202607", "假公司", "公司"),  # 不应出现(前缀过滤在加载层, 这里模拟)
        ])

    def test_links_created_on_archive(self, tmp_path, monkeypatch):
        self._fake_cands(monkeypatch)
        f = arch.archive_exchange("c1", "半导体和宁德时代 300750 怎么看", _OK)
        text = f.read_text(encoding="utf-8")
        assert "## 关联节点" in text
        assert "[[industry_tdx_881001.SH|半导体]]" in text
        assert "[[company_300750|宁德时代]]" in text
        assert text.count("[[company_300750") == 1  # 名称和代码命中同一文件, 只链一次

    def test_links_refresh_idempotent_on_append(self, tmp_path, monkeypatch):
        self._fake_cands(monkeypatch)
        f = arch.archive_exchange("c1", "半导体如何", _OK)
        arch.archive_exchange("c1", "那农林牧渔呢", _OK)
        text = f.read_text(encoding="utf-8")
        assert text.count("## 关联节点") == 1          # 不重复追加段
        assert "[[industry_110000|农林牧渔]]" in text   # 新提到的节点补进来
        assert "[[industry_tdx_881001.SH|半导体]]" in text  # 旧的还在
        assert text.count("你问") == 2                     # 正文追加不受影响

    def test_no_mention_no_link_section(self, tmp_path, monkeypatch):
        self._fake_cands(monkeypatch)
        f = arch.archive_exchange("c1", "今天天气怎么样", _OK)
        assert "## 关联节点" not in f.read_text(encoding="utf-8")

    def test_candidates_missing_db_degrades(self, tmp_path, monkeypatch):
        monkeypatch.setattr(arch, "_CANDIDATES_CACHE", None)
        cands = arch._link_candidates(tmp_path / "nope.db")
        assert cands == []  # db 缺失 → 空候选, 不抛


class TestAskBrainIntegration:
    def test_ask_brain_archives_and_optout(self, tmp_path, monkeypatch):
        async def fake_impl(question, session_id, timeout, max_turns, channel,
                            on_line=None, system=None):
            return dict(_OK)
        monkeypatch.setattr(cli, "_ask_brain_impl", fake_impl)

        asyncio.run(cli.ask_brain("集成问", channel="int1"))
        files = list((tmp_path / "arch").glob("*.md"))
        assert len(files) == 1 and "集成问" in files[0].read_text(encoding="utf-8")

        asyncio.run(cli.ask_brain("豁免问", channel="int2", archive=False))
        files = list((tmp_path / "arch").glob("*.md"))
        assert len(files) == 1  # 没新增
