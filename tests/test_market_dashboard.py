# -*- coding: utf-8 -*-
"""tests/test_market_dashboard.py — 大盘仪表盘编排层的离线单测 (2026-09-18)。

只测**不联网**的纯逻辑(指标级双周期对比、快照结构)。取数/联网由真实刷新另测。
"""
from __future__ import annotations

import datetime as dt

import pytest

from core import market_dashboard_runner as mdr


def _mk_snap(date, total, ind_scores):
    return {
        "date": date,
        "scores": {"final_total": total, "dimensions": {}},
        "indicators": {k: {"score": v} for k, v in ind_scores.items()},
    }


def test_inject_compare_with_history():
    """有上一日 + 30日前快照 → 指标级与总分双周期 delta 都正确。"""
    cur = _mk_snap("2026-09-18", 5.39, {"erp": 10, "pmi": 5, "us10y": 0})
    prev = _mk_snap("2026-09-17", 5.0, {"erp": 8, "pmi": 5, "us10y": 2})
    d30 = _mk_snap("2026-08-15", 4.0, {"erp": 6, "pmi": 3, "us10y": 4})
    snap = {"date": cur["date"], "scores": dict(cur["scores"]),
            "indicators": {k: dict(v) for k, v in cur["indicators"].items()}}
    mdr._inject_indicator_compare(snap, [d30, prev])
    assert snap["indicators"]["erp"]["delta_prev"] == 2     # 10 - 8
    assert snap["indicators"]["erp"]["delta_d30"] == 4      # 10 - 6
    assert snap["indicators"]["pmi"]["delta_prev"] == 0
    assert snap["indicators"]["us10y"]["delta_d30"] == -4   # 0 - 4
    assert snap["compare"]["prev_day"]["delta"] == pytest.approx(0.39)
    assert snap["compare"]["day_30"]["delta"] == pytest.approx(1.39)
    assert snap["compare"]["prev_day"]["direction"] == "转暖"


def test_inject_compare_cold_start_no_history():
    """冷启动(没有任何历史快照) → delta 全 None, compare 显示"历史数据不足"。"""
    cur = _mk_snap("2026-09-18", 5.39, {"erp": 10})
    snap = {"date": cur["date"], "scores": dict(cur["scores"]),
            "indicators": {k: dict(v) for k, v in cur["indicators"].items()}}
    mdr._inject_indicator_compare(snap, [])
    assert snap["indicators"]["erp"]["delta_prev"] is None
    assert snap["indicators"]["erp"]["delta_d30"] is None
    assert snap["compare"]["prev_day"]["delta"] is None
    assert "历史数据不足" in snap["compare"]["prev_day"]["direction"]


def test_inject_compare_skips_future_and_same_day():
    """all_recs 里晚于/等于当日的快照不得被当作 prev(防止同日 upsert 前自比)。"""
    cur = _mk_snap("2026-09-18", 5.0, {"erp": 10})
    same_day = _mk_snap("2026-09-18", 9.9, {"erp": 99})
    prev = _mk_snap("2026-09-17", 4.0, {"erp": 7})
    snap = {"date": cur["date"], "scores": dict(cur["scores"]),
            "indicators": {k: dict(v) for k, v in cur["indicators"].items()}}
    mdr._inject_indicator_compare(snap, [same_day, prev])
    assert snap["indicators"]["erp"]["delta_prev"] == 3     # 10 - 7, 不是 10-99
    assert snap["compare"]["prev_day"]["delta"] == pytest.approx(1.0)


def test_direction_labels():
    """变化方向文案: >0.05 转暖 / <-0.05 转冷 / 其余持平(大白话)。"""
    cur = _mk_snap("2026-09-18", 5.0, {"erp": 5})
    prev = _mk_snap("2026-09-17", 5.02, {"erp": 5})
    snap = {"date": cur["date"], "scores": dict(cur["scores"]),
            "indicators": {k: dict(v) for k, v in cur["indicators"].items()}}
    mdr._inject_indicator_compare(snap, [prev])
    assert snap["compare"]["prev_day"]["direction"] == "基本持平"


def test_dashboard_runner_no_import_trade():
    """铁律 1: 编排模块不得 import trade(AST 检查真实 import, 注释字样不算)。"""
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path(mdr.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name.split(".")[0] != "trade"
        elif isinstance(node, ast.ImportFrom):
            assert node.module is None or node.module.split(".")[0] != "trade"


def test_event_adjustment_flows_into_total(tmp_path, monkeypatch):
    """事件修正分要真的加进最终总分(最终 = 基础 + 事件, 钳到 0~10)。"""
    import datetime as _dt
    from core import market_events as me
    monkeypatch.setattr(me, "EVENTS_PATH", tmp_path / "ev.jsonl")
    me.add_event("epic", "史诗级利好(测试)", 1.0, _dt.date(2026, 9, 18), logic="测试")
    # 只给一个指标, 其余缺 → 各维度沿用/中性; 重点验证事件修正分加进总分
    ind_raw = {"erp": (6.2, "2026-09-18")}
    snap = mdr._build_snapshot(_dt.date(2026, 9, 18), "close", ind_raw, None)
    assert snap["scores"]["event_adj"] == pytest.approx(1.0)
    assert snap["scores"]["final_total"] == pytest.approx(
        min(10.0, snap["scores"]["base_total"] + 1.0))


def test_no_event_then_total_equals_base(tmp_path, monkeypatch):
    """无生效事件时, 最终总分 = 基础总分(事件修正=0)。"""
    import datetime as _dt
    from core import market_events as me
    monkeypatch.setattr(me, "EVENTS_PATH", tmp_path / "ev_empty.jsonl")
    ind_raw = {"erp": (6.2, "2026-09-18")}
    snap = mdr._build_snapshot(_dt.date(2026, 9, 18), "close", ind_raw, None)
    assert snap["scores"]["event_adj"] == 0
    assert snap["scores"]["final_total"] == pytest.approx(snap["scores"]["base_total"])
