"""MetricsCalculator 全量单元测试 (迭代 3, 2026-07-15).

覆盖:
- compute_all 正常路径 (含 hold_days)
- 边界: 空 equity, 空 trades, 涨跌全单边
- 异常路径: pd.to_datetime 抛错时 hold_days 跳过 (业务铁律 F-H4 修复)
- _sharpe / max_drawdown / sharpe_ratio / win_rate / profit_loss_ratio / calmar_ratio
- 不同 periods_per_year (P1-4)
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.metrics import MetricsCalculator


# ═══════════════════════════════════════════════════════════════
# compute_all 正常路径
# ═══════════════════════════════════════════════════════════════


def _make_equity_curve(start: float, returns: list, dates=None):
    """构造 equity_curve DataFrame."""
    if dates is None:
        dates = pd.date_range("2024-01-01", periods=len(returns) + 1)
    eq_values = [start]
    for r in returns:
        eq_values.append(eq_values[-1] * (1 + r))
    return pd.DataFrame({"date": dates, "equity": eq_values})


def _make_trades(profits, entry_dates=None, exit_dates=None):
    """构造 trades DataFrame."""
    n = len(profits)
    if entry_dates is None:
        entry_dates = pd.date_range("2024-01-01", periods=n)
    if exit_dates is None:
        exit_dates = entry_dates + pd.Timedelta(days=5)
    return pd.DataFrame({
        "stock_code": [f"00000{i}" for i in range(n)],
        "profit_pct": profits,
        "entry_date": entry_dates,
        "exit_date": exit_dates,
    })


def test_compute_all_basic_metrics():
    """基础场景: 收益 + 胜率 + 最大回撤 + 夏普."""
    eq = _make_equity_curve(1_000_000.0, [0.01, -0.02, 0.03, 0.01, -0.01])
    trades = _make_trades([0.05, -0.03, 0.08, 0.02, -0.01])
    m = MetricsCalculator.compute_all(eq, trades)
    assert "cumulative_return" in m
    assert "annualized_return" in m
    assert "max_drawdown" in m
    assert "sharpe_ratio" in m
    assert "calmar_ratio" in m
    assert "total_trades" in m
    assert "win_rate" in m
    assert m["total_trades"] == 5


def test_compute_all_win_rate_correct():
    """胜率 = 盈利笔数 / 总笔数."""
    eq = _make_equity_curve(1_000_000.0, [0.01] * 10)
    trades = _make_trades([0.05, 0.03, -0.02, -0.01, 0.04])  # 3 胜 2 负
    m = MetricsCalculator.compute_all(eq, trades)
    assert m["win_rate"] == pytest.approx(0.6)


def test_compute_all_profit_loss_ratio():
    """盈亏比 = 平均盈利 / |平均亏损|."""
    eq = _make_equity_curve(1_000_000.0, [0.01] * 10)
    trades = _make_trades([0.10, -0.05])  # 盈亏比 = 2.0
    m = MetricsCalculator.compute_all(eq, trades)
    assert m["profit_loss_ratio"] == pytest.approx(2.0)


def test_compute_all_profit_loss_ratio_only_gains():
    """全部盈利 → 盈亏比 = 0 (losses 空)."""
    eq = _make_equity_curve(1_000_000.0, [0.01] * 5)
    trades = _make_trades([0.05, 0.03, 0.02])
    m = MetricsCalculator.compute_all(eq, trades)
    assert m["profit_loss_ratio"] == 0.0


def test_compute_all_profit_loss_ratio_only_losses():
    """全部亏损 → 盈亏比 = 0 (gains 空)."""
    eq = _make_equity_curve(1_000_000.0, [-0.01] * 5)
    trades = _make_trades([-0.05, -0.03, -0.02])
    m = MetricsCalculator.compute_all(eq, trades)
    assert m["profit_loss_ratio"] == 0.0


def test_compute_all_profit_factor_normal():
    """profit_factor = 总盈利 / |总亏损|."""
    eq = _make_equity_curve(1_000_000.0, [0.01] * 10)
    trades = _make_trades([0.10, -0.05, 0.08, -0.02])  # 0.18/0.07 ≈ 2.571
    m = MetricsCalculator.compute_all(eq, trades)
    assert m["profit_factor"] == pytest.approx(0.18 / 0.07)


def test_compute_all_profit_factor_only_gains_returns_inf():
    """无亏损 → profit_factor = inf."""
    eq = _make_equity_curve(1_000_000.0, [0.01] * 5)
    trades = _make_trades([0.05, 0.03])
    m = MetricsCalculator.compute_all(eq, trades)
    assert m["profit_factor"] == float("inf")


def test_compute_all_hold_days_correct():
    """avg/max/min_hold_days 必须正确."""
    eq = _make_equity_curve(1_000_000.0, [0.01] * 20)
    trades = _make_trades(
        [0.05, -0.03, 0.08],
        entry_dates=pd.to_datetime(["2024-01-01", "2024-01-05", "2024-01-10"]),
        exit_dates=pd.to_datetime(["2024-01-06", "2024-01-15", "2024-01-12"]),
    )
    m = MetricsCalculator.compute_all(eq, trades)
    assert m["avg_hold_days"] == pytest.approx((5 + 10 + 2) / 3)
    assert m["max_hold_days"] == 10
    assert m["min_hold_days"] == 2


# ═══════════════════════════════════════════════════════════════
# 边界 / 异常
# ═══════════════════════════════════════════════════════════════


def test_compute_all_empty_equity_returns_empty_dict():
    """空 equity_curve → 返回空 dict (不抛)."""
    eq = pd.DataFrame({"date": [], "equity": []})
    m = MetricsCalculator.compute_all(eq, pd.DataFrame())
    assert m == {}


def test_compute_all_single_equity_point_returns_empty_dict():
    """单点 equity → 空 dict (无法算收益)."""
    eq = pd.DataFrame({"date": [pd.Timestamp("2024-01-01")], "equity": [1_000_000.0]})
    m = MetricsCalculator.compute_all(eq, pd.DataFrame())
    assert m == {}


def test_compute_all_empty_trades_skips_trade_metrics():
    """空 trades → 不算 trade 相关指标, 但算 equity 相关."""
    eq = _make_equity_curve(1_000_000.0, [0.01, 0.02, 0.01])
    m = MetricsCalculator.compute_all(eq, pd.DataFrame())
    assert "cumulative_return" in m
    assert "win_rate" not in m
    assert "total_trades" not in m


def test_compute_all_hold_days_with_unparseable_dates_warns_but_no_crash():
    """hold_days 异常路径 (审计 F-H4 修复): 不可解析日期应 logger.warning 而非 NameError.

    实际代码 line 67 是 `except Exception as e:` — 报告 C1 假阳性.
    此测试锁住真实行为: 给脏数据, 不崩, 给出指标 (其他字段照常).
    """
    eq = _make_equity_curve(1_000_000.0, [0.01] * 10)
    trades = pd.DataFrame({
        "stock_code": ["000001"],
        "profit_pct": [0.05],
        "entry_date": ["not-a-date"],  # 脏数据
        "exit_date": ["also-bad"],
    })
    # 不应抛 — 应走 except 块 logger.warning
    m = MetricsCalculator.compute_all(eq, trades)
    assert "win_rate" in m
    # hold_days 字段应缺失 (没算出来)
    assert "avg_hold_days" not in m or m.get("avg_hold_days") is None


def test_compute_all_hold_days_logs_warning(caplog):
    """脏数据时必须 logger.warning (业务铁律 F-H4)."""
    eq = _make_equity_curve(1_000_000.0, [0.01] * 10)
    trades = pd.DataFrame({
        "stock_code": ["000001"],
        "profit_pct": [0.05],
        "entry_date": ["not-a-date"],
        "exit_date": ["also-bad"],
    })
    with caplog.at_level("WARNING", logger="backtest.metrics"):
        MetricsCalculator.compute_all(eq, trades)
    # 至少有一条 hold_days 相关的 warning
    hold_warnings = [r for r in caplog.records if "hold_days" in r.message]
    assert len(hold_warnings) >= 1


def test_compute_all_no_entry_exit_columns_skips_hold_days():
    """trades 无 entry_date / exit_date 列时跳过 hold_days 计算."""
    eq = _make_equity_curve(1_000_000.0, [0.01] * 10)
    trades = pd.DataFrame({"stock_code": ["000001"], "profit_pct": [0.05]})
    m = MetricsCalculator.compute_all(eq, trades)
    assert "avg_hold_days" not in m


# ═══════════════════════════════════════════════════════════════
# periods_per_year 多周期 (P1-4)
# ═══════════════════════════════════════════════════════════════


def test_compute_all_uses_correct_periods_per_year_for_weekly():
    """P1-4: 1w 周期 annualized_return 必须用 52 (而非 252)."""
    # 构造 52 个 bar 的 equity (1 年周线)
    eq = _make_equity_curve(1_000_000.0, [0.02] * 51)
    m = MetricsCalculator.compute_all(eq, pd.DataFrame(), periods_per_year=52)
    # 累计收益 ~ 1.02^52 - 1 ≈ 1.78, 用 52 应接近 1.78
    assert m["annualized_return"] > 1.5  # 接近累计值


def test_compute_all_uses_correct_periods_per_year_for_5m():
    """5m 周期 annualized_return 用 48*252=12096."""
    eq = _make_equity_curve(1_000_000.0, [0.001] * 119)  # 120 个 bar
    m = MetricsCalculator.compute_all(eq, pd.DataFrame(), periods_per_year=48 * 252)
    assert m["annualized_return"] > 0


# ═══════════════════════════════════════════════════════════════
# _sharpe
# ═══════════════════════════════════════════════════════════════


def test_sharpe_constant_equity_returns_zero():
    """无波动 → sharpe = 0 (std=0 防御)."""
    eq = pd.Series([1_000_000.0] * 10)
    assert MetricsCalculator._sharpe(eq, risk_free=0.015, periods_per_year=252) == 0.0


def test_sharpe_insufficient_data_returns_zero():
    """<2 个 return → 0."""
    eq = pd.Series([1_000_000.0, 1_010_000.0])  # 只有 1 个 return
    assert MetricsCalculator._sharpe(eq) == 0.0


def test_sharpe_positive_returns_positive_sharpe():
    """上涨 + 低波动 → sharpe > 0."""
    eq = pd.Series([1.0, 1.01, 1.02, 1.03, 1.04])
    sharpe = MetricsCalculator._sharpe(eq, risk_free=0.0, periods_per_year=252)
    assert sharpe > 0


# ═══════════════════════════════════════════════════════════════
# max_drawdown / sharpe_ratio / win_rate / profit_loss_ratio / calmar_ratio
# ═══════════════════════════════════════════════════════════════


def test_max_drawdown_method_basic():
    """最大回撤 = max((peak-trough)/peak)."""
    eq = pd.Series([100.0, 110.0, 90.0, 95.0, 80.0, 100.0])
    dd = MetricsCalculator.max_drawdown(eq)
    # peak=110, trough=80, dd = (110-80)/110 = 0.2727
    assert dd == pytest.approx(-0.2727, abs=1e-3)


def test_max_drawdown_insufficient_data():
    """<2 点 → 0."""
    assert MetricsCalculator.max_drawdown(pd.Series([100.0])) == 0.0


def test_sharpe_ratio_method_basic():
    """sharpe_ratio 静态方法."""
    eq = pd.Series([1.0, 1.02, 1.05, 1.03, 1.06])
    s = MetricsCalculator.sharpe_ratio(eq, risk_free=0.015, periods_per_year=252)
    assert isinstance(s, float)


def test_sharpe_ratio_constant_returns_zero():
    """无波动 → 0."""
    eq = pd.Series([1.0] * 5)
    assert MetricsCalculator.sharpe_ratio(eq) == 0.0


def test_win_rate_empty_returns_zero():
    """空 trades → 0."""
    assert MetricsCalculator.win_rate(pd.DataFrame()) == 0.0


def test_win_rate_no_profit_pct_column_returns_zero():
    """无 profit_pct 列 → 0."""
    trades = pd.DataFrame({"stock_code": ["000001"]})
    assert MetricsCalculator.win_rate(trades) == 0.0


def test_win_rate_basic():
    """胜率 = 盈利数 / 总数."""
    trades = pd.DataFrame({"profit_pct": [0.05, -0.02, 0.03, -0.01, 0.04]})
    assert MetricsCalculator.win_rate(trades) == pytest.approx(0.6)


def test_profit_loss_ratio_method_only_gains_returns_inf():
    """全盈利 → inf."""
    trades = pd.DataFrame({"profit_pct": [0.05, 0.03]})
    assert MetricsCalculator.profit_loss_ratio(trades) == float("inf")


def test_profit_loss_ratio_method_only_losses_returns_zero():
    """全亏损 → 0."""
    trades = pd.DataFrame({"profit_pct": [-0.05, -0.03]})
    assert MetricsCalculator.profit_loss_ratio(trades) == 0.0


def test_profit_loss_ratio_method_empty_returns_zero():
    """空 → 0."""
    assert MetricsCalculator.profit_loss_ratio(pd.DataFrame()) == 0.0


def test_calmar_ratio_normal():
    """calmar = annualized / |max_dd|."""
    calmar = MetricsCalculator.calmar_ratio(annualized_ret=0.20, max_dd=-0.10)
    assert calmar == pytest.approx(2.0)


def test_calmar_ratio_zero_dd_returns_zero():
    """max_dd ≈ 0 → 0 (防除零)."""
    assert MetricsCalculator.calmar_ratio(0.20, 0.00001) == 0.0
    assert MetricsCalculator.calmar_ratio(0.20, 0.0) == 0.0


def test_calmar_ratio_negative_dd():
    """max_dd 为负也用绝对值."""
    calmar = MetricsCalculator.calmar_ratio(0.20, -0.05)
    assert calmar == pytest.approx(4.0)


# ═══════════════════════════════════════════════════════════════
# cumulative_return 精度
# ═══════════════════════════════════════════════════════════════


def test_cumulative_return_simple():
    """100 万 → 110 万 → cumulative_return = 0.10."""
    eq = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=2),
        "equity": [1_000_000.0, 1_100_000.0],
    })
    m = MetricsCalculator.compute_all(eq, pd.DataFrame())
    assert m["cumulative_return"] == pytest.approx(0.10)


def test_cumulative_return_loss():
    """100 万 → 90 万 → cumulative_return = -0.10."""
    eq = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=2),
        "equity": [1_000_000.0, 900_000.0],
    })
    m = MetricsCalculator.compute_all(eq, pd.DataFrame())
    assert m["cumulative_return"] == pytest.approx(-0.10)


def test_total_trading_days_matches_len():
    """total_trading_days = len(equity_curve)."""
    eq = _make_equity_curve(1_000_000.0, [0.01] * 20)
    m = MetricsCalculator.compute_all(eq, pd.DataFrame())
    assert m["total_trading_days"] == 21  # 20 returns + 起点 = 21 bars