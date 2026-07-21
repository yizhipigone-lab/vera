# -*- coding: utf-8 -*-
"""公式因子体检实验室 — S0 拒跑 / S3 选臂 / 臂字符串 / 报告模板 测试"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import formula_lab as lab  # noqa: E402


# ── S0 前置检查 ──────────────────────────────────────────────

def _sel(n, days=60, per_day=8):
    dates = pd.bdate_range("2026-01-01", periods=days)
    rows = [(f"S{i % 50}.SZ", d) for d in dates for i in range(per_day)][:n]
    return pd.DataFrame(rows, columns=["stock_code", "select_date"])


def test_precheck_ok():
    ok, note = lab.precheck(_sel(480))
    assert ok and "OK" in note


def test_precheck_rejects_few_signals():
    ok, note = lab.precheck(_sel(100))
    assert not ok and "信号总量" in note


def test_precheck_rejects_sparse_days():
    sel = _sel(300, days=150, per_day=2)   # 每天仅 2 条, 有效截面日=0
    ok, note = lab.precheck(sel)
    assert not ok and "有效截面日" in note


# ── S3 选臂(v2.1: 阈值只卡证据窗, 其余窗符号一致即可) ────────

FAM = {"fa": "族A", "fb": "族A", "fc": "族B", "fd": "族C", "fe": "族D"}


def _ic(rows):
    return pd.DataFrame(rows, columns=["factor", "ic_mean", "icir"])


def test_select_arms_picks_strongest_family():
    w1 = _ic([("fa", -0.05, -0.20), ("fb", -0.04, -0.15),   # 弱窗(不过线也无妨)
              ("fc", -0.03, -0.10), ("fd", +0.01, +0.10)])
    w2 = _ic([("fa", -0.10, -0.50), ("fb", -0.11, -0.60),   # 证据窗: 族A 代表=fb
              ("fc", -0.07, -0.40), ("fd", +0.02, +0.15)])
    quals = lab.select_arms({"t1": w1, "t2": w2}, FAM, primary="t2")
    assert len(quals) == 2
    assert quals[0]["family"] == "族A" and quals[0]["factor"] == "fb"
    assert quals[0]["ic_sign"] == -1
    assert quals[1]["family"] == "族B"


def test_select_arms_skips_sign_inconsistent():
    w1 = _ic([("fa", +0.10, +0.50)])
    w2 = _ic([("fa", -0.10, -0.50)])     # 符号跨窗不一致
    assert lab.select_arms({"t1": w1, "t2": w2}, FAM, primary="t2") == []


def test_select_arms_weak_secondary_window_ok():
    w1 = _ic([("fa", -0.01, -0.05)])     # 弱但同号 → v2.1 下放行, 留给 S4 终审
    w2 = _ic([("fa", -0.10, -0.50)])
    quals = lab.select_arms({"t1": w1, "t2": w2}, FAM, primary="t2")
    assert len(quals) == 1 and quals[0]["factor"] == "fa"


def test_select_arms_skips_below_threshold_in_primary():
    w1 = _ic([("fa", -0.10, -0.50)])
    w2 = _ic([("fa", -0.02, -0.50)])     # 证据窗 IC 不过线
    assert lab.select_arms({"t1": w1, "t2": w2}, FAM, primary="t2") == []


def test_select_arms_positive_ic_gives_bottom_rules():
    w1 = _ic([("fc", +0.09, +0.45)])
    w2 = _ic([("fc", +0.08, +0.40)])
    quals = lab.select_arms({"t1": w1, "t2": w2}, FAM, primary="t2")
    assert quals[0]["ic_sign"] == 1
    assert lab.arms_spec(quals) == "fc:bottom10,fc:bottom20"


def test_arms_spec_negative_ic_top_rules():
    quals = [{"family": "族A", "factor": "fb", "ic_sign": -1, "strength": 0.6, "per_win": {}}]
    assert lab.arms_spec(quals) == "fb:top10,fb:top20"


def test_arms_spec_appends_combos_when_two_families():
    """2026-07-20 用户拍板: ≥2 族时追加同款档位组合臂。"""
    quals = [{"family": "A", "factor": "f1", "ic_sign": -1, "strength": 0.6, "per_win": {}},
             {"family": "B", "factor": "f2", "ic_sign": 1, "strength": 0.5, "per_win": {}}]
    spec = lab.arms_spec(quals)
    assert "f1:top10+f2:bottom10" in spec and "f1:top20+f2:bottom20" in spec


# ── S5 报告模板(大白话版) ─────────────────────────────────────

def _ab_df():
    return pd.DataFrame([
        {"arm": "base", "kept": 100, "removed": 0, "annret": 0.10, "maxdd": -0.05,
         "sharpe": 1.0, "winrate": 0.6, "calmar": 2.0, "verdict": "BASE"},
        {"arm": "fb_top10", "kept": 90, "removed": 10, "annret": 0.12, "maxdd": -0.04,
         "sharpe": 1.2, "winrate": 0.62, "calmar": 3.0, "verdict": "PASS(增收)"},
    ])


def _ic_df():
    return pd.DataFrame([
        {"factor": "turnover_rate", "ic_mean": -0.15, "icir": -0.8},
        {"factor": "dist_ma20", "ic_mean": -0.12, "icir": -0.6},
        {"factor": "circ_mv", "ic_mean": 0.06, "icir": 0.2},
    ])


def test_report_contains_verdict_and_warnings():
    md = lab.render_report("TESTF", ["t1", "t2"], "cfg.yaml",
                           {"t1": "OK", "t2": "OK"},
                           [{"family": "族A", "factor": "fb", "ic_sign": -1,
                             "strength": 0.6, "per_win": {}}],
                           {"t1": _ab_df(), "t2": _ab_df()},
                           ["fb_top10"], "2026-07-19 15:00",
                           ic_primary=_ic_df())
    assert "fb_top10" in md and "两个窗口都验证通过" in md and "必须知道的提醒" in md
    assert "因子排名" in md and "换手率" in md and "越高越跌" in md


def test_report_zero_family_path():
    md = lab.render_report("TESTF", ["t1"], "cfg.yaml", {"t1": "OK"}, [], {}, [],
                           "2026-07-19 15:00", ic_primary=_ic_df())
    assert "没什么好加的" in md and "待复核" in md
