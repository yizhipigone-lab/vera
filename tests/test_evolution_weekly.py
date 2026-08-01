# -*- coding: utf-8 -*-
"""evolution/weekly.py 测试 — 合成 db + 假大脑, 零网络零 CLI。

覆盖: 教训解析 (多条/上限/畸形)、playbook 追加格式与松耦合、
周度流程 (有成交→大脑复盘+教训入册+报告、无成交→跳过大脑、
大脑失败→统计照出、db 缺失→None)。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from evolution import weekly
from evolution.weekly import append_lessons, parse_lessons, run_weekly_evolution


class TestParseLessons:
    def test_basic_and_cap(self):
        text = ("分析…<lesson>止损太宽|20笔有6笔亏超10%, 收紧到10%</lesson>"
                "…<lesson>档位纪律|AVOID档全亏, 禁买</lesson>"
                "<lesson>第三条|x</lesson><lesson>第四条|y</lesson>")
        ls = parse_lessons(text)
        assert len(ls) == 3  # 上限
        assert ls[0][0] == "止损太宽" and "收紧" in ls[0][1]

    def test_malformed_ignored(self):
        assert parse_lessons("<lesson>没竖线</lesson>") == []
        assert parse_lessons("没有标签") == []
        assert parse_lessons("") == []


class TestAppendLessons:
    def test_append_format(self, tmp_path):
        pb = tmp_path / "LESSONS.md"
        assert append_lessons(pb, [("止损太宽", "收紧到 10%")], "2026-07-29")
        text = pb.read_text(encoding="utf-8")
        assert "### 2026-07-29 止损太宽" in text and "收紧到 10%" in text
        assert "- 来源: 周度复盘" in text

    def test_empty_noop_and_failure_soft(self, tmp_path):
        assert append_lessons(tmp_path / "a.md", [], "2026-07-29") is True
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        assert append_lessons(blocker / "sub" / "a.md", [("t", "b")],
                              "2026-07-29") is False  # 不抛


def _mk_db(path: Path, trades: list[tuple]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE trades (
        traded_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, code TEXT NOT NULL,
        direction INTEGER NOT NULL, price REAL NOT NULL, qty INTEGER NOT NULL,
        amount REAL NOT NULL, ts REAL NOT NULL)""")
    conn.executemany(
        "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?)",
        [(f"t{i}", "o", c, d, p, q, p * q, ts)
         for i, (c, d, p, q, ts) in enumerate(trades)])
    conn.commit()
    conn.close()
    return path


_T0 = dt.datetime(2026, 7, 20).timestamp()
_TIERS = {"AAA": {"tier": "P1", "sector_code": "s", "sector_name": "半导体"}}


@pytest.fixture
def patch_tiers(monkeypatch):
    prz = weekly._load_prz()
    monkeypatch.setattr(prz, "load_tiers", lambda codes: {
        c: _TIERS.get(c, {"tier": "UNTAGGED", "sector_code": "", "sector_name": ""})
        for c in codes})


@pytest.fixture
def fake_brain(monkeypatch):
    pkg = types.ModuleType("brain")
    cli = types.ModuleType("brain.claude_cli")
    state = {"resp": {"success": True,
                      "answer": "复盘分析…<lesson>止损太宽|收紧到10%</lesson>"}}
    cli.ask_brain_sync = lambda *a, **k: state["resp"]
    monkeypatch.setitem(sys.modules, "brain", pkg)
    monkeypatch.setitem(sys.modules, "brain.claude_cli", cli)
    return state


class TestWeeklyFlow:
    def test_full_cycle(self, tmp_path, patch_tiers, fake_brain):
        db = _mk_db(tmp_path / "t.db", [("AAA", 23, 10.0, 100, _T0),
                                        ("AAA", 24, 9.0, 100, _T0 + 86400)])
        pb = tmp_path / "playbook" / "LESSONS.md"
        out = run_weekly_evolution(db_path=db, report_dir=tmp_path / "notes",
                                   playbook=pb, today=dt.date(2026, 7, 26))
        assert out is not None and out.name == "weekly_2026-W30.md"
        md = out.read_text(encoding="utf-8")
        assert "brain: true" in md and "lessons_added: 1" in md
        assert "本周新教训" in md and "止损太宽" in md
        # playbook 也写进去了
        assert "止损太宽" in pb.read_text(encoding="utf-8")

    def test_no_trades_skips_brain(self, tmp_path, monkeypatch):
        db = _mk_db(tmp_path / "t.db", [])
        # 大脑被调就炸 —— 验证它根本没被调
        monkeypatch.setitem(sys.modules, "brain.claude_cli", None)
        out = run_weekly_evolution(db_path=db, report_dir=tmp_path / "notes",
                                   playbook=tmp_path / "pb.md",
                                   today=dt.date(2026, 7, 26))
        assert out is not None
        assert "brain: false" in out.read_text(encoding="utf-8")

    def test_brain_failure_keeps_stats(self, tmp_path, patch_tiers, fake_brain):
        fake_brain["resp"] = {"success": False, "answer": "超时"}
        db = _mk_db(tmp_path / "t.db", [("AAA", 23, 10.0, 100, _T0),
                                        ("AAA", 24, 9.0, 100, _T0 + 86400)])
        out = run_weekly_evolution(db_path=db, report_dir=tmp_path / "notes",
                                   playbook=tmp_path / "pb.md",
                                   today=dt.date(2026, 7, 26))
        assert out is not None
        md = out.read_text(encoding="utf-8")
        assert "brain: false" in md and "分档战绩" in md  # 统计不丢

    def test_missing_db_returns_none(self, tmp_path):
        assert run_weekly_evolution(db_path=tmp_path / "nope.db",
                                    report_dir=tmp_path / "n") is None
