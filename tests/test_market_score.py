# -*- coding: utf-8 -*-
"""tests/test_market_score.py — 大盘仪表盘打分引擎单元测试 (2026-09-18)。

`core/market_score.py` 是**纯函数**(不联网/不读盘/不 import trade), 这里全部离线可跑。
覆盖: 阈值边界 / 权重约束 / 缺口径指标缺席 / 特殊指标逻辑 / 总分校验 / 定性映射。
"""
from __future__ import annotations

import pytest

from core import market_score as ms


# ── 结构约束(权重/维度/指标对齐) ─────────────────────────────

def test_weights_sum_to_one():
    assert abs(sum(ms.DIMENSION_WEIGHTS.values()) - 1.0) < 1e-9


def test_each_indicator_in_exactly_one_dimension():
    flat = [k for keys in ms.DIMENSION_INDICATORS.values() for k in keys]
    assert len(flat) == len(set(flat)), "同一指标出现在了多个维度"
    assert set(flat) == set(ms.THRESHOLDS), "维度指标清单与阈值表不对齐"


def test_removed_indicators_absent():
    """社融(剔政府债)与 CME 利率预期已确认做不到并放弃(审计报告§3.4/§3.5) ——
    从结构上保证它们不出现在阈值表里, 不会被误算。"""
    for banned in ("credit_ex_gov", "cme_rate", "社融", "cme"):
        assert banned not in ms.THRESHOLDS


def test_indicator_names_cover_all():
    """每个指标都有大白话中文名(用户规则: 面向用户的内容不许裸代码/黑话)。"""
    assert set(ms.INDICATOR_NAMES) == set(ms.THRESHOLDS)


# ── 单项阈值边界 ─────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (6.2, 10), (6.0, 10), (5.9, 8), (5.5, 8), (5.4, 5), (4.5, 5),
    (4.4, 2), (3.5, 2), (3.4, 0),
])
def test_erp_boundaries(value, expected):
    assert ms.score_indicator("erp", value) == expected


@pytest.mark.parametrize("value,expected", [
    (5, 10), (10, 10), (15, 7), (30, 7), (50, 5), (70, 5), (80, 2), (90, 2), (95, 0),
])
def test_pe_percentile_boundaries(value, expected):
    assert ms.score_indicator("pe_percentile", value) == expected


@pytest.mark.parametrize("value,expected", [
    (3.0, 10), (3.5, 10), (4.0, 7), (4.2, 7), (4.5, 4), (4.8, 4), (4.94, 0),
])
def test_us10y_boundaries(value, expected):
    assert ms.score_indicator("us10y", value) == expected


@pytest.mark.parametrize("value,expected", [
    (0.9, 10), (0.95, 10), (0.7188, 7), (0.5, 5), (0.3, 2), (0.25, 0),
])
def test_option_pcr_boundaries(value, expected):
    assert ms.score_indicator("option_pcr", value) == expected


def test_turnover_blank_band_is_five():
    """成交额 4000~5000 亿是需求空白档, 补归 5 分档(计划书§5.2)。"""
    assert ms.score_indicator("turnover_amt", 9000) == 10
    assert ms.score_indicator("turnover_amt", 8000) == 10
    assert ms.score_indicator("turnover_amt", 5000) == 5
    assert ms.score_indicator("turnover_amt", 4500) == 5   # 空白档 → 5
    assert ms.score_indicator("turnover_amt", 3999) == 0


def test_none_input_returns_none():
    """数据暂缺 → 该指标分为 None(维度均分时跳过), 不报错。"""
    assert ms.score_indicator("erp", None) is None


# ── 特殊指标 ─────────────────────────────────────────────────

def test_ppi_trend_from_negative_to_positive():
    """近3个月由负转正 → 8(需求档, 优先级高于水平分档)。"""
    assert ms._score_ppi(0.5, -1.0) == 8
    assert ms._score_ppi(1.5, -0.3) == 8


def test_ppi_levels():
    assert ms._score_ppi(5.5, 4.0) == 1     # 过热
    assert ms._score_ppi(3.8, 3.0) == 6     # 温和扩张(补档, 实测2026-08值)
    assert ms._score_ppi(1.0, 0.5) == 5     # 温和
    assert ms._score_ppi(-1.0, -0.5) == 3   # 偏弱(未转正, 仍为负)
    assert ms._score_ppi(-3.0, -2.5) == 0   # 通缩
    assert ms._score_ppi(None, 1.0) is None


def test_margin_surge_beats_trend():
    """单周激增(>10%)是异常警示 → 1, 即使60日趋势向上。"""
    assert ms._score_margin(5.0, 12.0) == 1
    assert ms._score_margin(None, 11.0) == 1


def test_margin_trend():
    assert ms._score_margin(5.0, 2.0) == 8      # 稳步抬升
    assert ms._score_margin(1.0, 1.0) == 5      # 区间震荡
    assert ms._score_margin(-12.7, 1.0) == 2    # 持续下降(实测2026-09-17值)
    assert ms._score_margin(None, 2.0) == 5     # 60日缺, 单周正常 → 中性
    assert ms._score_margin(None, None) is None


def test_usdcny():
    """偏离年线: 人民币偏强(汇率低于年线)→8; 中性→5; 偏弱→2。"""
    assert ms._score_usdcny(-3.0) == 8
    assert ms._score_usdcny(0.0) == 5
    assert ms._score_usdcny(2.0) == 5
    assert ms._score_usdcny(3.0) == 2
    assert ms._score_usdcny(None) is None


# ── 维度合成与总分校验 ────────────────────────────────────────

def test_dimensions_skip_missing_indicator():
    """缺失指标不参与维度均分(决策7: 不上收权重, 只用剩余有效指标均分)。"""
    ind = {"erp": 10, "pe_percentile": 5, "pb_percentile": None, "dividend_spread": 10}
    dims = ms.score_dimensions(ind)
    v = dims["valuation"]
    assert v["n_valid"] == 3 and v["n_total"] == 4
    assert v["avg"] == pytest.approx(round((10 + 5 + 10) / 3, 4))
    assert v["note"] is None


def test_dimensions_all_missing_uses_prev():
    """某维度全缺 → 沿用上一交易日该维度均分, 并标注。"""
    ind = {"m1m2_spread": None, "pmi": None, "ppi": None}
    dims = ms.score_dimensions(ind, prev_dim_avg={"macro": 6.0})
    assert dims["macro"]["avg"] == 6.0
    assert "沿用上一交易日" in dims["macro"]["note"]


def test_dimensions_all_missing_cold_start_neutral_5():
    """冷启动(连上一日都没有) → 中性 5 分, 并标注。"""
    ind = {"m1m2_spread": None, "pmi": None, "ppi": None}
    dims = ms.score_dimensions(ind)
    assert dims["macro"]["avg"] == 5.0
    assert "中性" in dims["macro"]["note"]


def test_compute_base_total_check_err_is_tiny():
    """基础总分两条独立计算路径互相印证, 误差应 ≈ 0(远小于 0.1 容差)。"""
    ind = ms.score_all({"erp": 6.2, "pe_percentile": 66.0, "pb_percentile": 40.0,
                        "dividend_spread": 0.9, "m1m2_spread": -3.4, "pmi": 49.8,
                        "ppi": 3.8, "ppi_3m_ago": 3.0, "hs300_vs_ma200": 2.0,
                        "turnover_amt": 12000.0, "breadth_20d": 55.0,
                        "margin_60d_pct": -12.7, "margin_week_pct": 1.0,
                        "option_pcr": 0.7188, "us10y": 4.94, "usdcny_ma_dev": 1.0})
    dims = ms.score_dimensions(ind)
    base, err = ms.compute_base_total(dims)
    assert err <= ms.CHECK_TOLERANCE
    assert err == pytest.approx(0.0, abs=1e-3)
    # 各维度加权分之和 = 基础总分
    assert sum(d["weighted"] for d in dims.values()) == pytest.approx(base)


def test_label_and_band_boundaries():
    assert ms.label_and_band(8.0)["label"] == "极佳赔率，牛市环境"
    assert ms.label_and_band(6.5)["position_band"] == "50%~75%"
    assert ms.label_and_band(5.0)["label"] == "中性震荡，结构性行情"
    assert ms.label_and_band(3.5)["position_band"] == "15%~30%"
    assert ms.label_and_band(2.0)["label"] == "高风险，熊市主跌段"


def test_build_scores_structure_and_check_ok():
    out = ms.build_scores({"erp": 6.2, "pe_percentile": 66.0, "pb_percentile": 40.0,
                           "dividend_spread": 0.9, "m1m2_spread": -3.4, "pmi": 49.8,
                           "ppi": 3.8, "ppi_3m_ago": 3.0, "hs300_vs_ma200": 2.0,
                           "turnover_amt": 12000.0, "breadth_20d": 55.0,
                           "margin_60d_pct": -12.7, "margin_week_pct": 1.0,
                           "option_pcr": 0.7188, "us10y": 4.94, "usdcny_ma_dev": 1.0})
    assert out["check_ok"] is True
    assert 0.0 <= out["base_total"] <= 10.0
    assert set(out["dimensions"]) == set(ms.DIMENSION_WEIGHTS)
    assert out["label"] and out["position_band"]
