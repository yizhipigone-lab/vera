# -*- coding: utf-8 -*-
"""过热剔除 A/B — 日截面过滤规则 + 预注册判定 测试"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import overheat_ab_test as ab  # noqa: E402


def _sel():
    # 两天, 每天 10 条信号, turnover 0~9
    rows = []
    for d in ("2026-07-15", "2026-07-16"):
        for i in range(10):
            rows.append((f"S{i}.SZ", d, "Q", float(i)))
    return pd.DataFrame(rows, columns=["stock_code", "select_date", "formula_name", "turnover_rate"])


def test_top10_drops_hottest_one_per_day():
    sel = _sel()
    out = ab.apply_filter(sel, "turnover_rate", "top10")
    assert len(out) == 18                       # 每天剔 1 条(最热)
    assert (out.groupby("select_date").size() == 9).all()
    assert "S9.SZ" not in out["stock_code"].values


def test_top20_drops_hottest_two_per_day():
    out = ab.apply_filter(_sel(), "turnover_rate", "top20")
    assert len(out) == 16
    assert "S9.SZ" not in out["stock_code"].values
    assert "S8.SZ" not in out["stock_code"].values


def test_bottom20_drops_smallest_two_per_day():
    sel = _sel().rename(columns={"turnover_rate": "circ_mv"})
    out = ab.apply_filter(sel, "circ_mv", "bottom20")
    assert len(out) == 16
    assert "S0.SZ" not in out["stock_code"].values
    assert "S1.SZ" not in out["stock_code"].values


def test_nan_rows_kept():
    sel = _sel()
    sel.loc[0, "turnover_rate"] = np.nan
    out = ab.apply_filter(sel, "turnover_rate", "top10")
    assert "S0.SZ" in out["stock_code"].values  # 缺失值保留


def test_base_returns_all():
    sel = _sel()
    assert len(ab.apply_filter(sel, None, None)) == len(sel)


def test_verdict_pass_gain():
    base = {"annret": 0.175, "maxdd": -0.045, "calmar": 3.86}
    assert ab.verdict({"annret": 0.19, "maxdd": -0.05, "calmar": 3.90}, base) == "PASS(增收)"


def test_verdict_pass_dd():
    base = {"annret": 0.175, "maxdd": -0.045, "calmar": 3.86}
    assert ab.verdict({"annret": 0.170, "maxdd": -0.024, "calmar": 3.80}, base) == "PASS(控回撤)"


def test_verdict_fail_calmar():
    base = {"annret": 0.175, "maxdd": -0.045, "calmar": 3.86}
    assert ab.verdict({"annret": 0.15, "maxdd": -0.05, "calmar": 3.0}, base) == "FAIL"


# ── 组合臂(2026-07-20 用户拍板: 组合也要终审) ─────────────────

def test_parse_arms_combo():
    arms = ab.parse_arms("f1:top10,f2:top10+f3:top20")
    assert arms[0] == ("base", None, None)
    assert ("f1_top10", "f1", "top10") in arms
    assert ("combo_f2_top10+f3_top20", None, "f2:top10+f3:top20") in arms


def test_parse_arms_combo_bad_rule():
    import pytest
    with pytest.raises(ValueError):
        ab.parse_arms("f2:top99+f3:top20")


def test_combo_filter_matches_production_semantics():
    """组合臂过滤 = 生产 apply_rules 顺序语义(与 tests/test_factor_filter 同预期)。"""
    rows = []
    for d in ("2026-07-15", "2026-07-16"):
        for i in range(10):
            rows.append((f"S{i}.SZ", d, "Q", float(i), float(i)))
    sel = pd.DataFrame(rows, columns=["stock_code", "select_date", "formula_name", "f2", "f3"])
    out = ab.apply_filter(sel, None, "f2:top10+f3:top10")
    assert "S9.SZ" not in out["stock_code"].values
    assert len(out) == 16                     # 每条规则每天各剔 1 只

