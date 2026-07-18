"""超额收益统计 + Sortino + 回撤修复天数 测试 (2026-07-18)。

钉死四件事:
1. benchmark._align 挂 attrs["stats"], 累计/年化超额为几何口径
2. information_ratio 与 excess_monthly_win_rate 口径正确 (含退化场景)
3. metrics.sortino_ratio: 只罚下行波动; 全程不亏返回 0.0 (非 inf)
4. metrics 最大回撤修复天数: 已修复/未修复两分支
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.benchmark import BenchmarkComparator, compute_comparison_stats
from backtest.metrics import MetricsCalculator


def _align(days=126, strat_daily=0.002, index_flat=True, noisy=False):
    """合成权益与指数, 走真实 _align 产出 comparison (含 attrs)。"""
    dates = pd.bdate_range("2025-01-02", periods=days)
    if noisy:  # 交替涨跌 → excess std > 0, IR 非 None
        r = np.where(np.arange(days) % 2 == 0, strat_daily * 1.5, strat_daily * 0.5)
        r[0] = 0.0
    else:
        r = np.full(days, strat_daily)
        r[0] = 0.0
    eq = 1_000_000 * (1 + r).cumprod()
    px = np.full(days, 1000.0) if index_flat else 1000 * (1.001) ** np.arange(days)
    equity_curve = pd.DataFrame({"date": dates, "equity": eq})
    index_df = pd.DataFrame({"date": dates, "close": px})
    cmp_ = BenchmarkComparator({"indices": ["hs300"]})
    return cmp_._align(equity_curve, index_df, "hs300", periods_per_year=252)


class TestComparisonStats:
    def test_geometric_excess(self):
        comp = _align()
        st = comp.attrs.get("stats")
        assert st is not None, "_align 必须挂 attrs['stats']"
        expected_total = (1.002 ** 125) - 1  # 首日收益归零, 125 期复利
        assert st["strategy_total"] == pytest.approx(expected_total, rel=1e-6)
        assert st["index_total"] == pytest.approx(0.0, abs=1e-12)
        assert st["total_excess"] == pytest.approx(expected_total, rel=1e-6)
        # 年化: n_years = 126/252 = 0.5 → (1+total)^2 - 1
        assert st["annual_excess"] == pytest.approx((1 + expected_total) ** 2 - 1, rel=1e-6)

    def test_ir_none_when_excess_const(self):
        """超额收益恒定时 std=0 → IR 为 None (不能除零)。"""
        comp = _align()
        assert comp.attrs["stats"]["information_ratio"] is None

    def test_ir_positive_when_noisy(self):
        comp = _align(noisy=True)
        ir = comp.attrs["stats"]["information_ratio"]
        assert ir is not None and ir > 0

    def test_monthly_win_rate(self):
        comp = _align(days=126)  # ~6 个月, 策略每天涨, 指数平
        assert comp.attrs["stats"]["excess_monthly_win_rate"] == pytest.approx(1.0)
        # 指数涨得更快 → 月度全输
        dates = pd.bdate_range("2025-01-02", periods=126)
        eq = 1_000_000 * np.ones(126)
        px = 1000 * (1.005) ** np.arange(126)
        cmp2 = BenchmarkComparator({})._align(
            pd.DataFrame({"date": dates, "equity": eq}),
            pd.DataFrame({"date": dates, "close": px}), "hs300", 252)
        assert cmp2.attrs["stats"]["excess_monthly_win_rate"] == pytest.approx(0.0)
        assert cmp2.attrs["stats"]["total_excess"] < 0

    def test_empty_comparison(self):
        assert compute_comparison_stats(pd.DataFrame()) == {}


class TestSortino:
    def test_penalizes_downside_only(self):
        # 大涨大跌交替 vs 平稳上行, 同样累计收益下 Sortino 应不同;
        # 这里只验证基本正确性: 有下行 → 有限正值
        dates = pd.bdate_range("2025-01-02", periods=61)
        r = np.where(np.arange(61) % 2 == 0, 0.01, -0.005)
        r[0] = 0.0
        eq = pd.Series(1_000_000 * (1 + r).cumprod(), index=dates)
        s = MetricsCalculator._sortino(eq, 252)
        assert np.isfinite(s)

    def test_no_downside_returns_zero(self):
        eq = pd.Series(np.linspace(1e6, 1.2e6, 60))
        assert MetricsCalculator._sortino(eq, 252) == 0.0


class TestMaxDDRecovery:
    def test_recovered(self):
        # 100 → 120 → 90 (谷底, 回撤-25%) → 125 (修复): 谷底 idx=2, 修复 idx=3
        eq = pd.Series([100.0, 120.0, 90.0, 125.0, 130.0])
        periods, ok = MetricsCalculator._max_dd_recovery(eq)
        assert ok is True and periods == 1

    def test_unrecovered(self):
        eq = pd.Series([100.0, 120.0, 90.0, 95.0, 98.0])  # 谷底 idx=2, 到末尾没回 120
        periods, ok = MetricsCalculator._max_dd_recovery(eq)
        assert ok is False and periods == 2

    def test_compute_all_integration(self):
        dates = pd.bdate_range("2025-01-02", periods=30)
        eq_vals = np.linspace(1e6, 1.3e6, 15).tolist() + np.linspace(1.3e6, 1.1e6, 15).tolist()
        curve = pd.DataFrame({"date": dates, "equity": eq_vals})
        m = MetricsCalculator.compute_all(curve, pd.DataFrame(), 1_000_000.0)
        assert "sortino_ratio" in m
        assert m["max_dd_recovered"] is False  # 末尾仍在回撤中
        assert m["max_dd_recovery_days"] >= 0
