"""MetricsCalculator.rolling_metrics 单元测试 (图表分析深挖包 Phase 2, 2026-08-13)。

锁滚动指标契约:
- 日级聚合: bar 级输入 (一日多行) 按 date 前 10 字符分组取每日末值
- 窗口不足的点输出 None (不是 NaN — FastAPI allow_nan=False, NaN 直接 500)
- rolling_return/rolling_vol 为窗口内年化 (×252) 小数, 手算可验证
- 空输入/缺列 → 不抛出, 返回空序列
"""
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.metrics import MetricsCalculator


def _curve(rows):
    """rows: [(date_str, equity), ...] → date/equity/drawdown 三列 DataFrame。"""
    return pd.DataFrame({
        "date": [r[0] for r in rows],
        "equity": [r[1] for r in rows],
        "drawdown": [0.0] * len(rows),
    })


# ---------- 数值正确性 (手算可验证) ----------

def test_rolling_values_hand_computed():
    """4 日净值, window=3, rf=0: 收益 [0.01, -0.02, 0.03], 仅最后一点有值。

    手算: mean=0.0066667, std(ddof=1)=0.0251661
    rolling_return = mean*252 = 1.68
    rolling_vol    = std*sqrt(252) = 0.39951
    rolling_sharpe = mean/std*sqrt(252) = 4.20547
    """
    eq = _curve([
        ("2024-01-01", 100.0),
        ("2024-01-02", 101.0),       # +1%
        ("2024-01-03", 98.98),       # -2%
        ("2024-01-04", 101.9494),    # +3%
    ])
    out = MetricsCalculator.rolling_metrics(eq, window=3, risk_free=0.0)
    assert out["window"] == 3
    assert out["dates"] == ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]
    # 前 3 点窗口不足 → None
    for key in ("rolling_sharpe", "rolling_return", "rolling_vol"):
        assert len(out[key]) == 4
        assert out[key][:3] == [None, None, None]
    mean_r = (0.01 - 0.02 + 0.03) / 3
    std_r = float(np.std([0.01, -0.02, 0.03], ddof=1))
    assert out["rolling_return"][3] == pytest.approx(mean_r * 252, rel=1e-6)
    assert out["rolling_vol"][3] == pytest.approx(std_r * math.sqrt(252), rel=1e-6)
    assert out["rolling_sharpe"][3] == pytest.approx(
        mean_r / std_r * math.sqrt(252), rel=1e-6)


def test_rolling_sharpe_uses_risk_free():
    """rf>0 时夏普 = (mean - rf/252) / std * sqrt(252)。"""
    eq = _curve([
        ("2024-01-01", 100.0),
        ("2024-01-02", 101.0),
        ("2024-01-03", 98.98),
        ("2024-01-04", 101.9494),
    ])
    rf = 0.015
    out = MetricsCalculator.rolling_metrics(eq, window=3, risk_free=rf)
    mean_r = (0.01 - 0.02 + 0.03) / 3
    std_r = float(np.std([0.01, -0.02, 0.03], ddof=1))
    expect = (mean_r - rf / 252) / std_r * math.sqrt(252)
    assert out["rolling_sharpe"][3] == pytest.approx(expect, rel=1e-6)


def test_bar_level_input_aggregated_to_daily():
    """5m bar 级输入 (一日 3 行) 必须按日聚合取每日末值再算。"""
    rows = []
    # 3 天 × 每天 3 根 bar, 每日末值分别为 100 / 110 / 99
    for d, closes in (("2024-01-02", (100.0, 101.0, 100.0)),
                      ("2024-01-03", (108.0, 109.0, 110.0)),
                      ("2024-01-04", (100.0, 95.0, 99.0))):
        for t, e in zip(("09:35:00", "10:00:00", "15:00:00"), closes):
            rows.append((f"{d} {t}", e))
    out = MetricsCalculator.rolling_metrics(_curve(rows), window=2, risk_free=0.0)
    # 聚合成 3 个交易日
    assert out["dates"] == ["2024-01-02", "2024-01-03", "2024-01-04"]
    # 日收益: 110/100-1 = 0.1, 99/110-1 = -0.1
    mean_r = 0.0
    std_r = float(np.std([0.1, -0.1], ddof=1))
    assert out["rolling_return"][1] is None  # 窗口不足
    assert out["rolling_return"][2] == pytest.approx(mean_r * 252, abs=1e-9)
    assert out["rolling_vol"][2] == pytest.approx(std_r * math.sqrt(252), rel=1e-6)


def test_window_larger_than_data_all_none():
    """window=60 但只有 3 天 → 全部 None, 不抛。"""
    eq = _curve([("2024-01-0%d" % i, 100.0 + i) for i in (1, 2, 3)])
    out = MetricsCalculator.rolling_metrics(eq, window=60)
    assert len(out["dates"]) == 3
    assert out["rolling_sharpe"] == [None, None, None]
    assert out["rolling_return"] == [None, None, None]
    assert out["rolling_vol"] == [None, None, None]


def test_zero_vol_window_sharpe_none():
    """窗口内收益恒定 (std=0) → sharpe 为 None (除零未定义), return/vol 仍有值。"""
    eq = _curve([
        ("2024-01-01", 100.0),
        ("2024-01-02", 101.0),     # +1%
        ("2024-01-03", 102.01),    # +1%
    ])
    out = MetricsCalculator.rolling_metrics(eq, window=2, risk_free=0.0)
    assert out["rolling_return"][2] == pytest.approx(0.01 * 252, rel=1e-6)
    assert out["rolling_vol"][2] == pytest.approx(0.0, abs=1e-12)
    assert out["rolling_sharpe"][2] is None


# ---------- 边界 ----------

def test_empty_input_returns_empty_series():
    """空 DataFrame → 空序列, 不抛。"""
    out = MetricsCalculator.rolling_metrics(pd.DataFrame())
    assert out == {"window": 60, "dates": [], "rolling_sharpe": [],
                   "rolling_return": [], "rolling_vol": []}


def test_missing_columns_returns_empty_series():
    """缺 date/equity 列 → 空序列, 不抛。"""
    out = MetricsCalculator.rolling_metrics(pd.DataFrame({"foo": [1, 2]}))
    assert out["dates"] == []


def test_none_values_never_nan():
    """输出列表绝不含 NaN/inf (FastAPI allow_nan=False 硬约束)。"""
    eq = _curve([
        ("2024-01-01", 100.0),
        ("2024-01-02", 0.0),       # 净值归零 → 收益 -100%, 次日除零
        ("2024-01-03", 50.0),
        ("2024-01-04", 55.0),
    ])
    out = MetricsCalculator.rolling_metrics(eq, window=2, risk_free=0.0)

    def _walk(v):
        if isinstance(v, dict):
            for x in v.values():
                _walk(x)
        elif isinstance(v, list):
            for x in v:
                _walk(x)
        elif isinstance(v, float):
            assert math.isfinite(v), f"出现非有限值: {v}"

    _walk(out)
