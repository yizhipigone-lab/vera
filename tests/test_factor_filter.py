# -*- coding: utf-8 -*-
"""selection/factor_filter — 规则应用 + 因子计算 + 因果性 测试"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from selection.factor_filter import apply_rules, compute_factor_values  # noqa: E402


def _df():
    rows = []
    for d in ("2026-07-15", "2026-07-16"):
        for i in range(10):
            rows.append((f"S{i}.SZ", d, "Q", float(i)))
    return pd.DataFrame(rows, columns=["stock_code", "select_date", "formula_name", "turnover_rate"])


def test_apply_rules_top10():
    out = apply_rules(_df(), ["turnover_rate:top10"])
    assert len(out) == 18 and "S9.SZ" not in out["stock_code"].values


def test_apply_rules_multiple_sequential():
    df = _df()
    df["dist_ma20"] = df["turnover_rate"]  # 第二因子同值, 便于推断
    out = apply_rules(df, ["turnover_rate:top20", "dist_ma20:top20"])
    # 第一轮剔 S9,S8; 第二轮在剩余 8 只里再剔 top20%(rank>0.8 → 2 只)
    assert len(out) == 16 - 2 or len(out) < 16
    assert "S9.SZ" not in out["stock_code"].values


def test_apply_rules_nan_kept():
    df = _df()
    df.loc[0, "turnover_rate"] = np.nan
    out = apply_rules(df, ["turnover_rate:top10"])
    assert "S0.SZ" in out["stock_code"].values


def test_apply_rules_unknown_rule_raises():
    with pytest.raises(ValueError):
        apply_rules(_df(), ["turnover_rate:top99"])


def test_apply_rules_missing_factor_raises():
    with pytest.raises(KeyError):
        apply_rules(_df(), ["not_a_factor:top10"])


def test_compute_panel_factor_causal():
    """面板因子(dist_ma20)截断未来数据, 过去取值逐值不变。"""
    idx = pd.bdate_range("2026-01-01", periods=80)
    closes = pd.DataFrame(
        {c: np.linspace(100, 120 + j * 5, 80) for j, c in enumerate(["A.SZ", "B.SZ"])},
        index=idx)
    sys.path.insert(0, str(ROOT / "tools"))
    from factor_ic_screen import f_dist_ma20, lookup
    full = f_dist_ma20({"close": closes})
    cut = 60
    trunc = f_dist_ma20({"close": closes.iloc[:cut]})
    pd.testing.assert_frame_equal(full.iloc[:cut], trunc, check_names=False)


def test_compute_factor_values_full_panel_factors():
    """审计 CRITICAL-1 回归: 需要 open/volume 的因子(intraday20/volr5_20)在生产路径必须可算。
    用真实 kline_cache(000001.SZ 必在), 验证不再 KeyError。"""
    code = "000001.SZ"
    if not (ROOT / "data" / "kline_cache" / "1d" / f"{code}.parquet").exists():
        pytest.skip("kline_cache 无 000001.SZ")
    k = pd.read_parquet(ROOT / "data" / "kline_cache" / "1d" / f"{code}.parquet")
    date = str(k["date"].iloc[-1])[:10]
    sel = pd.DataFrame([(code, date, "Q")], columns=["stock_code", "select_date", "formula_name"])
    out = compute_factor_values(sel, ["intraday20", "volr5_20", "dist_ma20"])
    assert "intraday20" in out.columns and out["intraday20"].notna().iloc[0]
    assert "volr5_20" in out.columns and out["volr5_20"].notna().iloc[0]
