"""notes_gen/monthly.py 测试 — 合成 sqlite trades 表, 不碰真实数据, 网络零依赖。

覆盖: 月度笔记端到端 (统计底座四块 + frontmatter)、月末截断口径
(次月成交不进当月笔记)、brain 不可用/success=False/正常三种路径、
db 缺失兜底 (返 None 不抛异常)。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from notes_gen import monthly
from notes_gen.monthly import generate_monthly_note

prz = monthly._load_prz()  # 与 monthly 内部共享同一模块对象, 供 monkeypatch

_TIERS = {
    "WIN1": {"tier": "P1", "sector_code": "881001.SH", "sector_name": "半导体"},
    "LOS1": {"tier": "AVOID", "sector_code": "881099.SH", "sector_name": "房地产"},
    "OPEN": {"tier": "AVOID", "sector_code": "881099.SH", "sector_name": "房地产"},
}


def _ts(y, m, d):
    return dt.datetime(y, m, d).timestamp()


def _mk_db(path: Path, trades: list[tuple]) -> Path:
    """造最小 trades 表 (参照 tests/test_policy_report_zero.py 的 _mk_db)。
    trades: (code, direction, price, qty, ts)。"""
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


@pytest.fixture
def may_db(tmp_path):
    """2026-05 的合成成交: 1 胜 1 负已平仓 + 1 笔在持 + 1 笔次月成交 (应被截掉)。"""
    return _mk_db(tmp_path / "trade.db", [
        ("WIN1", 23, 10.0, 100, _ts(2026, 5, 6)),
        ("WIN1", 24, 12.0, 100, _ts(2026, 5, 11)),
        ("LOS1", 23, 10.0, 100, _ts(2026, 5, 6)),
        ("LOS1", 24, 9.0, 100, _ts(2026, 5, 13)),
        ("OPEN", 23, 5.0, 200, _ts(2026, 5, 7)),
        ("JUNE", 23, 7.0, 100, _ts(2026, 6, 2)),  # 次月成交, 不进 5 月笔记
    ])


@pytest.fixture(autouse=True)
def _patch_tiers(monkeypatch):
    """行业标签不打 TDX, 直接用合成档位 (网络/外部环境零依赖)。"""
    monkeypatch.setattr(prz, "load_tiers", lambda codes: {
        c: _TIERS.get(c, {"tier": "UNTAGGED", "sector_code": "", "sector_name": ""})
        for c in codes})


@pytest.fixture
def no_brain(monkeypatch):
    """强制 brain 不可用 (sys.modules 置 None → import 即 ImportError)。"""
    monkeypatch.setitem(sys.modules, "brain", None)
    monkeypatch.setitem(sys.modules, "brain.claude_cli", None)


@pytest.fixture
def fake_brain(monkeypatch):
    """注入假 brain 模块, 可配置返回。"""
    pkg = types.ModuleType("brain")
    cli = types.ModuleType("brain.claude_cli")
    state = {"resp": {"success": True, "answer": "叙事内容XYZ"}}
    cli.ask_brain_sync = lambda question, timeout=120: state["resp"]
    monkeypatch.setitem(sys.modules, "brain", pkg)
    monkeypatch.setitem(sys.modules, "brain.claude_cli", cli)
    return state


class TestStatsOnly:
    def test_note_without_brain(self, may_db, tmp_path, no_brain):
        out = generate_monthly_note("2026-05", db_path=may_db,
                                    out_dir=tmp_path / "notes")
        assert out == tmp_path / "notes" / "monthly_2026-05.md"
        md = out.read_text(encoding="utf-8")
        # frontmatter
        assert 'month: "2026-05"' in md and "brain: false" in md
        # 统计底座四块 (零号报告口径)
        assert "仓位体检" in md and "分档战绩" in md
        assert "偏离清单" in md and "规律候选" in md
        # 具体统计: P1 胜 / AVOID 负 (min_n=5 → 样本不足口径)
        assert "| P1 | 1 | 100.0% | 20.0% | 5 | 200 |" in md
        assert "样本不足" in md
        # 次月成交被月末截断
        assert "JUNE" not in md
        # 无叙事段
        assert "大脑复盘叙事" not in md

    def test_default_month_is_previous(self, may_db, tmp_path, no_brain):
        out = generate_monthly_note(None, db_path=may_db, out_dir=tmp_path / "notes")
        assert out is not None
        prev = (dt.date.today().replace(day=1) - dt.timedelta(days=1))
        assert f"monthly_{prev.strftime('%Y-%m')}.md" == out.name


class TestBrainNarrative:
    def test_brain_success_appends_narrative(self, may_db, tmp_path, fake_brain):
        out = generate_monthly_note("2026-05", db_path=may_db,
                                    out_dir=tmp_path / "notes")
        md = out.read_text(encoding="utf-8")
        assert "brain: true" in md
        assert "## 大脑复盘叙事" in md and "叙事内容XYZ" in md
        # 统计底座不受影响
        assert "分档战绩" in md

    def test_brain_failure_falls_back_to_stats(self, may_db, tmp_path, fake_brain):
        fake_brain["resp"] = {"success": False, "error": "模拟大脑超时"}
        out = generate_monthly_note("2026-05", db_path=may_db,
                                    out_dir=tmp_path / "notes")
        assert out is not None  # 叙事失败不弄丢统计
        md = out.read_text(encoding="utf-8")
        assert "brain: false" in md and "大脑复盘叙事" not in md
        assert "分档战绩" in md

    def test_brain_exception_falls_back(self, may_db, tmp_path, monkeypatch):
        pkg = types.ModuleType("brain")
        cli = types.ModuleType("brain.claude_cli")
        def _boom(question, timeout=120):
            raise RuntimeError("模拟 brain 崩溃")
        cli.ask_brain_sync = _boom
        monkeypatch.setitem(sys.modules, "brain", pkg)
        monkeypatch.setitem(sys.modules, "brain.claude_cli", cli)
        out = generate_monthly_note("2026-05", db_path=may_db,
                                    out_dir=tmp_path / "notes")
        assert out is not None
        assert "brain: false" in out.read_text(encoding="utf-8")


class TestDegrade:
    def test_missing_db_returns_none(self, tmp_path, no_brain):
        out = generate_monthly_note("2026-05", db_path=tmp_path / "nope.db",
                                    out_dir=tmp_path / "notes")
        assert out is None
        assert not (tmp_path / "notes").exists()

    def test_tiers_failure_degrades_to_untagged(self, may_db, tmp_path,
                                                no_brain, monkeypatch):
        monkeypatch.setattr(prz, "load_tiers", lambda codes: None)
        out = generate_monthly_note("2026-05", db_path=may_db,
                                    out_dir=tmp_path / "notes")
        md = out.read_text(encoding="utf-8")
        assert "UNTAGGED" in md and "行业标签不可用" in md
