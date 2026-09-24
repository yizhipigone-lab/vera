# -*- coding: utf-8 -*-
"""voided_scan 回扫/复检测试 (2026-09-17 PLOYLINE 事件)。

锁三条纪律:
1. **只增不删** —— 已在名单里的条目不覆盖 (人工填的原因优先);
2. 登记时留证 —— 记下作废前的成绩 (prev_verdict/prev_annret), 便于事后追溯;
3. 扫前复检 —— `guard` 命中黑名单就登记作废, 干净公式放行。

全程 tmp 目录 (monkeypatch 模块级路径常量), 绝不碰生产 voided.json。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.formula_farm import voided_scan as vs  # noqa: E402


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(vs, "VOIDED", str(tmp_path / "voided.json"))
    monkeypatch.setattr(vs, "ARCHIVE", str(tmp_path / "archive.json"))
    return tmp_path


def _write(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def test_register_void_only_adds_and_keeps_manual_reason(sandbox):
    """只增不删: 人工登记的原因不被回扫结果覆盖。"""
    _write(sandbox / "voided.json",
           {"items": {"GS0001": {"reason": "人工:已知问题"}}})
    assert vs.register_void("GS0001", "回扫:未来函数:DYNAINFO") is False
    items = vs.load_voided()["items"]
    assert items["GS0001"]["reason"] == "人工:已知问题"
    assert vs.register_void("GS0002", "回扫:未来函数:PLOYLINE") is True
    assert len(vs.load_voided()["items"]) == 2


def test_register_void_records_previous_score(sandbox):
    """留证: 作废前是达标还是未达标、年化多少, 都写进名单。"""
    _write(sandbox / "archive.json",
           {"GS0009": {"verdict": {"code": "pass"}, "best": {"annret": 0.31}}})
    vs.register_void("GS0009", "未来函数:PLOYLINE", "a.md")
    e = vs.load_voided()["items"]["GS0009"]
    assert e["prev_verdict"] == "pass"
    assert e["prev_annret"] == 0.31
    assert e["source_file"] == "a.md"


def test_guard_returns_recorded_reason_without_re_register(sandbox):
    _write(sandbox / "voided.json", {"items": {"GS0001": {"reason": "X"}}})
    assert vs.guard("GS0001", "a.md", {}) == "X"


def test_guard_registers_blacklisted_source(sandbox):
    """扫前复检: 源码踩黑名单 → 现场登记并返回原因 (治本项)。"""
    rec = {"file": "a.md", "code": "A:=DYNAINFO(4)>0;\nXG:A;"}
    reason = vs.guard("GS0002", "a.md", {"a.md": rec})
    assert reason and "DYNAINFO" in reason
    assert "GS0002" in vs.load_voided()["items"]


def test_guard_passes_clean_source(sandbox):
    rec = {"file": "b.md", "code": "VAR1:=MA(C,10);\nXG:CROSS(C,VAR1);"}
    assert vs.guard("GS0003", "b.md", {"b.md": rec}) is None
    assert "GS0003" not in vs.load_voided()["items"]


def test_guard_passes_when_source_missing(sandbox):
    """源码找不到不拦 (交给原有流程报错), 免把无源码的误判成作废。"""
    assert vs.guard("GS0004", "missing.md", {}) is None
    assert vs.load_voided()["items"] == {}


def test_polyline_both_spellings_caught_by_scan(sandbox):
    """两种拼法都要被回扫抓到 (GS0607/GS1318 就是用 PLOYLINE 漏的)。"""
    for spelling in ("POLYLINE", "PLOYLINE"):
        rec = {"file": f"{spelling}.md",
               "code": f"A:={spelling}(CROSS(C,MA(C,5)),C);\nXG:A>1;"}
        assert not vs.static_vetting.vet_runtime(rec)[0]
