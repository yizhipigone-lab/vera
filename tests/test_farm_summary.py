# -*- coding: utf-8 -*-
"""core/farm_summary 看板汇总测试 (2026-09-16 计划书阶段 2, 全 tmp 目录造假产物)。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import farm_summary as fs  # noqa: E402


@pytest.fixture()
def root(tmp_path):
    r = tmp_path / "ff"
    (r / "runs" / "2026-09-16").mkdir(parents=True)
    (r / "reports").mkdir(parents=True)
    (r / "intake" / "xuangu").mkdir(parents=True)
    return r


def _wj(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def test_empty_root_all_gates_blocked(root):
    s = fs.gate_summaries(str(root))
    assert s["onboard"]["ready"] is False
    assert "先跑①" in s["onboard"]["hint"]
    assert s["verify"]["ready"] is False
    assert "先跑②" in s["verify"]["hint"]
    assert s["backtest"]["ready"] is False
    assert s["backtest"]["note"] == "本轮还没过定量复核"
    ov = fs.overview(str(root))
    assert ov["empty"] is True
    assert all(f["count"] == 0 for f in ov["funnel"])


def test_check_card_and_unlock_onboard(root):
    _wj(root / "runs" / "2026-09-16" / "check.json", {
        "date": "2026-09-16",
        "vetted": [{"file": "a.md"}, {"file": "b.md"}],
        "excluded": [{"file": "c.md", "reasons": ["筹码/专有函数:WINNER("]},
                     {"file": "d.md", "reasons": ["跨周期引用(#MONTH 等)",
                                                  "筹码/专有函数:COST("]}],
        "dup": [{"file": "e.md"}]})
    s = fs.gate_summaries(str(root))
    assert "新增可入库 2 条" in s["check"]["text"]
    assert "淘汰 2 条" in s["check"]["text"]
    assert s["onboard"]["ready"] is True
    ov = fs.overview(str(root))
    assert ov["top_reasons"][0] == ["筹码/专有函数", 2]
    assert ["跨周期引用", 1] in ov["top_reasons"]


def test_onboard_card_unlocks_verify_and_backtest(root):
    _wj(root / "runs" / "2026-09-16" / "check.json",
        {"date": "2026-09-16", "vetted": [{"file": "a.md"}, {"file": "b.md"},
                                          {"file": "c.md"}],
         "excluded": [], "dup": []})
    _wj(root / "runs" / "2026-09-16" / "onboard.json", {
        "date": "2026-09-16",
        "items": [{"gs": "GS0001", "file": "a.md", "url": "http://x", "ok": True, "msg": ""},
                  {"gs": "GS0002", "file": "b.md", "url": "", "ok": False, "msg": "编译失败"},
                  {"gs": "GS0003", "file": "c.md", "url": "", "ok": False, "msg": "EXC 超时"}]})
    s = fs.gate_summaries(str(root))
    assert "成功 1 条 / 失败 2 条" in s["onboard"]["text"]
    assert "全库已入库 1 条" in s["onboard"]["text"]
    # 还剩待入 = 3 个 vetted − (ok 1 + 编译失败 1) = 1 (EXC 可重试不算终态)
    assert "还剩 1 条待入" in s["onboard"]["text"]
    assert s["verify"]["ready"] is True
    assert s["backtest"]["ready"] is True
    ov = fs.overview(str(root))
    funnel = {f["key"]: f for f in ov["funnel"]}
    assert funnel["onboard"]["count"] == 1


def test_verify_json_card_and_overview(root):
    _wj(root / "runs" / "2026-09-16" / "verify.json", {
        "date": "2026-09-16", "cutoff": "20260801",
        "stats": {"total": 3, "pass": 2, "fail": 1, "unknown": 0},
        "formulas": {"GS0001": {"concl": "通过"}, "GS0002": {"concl": "通过"},
                     "GS0003": {"concl": "未通过"}}})
    s = fs.gate_summaries(str(root))
    assert "通过 2 / 未通过 1 / 无法判定 0" in s["verify"]["text"]
    assert s["backtest"]["note"] == ""   # 有复核产物 → 不再提醒
    ov = fs.overview(str(root))
    funnel = {f["key"]: f for f in ov["funnel"]}
    assert funnel["verified"]["count"] == 3
    assert funnel["verified"]["pass"] == 2


def test_verify_md_fallback_tolerates_stars_and_parens(root):
    (root / "reports" / "2026-09-11_定量复核_公式农场.md").write_text(
        "# x\n结论: 通过 2 · **未通过 0** · 无法判定(复核失败/未解析/零信号) 0\n",
        encoding="utf-8")
    s = fs.gate_summaries(str(root))
    assert "通过 2 / 未通过 0 / 无法判定 0" in s["verify"]["text"]
    # 总览漏斗同步兜底: 无 verify.json 时用 md 聚合数 (通过 2 / 共 2)
    ov = fs.overview(str(root))
    funnel = {f["key"]: f for f in ov["funnel"]}
    assert funnel["verified"]["count"] == 2
    assert funnel["verified"]["pass"] == 2


def test_backtest_json_and_md_fallback(root):
    _wj(root / "runs" / "2026-09-16" / "backtest_summary.json", {
        "date": "2026-09-16", "batch_date": "2026-09-16",
        "stats": {"pass": 1, "fail": 14, "insufficient": 3, "invalid": 0},
        "remaining": 2, "total": 20, "done": 18})
    s = fs.gate_summaries(str(root))
    assert "达标 1 / 未达标 14 / 样本不足 3 / 余 2 条待补" in s["backtest"]["text"]


def test_backtest_md_fallback(root):
    (root / "reports" / "2026-09-11_粗扫报告_公式农场.md").write_text(
        "# x\n批次: 本批入库 20 条 · 本轮粗扫 20 条 · 余 0 条待扫 (本批已扫完)\n"
        "本轮判定: 达标 0 · 未达标 14 · 样本不足 6 · 无有效组合 0\n",
        encoding="utf-8")
    s = fs.gate_summaries(str(root))
    assert "达标 0 / 未达标 14 / 样本不足 6 / 余 0 条待补" in s["backtest"]["text"]


def test_overview_board_groups_sorted(root):
    _wj(root / "archive.json", {
        "GS0001": {"file": "a.md", "url": "http://a", "onboard_date": "2026-09-06",
                   "best": {"key": "A", "annret": 0.20, "maxdd": -0.1,
                            "calmar": 2.0, "winrate": 0.6, "trades": 30},
                   "verdict": {"code": "pass", "label": "达标", "reason": "ok"}},
        "GS0002": {"file": "b.md", "url": "", "onboard_date": "2026-09-06",
                   "best": {"key": "A", "annret": 0.30, "maxdd": -0.01,
                            "calmar": 8.0, "winrate": 1.0, "trades": 3},
                   "verdict": {"code": "insufficient", "label": "样本不足",
                               "reason": "3 笔"}},
        "GS0003": {"file": "c.md", "url": "", "onboard_date": "2026-09-06",
                   "best": None,
                   "verdict": {"code": "invalid", "label": "无有效组合",
                               "reason": "零信号"}}})
    ov = fs.overview(str(root))
    assert [r["gs"] for r in ov["board"]["pass"]] == ["GS0001"]
    assert [r["gs"] for r in ov["board"]["insufficient"]] == ["GS0002"]
    assert [r["gs"] for r in ov["board"]["fail"]] == ["GS0003"]  # invalid 归折叠组
    funnel = {f["key"]: f for f in ov["funnel"]}
    assert funnel["swept"]["count"] == 2      # 有结果 = 非 invalid
    assert funnel["pass"]["count"] == 1
    assert ov["board_total"] == 3
    # 行字段齐全 (前端表格直接用)
    row = ov["board"]["pass"][0]
    assert row["annret"] == 0.20 and row["url"] == "http://a"
    assert row["onboard_date"] == "2026-09-06"
