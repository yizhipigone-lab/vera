# -*- coding: utf-8 -*-
"""tests/test_market_events.py — 事件引擎单元测试 (2026-09-18)。

事件引擎读写 events.jsonl, 测试一律传临时 path, 绝不污染生产数据
(与 conftest 的"新增落盘路径必须隔离"同一原则)。
"""
from __future__ import annotations

import datetime as dt

import pytest

from core import market_events as me

T0 = dt.date(2026, 9, 18)  # 一个交易日


def _events_file(tmp_path):
    return tmp_path / "events.jsonl"


def test_add_event_clamps_to_level_cap(tmp_path):
    """单事件修正分被 clamp 到该等级上限(需求: 史诗级±1 / 普通±0.5 / 短期±0.2)。"""
    f = _events_file(tmp_path)
    e = me.add_event("minor", "常规月度数据公布", 0.9, T0, logic="常规", path=f)
    assert e["initial_score"] == 0.2           # minor 上限 0.2
    e2 = me.add_event("epic", "万亿级平准基金", 5.0, T0, logic="系统政策", path=f)
    assert e2["initial_score"] == 1.0          # epic 上限 1.0
    assert e2["expire_days"] == 30


def test_add_event_unknown_level_raises(tmp_path):
    with pytest.raises(ValueError):
        me.add_event("super", "不存在等级", 0.5, T0, path=_events_file(tmp_path))


def test_linear_decay(tmp_path):
    """线性衰减: 首日满额, 第 n 天 = 初始 × 剩余/总, 到期为 0。"""
    f = _events_file(tmp_path)
    me.add_event("major", "央行降准0.5个百分点", 0.5, T0, logic="释放流动性", path=f)
    # 首日(第0天): 满额
    r0 = me.daily_tick(T0, path=f)
    assert r0["active"][0]["score_now"] == pytest.approx(0.5)
    assert r0["active"][0]["days_left"] == 10
    # 第4天: 剩6天 → 0.5 × 6/10 = 0.3
    r4 = me.daily_tick(T0 + dt.timedelta(days=4), path=f)
    assert r4["active"][0]["score_now"] == pytest.approx(0.3)
    assert r4["active"][0]["days_left"] == 6
    # 第10天: 剩余0 → 过期移除
    r10 = me.daily_tick(T0 + dt.timedelta(days=10), path=f)
    assert r10["count"] == 0 and r10["total_adj"] == 0.0
    assert r10["removed_ids"], "过期事件应被移除"


def test_expired_event_removed_from_file(tmp_path):
    """过期事件不仅当次不算分, 还会从落库文件里移除(下次不再出现)。"""
    f = _events_file(tmp_path)
    me.add_event("minor", "官员常规表态", 0.2, T0, path=f)   # minor 3天
    me.daily_tick(T0 + dt.timedelta(days=5), path=f)          # 过期, 触发移除重写
    assert me.list_active(T0 + dt.timedelta(days=5), path=f) == []


def test_total_adj_clamped_to_limit(tmp_path):
    """多个史诗级事件叠加, 合计修正分被 clamp 到 ±1(需求 §四: 总修正上限±1)。"""
    f = _events_file(tmp_path)
    for i in range(3):
        me.add_event("epic", f"史诗级利好{i+1}", 1.0, T0, logic="x", path=f)
    r = me.daily_tick(T0, path=f)
    assert r["total_adj"] == 1.0     # 3×1.0=3.0 → 封顶 1.0
    # 负向同样封顶 -1
    f2 = tmp_path / "neg.jsonl"
    for i in range(3):
        me.add_event("epic", f"史诗级利空{i+1}", -1.0, T0, logic="x", path=f2)
    assert me.daily_tick(T0, path=f2)["total_adj"] == -1.0


def test_active_sorted_by_impact(tmp_path):
    """生效事件按影响力(|当前分|)从大到小排序(需求 TAB3: 按影响力度排序)。"""
    f = _events_file(tmp_path)
    me.add_event("minor", "小事件", 0.1, T0, path=f)
    me.add_event("major", "大事件", 0.5, T0, path=f)
    active = me.daily_tick(T0, path=f)["active"]
    assert active[0]["title"] == "大事件"
    assert active[1]["title"] == "小事件"


def test_suggest_level_keywords():
    assert me.suggest_level("央行宣布降准0.5个百分点")["level"] == "major"
    assert me.suggest_level("印花税调整")["level"] == "epic"
    assert me.suggest_level("某官员例行表态")["level"] == "minor"


def test_scan_news_is_placeholder():
    """LLM 自动扫描本期占位不做, 返回空且绝不抛。"""
    assert me.scan_news() == []


def test_no_import_trade():
    """铁律 1: 事件引擎不得 import trade —— 用 AST 检查真实 import 语句
    (注释里的"不 import trade"字样不应误伤, 故不能用文本匹配)。"""
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path(me.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name.split(".")[0] != "trade"
        elif isinstance(node, ast.ImportFrom):
            assert node.module is None or node.module.split(".")[0] != "trade"


def test_future_start_event_score_capped_at_full(tmp_path):
    """事件发生日晚于衰减基准日(周末刷周五快照的场景): 剩余天数封顶满额,
    分数绝不放大到初始分之上(2026-09-19 周六 18:00 周末 job 上线后此路径每周真走)。"""
    f = _events_file(tmp_path)
    me.add_event("major", "周六发生的利空", -0.4, T0 + dt.timedelta(days=1),
                 logic="周末事件", path=f)  # 周六录入, start=周六
    r = me.daily_tick(T0, path=f)          # 以周五(交易日)为基准衰减
    assert r["active"][0]["days_left"] == 10          # 不是 11
    assert r["active"][0]["score_now"] == pytest.approx(-0.4)  # 满额, 不放大


def test_upsert_tracker_created_persistent_and_non_decaying(tmp_path):
    """常驻跟踪事件: 创建后 list_active 显示 days_left=999、score_now=current_score,
    且 daily_tick 不会把它当过期清掉(与离散事件的衰减/过期模型解耦)。"""
    f = _events_file(tmp_path)
    me.upsert_tracker("fed_rate", "minor", "美联储利率预期", -0.2,
                      logic="测试", extra={"cur": 4.76}, path=f)
    active = me.list_active(T0, path=f)
    assert len(active) == 1
    assert active[0]["tracker"] == "fed_rate"
    assert active[0]["days_left"] == 999          # 常驻: 不过期
    assert active[0]["score_now"] == -0.2         # score_now = current_score, 不衰减
    # 推进 15 天后再看, 仍生效、分数不变(离散事件此时已过期)
    later = T0 + dt.timedelta(days=15)
    active2 = me.daily_tick(later, path=f)["active"]
    assert any(e["tracker"] == "fed_rate" for e in active2)
    assert active2[0]["score_now"] == -0.2


def test_upsert_tracker_only_changes_score_on_material_move(tmp_path):
    """常驻跟踪: 读数小幅变动(<0.02)不改分; 实质性变动(≥0.02)才改 current_score。"""
    f = _events_file(tmp_path)
    r0 = me.upsert_tracker("fed_rate", "minor", "美联储利率预期", -0.20,
                           logic="初始", extra={"cur": 4.76}, path=f)
    assert r0["changed"] is True
    # 小幅变动: -0.20 → -0.21 (Δ0.01 < 0.02 阈值) → 不改分
    r1 = me.upsert_tracker("fed_rate", "minor", "美联储利率预期", -0.21,
                           logic="小幅", extra={"cur": 4.77}, path=f)
    assert r1["changed"] is False
    assert r1["event"]["current_score"] == -0.20   # 分数不变
    # 实质性变动: -0.20 → -0.40 (Δ0.20 ≥ 0.02) → 改分
    r2 = me.upsert_tracker("fed_rate", "minor", "美联储利率预期", -0.40,
                           logic="大幅", extra={"cur": 5.20}, path=f)
    assert r2["changed"] is True
    assert r2["event"]["current_score"] == -0.40
