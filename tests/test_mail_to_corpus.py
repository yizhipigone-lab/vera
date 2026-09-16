"""tests/test_mail_to_corpus.py — 邮箱外部研究产物 → RAG 语料（2026-09-17, M5）。

全部离线：自己造 `.eml`（内含 zip 附件），不碰真邮箱、不调 qqmail 技能。
覆盖计划书 §4.6 的落盘设计与 §12.4 的"技能缺失必须报错退出"。
"""
from __future__ import annotations

import importlib.util
import io
import sys
import zipfile
from email.message import EmailMessage
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "mail_to_corpus", ROOT / "tools" / "mail_to_corpus.py")
mtc = importlib.util.module_from_spec(spec)
sys.modules["mail_to_corpus"] = mtc
spec.loader.exec_module(mtc)


def _make_zip(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in members.items():
            zf.writestr(name, text)
    return buf.getvalue()


def _make_eml(path: Path, *, subject: str = "【A股研究产物包】测试",
              sender: str = "研究产物自动分发 <dcliuzh@qq.com>",
              date: str = "Sat, 12 Sep 2026 10:00:00 +0800",
              members: dict[str, str] | None = None) -> Path:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = "393328@qq.com"
    msg["Date"] = date
    msg.set_content("见附件")
    msg.add_attachment(_make_zip(members or {"01_面板/面板.md": "这是外部研报正文。" * 10}),
                       maintype="application", subtype="zip", filename="产物包.zip")
    path.write_bytes(msg.as_bytes())
    return path


class TestExtract:
    def test_only_md_is_kept(self, tmp_path):
        """§4.6.1：只要 .md —— html/py/json 是噪音或源码，一概不收。"""
        e = _make_eml(tmp_path / "a.eml", members={
            "报告.md": "有效内容" * 20,
            "报告.html": "<html>噪音</html>" * 20,
            "脚本.py": "print(1)" * 20,
            "数据.json": "{}" * 50,
        })
        items = mtc.extract_md_from_eml(e)
        assert [i["name"] for i in items] == ["报告.md"]

    def test_header_decoded_and_date_normalised(self, tmp_path):
        """主题是 MIME 编码、日期是 RFC2822 —— 都必须解码后再落盘。"""
        e = _make_eml(tmp_path / "b.eml",
                      subject="=?utf-8?b?44CQ56CU56m2?=", date="Sat, 12 Sep 2026 10:00:00 +0800")
        it = mtc.extract_md_from_eml(e)[0]
        assert it["date"] == "2026-09-12"
        assert "UTF" not in it["subject"] and "=?" not in it["subject"]

    def test_broken_eml_returns_empty_not_crash(self, tmp_path):
        p = tmp_path / "bad.eml"
        p.write_bytes(b"not an email at all")
        assert mtc.extract_md_from_eml(p) == []


class TestWriteCorpus:
    def test_dedup_by_content_hash(self, tmp_path):
        """§4.6.1：同内容只留一份，否则同一篇挤占多个 top-k 名额。"""
        items = [
            {"name": "A/x.md", "text": "一样的内容" * 20, "subject": "s1",
             "sender": "s", "date": "2026-09-12", "zip": "1.zip"},
            {"name": "B/y.md", "text": "一样的内容" * 20, "subject": "s2",
             "sender": "s", "date": "2026-09-13", "zip": "2.zip"},
        ]
        res = mtc.write_corpus(items, tmp_path / "corpus")
        assert len(res["written"]) == 1 and len(res["dups"]) == 1

    def test_source_header_injected(self, tmp_path):
        """§4.6.3：检索片段必须自带出处与"未经复核"警告。"""
        items = [{"name": "01_面板/面板.md", "text": "正文" * 20, "subject": "产物包",
                  "sender": "研究产物自动分发 <dcliuzh@qq.com>", "date": "2026-09-12",
                  "zip": "包.zip"}]
        res = mtc.write_corpus(items, tmp_path / "corpus")
        body = (ROOT / res["written"][0]).read_text(encoding="utf-8") \
            if (ROOT / res["written"][0]).exists() else \
            (tmp_path / "corpus" / Path(res["written"][0]).relative_to(
                "docs/research_inbox")).read_text(encoding="utf-8")
        assert "外部" in body and "未经本系统复核" in body
        assert "dcliuzh@qq.com" in body and "包.zip" in body

    def test_folder_is_date_plus_subject(self, tmp_path):
        items = [{"name": "x.md", "text": "正文" * 20, "subject": "某主题/带斜杠",
                  "sender": "s", "date": "2026-09-12", "zip": "z.zip"}]
        res = mtc.write_corpus(items, tmp_path / "corpus")
        p = Path(res["written"][0])
        assert "2026-09-12" in str(p)
        # 主题里的路径分隔符必须被替换掉，不许在目录名里再分层
        assert p.parts[-2].count("2026-09-12") == 1


class TestMainExits:
    def test_missing_skill_exits_nonzero(self, monkeypatch, tmp_path, capsys):
        """§12.4：技能缺失必须报错退出，不许静默产出空语料。"""
        rc = mtc.main(["--skill-dir", str(tmp_path / "不存在的技能")])
        assert rc != 0
        err = capsys.readouterr().err
        assert "找不到 qqmail 技能" in err and "--from-dir" in err

    def test_from_dir_without_md_exits_nonzero(self, tmp_path, capsys):
        d = tmp_path / "empty"
        d.mkdir()
        rc = mtc.main(["--from-dir", str(d)])
        assert rc != 0
        assert "空语料" in capsys.readouterr().err

    def test_from_dir_missing_dir_exits_nonzero(self, tmp_path):
        assert mtc.main(["--from-dir", str(tmp_path / "nope")]) == 2

    def test_from_dir_happy_path(self, tmp_path, capsys):
        eml_dir = tmp_path / "emls"
        eml_dir.mkdir()
        _make_eml(eml_dir / "1.eml", members={"01_面板/面板.md": "内容" * 50})
        out = tmp_path / "corpus"
        rc = mtc.main(["--from-dir", str(eml_dir), "--out", str(out), "--json"])
        assert rc == 0
        assert len(list(out.rglob("*.md"))) == 1
        assert "md_found" in capsys.readouterr().out
