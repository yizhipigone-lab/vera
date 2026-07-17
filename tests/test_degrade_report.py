"""5m 降级影响报告测试 (计划书 2026-07-18, Phase B)。

被测: backtest/degrade_report.py — 模糊日判定 + peak_hi/ladder_done 重放 +
乐观/悲观两档影响金额范围 + close 价策略 (时间止损/条件时间止盈/首日) 偏差。

口径 (计划书 §4.5 + 开工修正):
- 先止损分支: 模糊日自己的 high 不计入 peak_hi (先破低时当日 high 尚未发生)
- close 价策略: 真 5m 早盘价不可观测 (数据已缺), 界 = [当日 1d 最低, 1d 最高]
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import backtest.engine as engine_module
from backtest.degrade_report import compute_impact_report
from backtest.engine import BacktestEngine
from tests.test_degrade_5m import (
    BARS_5M_PER_DAY,
    DAYS,
    _engine_fixture,
    _mk_1d,
    _mk_5m,
    _run_engine_5m,
)

B = BARS_5M_PER_DAY


def _arrays(day_hlc):
    """day_hlc: [(high, low, close), ...] → (close, high, low) (n_days*48, 1)。"""
    n = len(day_hlc) * B
    high = np.zeros((n, 1))
    low = np.zeros((n, 1))
    close = np.zeros((n, 1))
    for d, (h, l, c) in enumerate(day_hlc):
        high[d * B:(d + 1) * B, 0] = h
        low[d * B:(d + 1) * B, 0] = l
        close[d * B:(d + 1) * B, 0] = c
    return close, high, low


def _degraded(day_idx, n_days, n_stocks=1):
    d = np.zeros((n_days * B, n_stocks), dtype=bool)
    d[day_idx * B:(day_idx + 1) * B, :] = True
    return d


def _row(ci, ei, xi, ep, xp, sh, reason):
    pnl = (xp - ep) * sh
    return [ci, ei, xi, ep, xp, sh, pnl, (xp - ep) / ep, reason]


# ─────────────────────────────────────────────────────────────
# 模糊日判定 + 两档重算
# ─────────────────────────────────────────────────────────────

def test_ambiguous_day_actual_sl_exit_gives_optimistic_range():
    """模糊日 (high 够止盈档 且 low 破止损线), 实际按止损出 → 乐观界=止盈价, 悲观=实际。"""
    # day0: entry 10.0; day1 (降级): high 11.0 够 10.8 止盈档, low 8.5 破 9.0 止损线
    close, high, low = _arrays([(10.1, 9.9, 10.0), (11.0, 8.5, 10.5)])
    degraded = _degraded(1, 2)
    raw = np.array([_row(0, 47, 60, 10.0, 9.0, 100, 3.0)])  # 实际: 成本止损 9.0 出
    rep = compute_impact_report(
        raw, degraded, high, low, B,
        cost_enabled=True, cost_threshold=-0.10,
        ladder_enabled=True, ladder_profits=[0.08],
        initial_capital=100000.0)
    assert rep["ambiguous_trades"] == 1
    assert rep["impact_amount"]["pessimistic"] == pytest.approx(0.0)
    assert rep["impact_amount"]["actual"] == 0.0
    # 乐观: 先止盈 → 10.8 出, 差 (10.8-9.0)*100 = 180
    assert rep["impact_amount"]["optimistic"] == pytest.approx(180.0)
    assert rep["return_range"]["optimistic"] == pytest.approx(180.0 / 100000.0)


def test_ambiguous_day_actual_tp_exit_gives_pessimistic_range():
    """模糊日实际按止盈出 → 悲观界=止损价 (负), 乐观=实际。"""
    close, high, low = _arrays([(10.1, 9.9, 10.0), (11.0, 8.5, 10.5)])
    degraded = _degraded(1, 2)
    raw = np.array([_row(0, 47, 60, 10.0, 10.8, 100, 5.0)])  # 实际: 阶梯止盈 10.8 出
    rep = compute_impact_report(
        raw, degraded, high, low, B,
        cost_enabled=True, cost_threshold=-0.10,
        ladder_enabled=True, ladder_profits=[0.08],
        initial_capital=100000.0)
    assert rep["ambiguous_trades"] == 1
    # 悲观: 先止损 → 9.0 出, 差 (9.0-10.8)*100 = -180
    assert rep["impact_amount"]["pessimistic"] == pytest.approx(-180.0)
    assert rep["impact_amount"]["optimistic"] == pytest.approx(0.0)


def test_pessimistic_branch_excludes_today_high_from_peak():
    """先止损分支: 模糊日自己的 high 不计入 peak_hi (口径写死)。

    trailing: activation 5%, drawdown 10%。entry 10.0, day0 high 10.6 (peak_prev)。
    悲观 trail 线 = 10.6*0.9 = 9.54 (只用前日 peak);
    乐观 trail 线 = max(10.6, 11.5)*0.9 = 10.35 (计入当日 high)。
    若口径错把当日 high 计入悲观分支 → 悲观线 10.35, 金额差被缩窄 → 测试能抓。
    """
    close, high, low = _arrays([(10.6, 9.9, 10.0), (11.5, 9.4, 10.5)])
    degraded = _degraded(1, 2)
    # 实际: 移动止损按悲观线 9.54 出 (filled run 里 trail_line 用累计 peak 含当日,
    #  这里直接固定 xp=9.54 表示实际发生在 9.54)
    raw = np.array([_row(0, 47, 60, 10.0, 9.54, 100, 4.0)])
    rep = compute_impact_report(
        raw, degraded, high, low, B,
        trailing_enabled=True, trailing_activation=0.05, trailing_drawdown=0.10,
        initial_capital=100000.0)
    assert rep["ambiguous_trades"] == 1
    # 乐观: 先摸高 → peak=11.5 → 线 10.35 出, 差 (10.35-9.54)*100 = 81
    assert rep["impact_amount"]["optimistic"] == pytest.approx(81.0)
    # 悲观线 9.54 = 实际 → 0 (若口径错, 悲观线=10.35 → 悲观额=+81 ≠ 0, 被抓)
    assert rep["impact_amount"]["pessimistic"] == pytest.approx(0.0)


def test_non_ambiguous_day_determined_no_range():
    """非模糊日 (只够得着一侧) → 收益确定, 不计入区间。"""
    # day1: high 10.3 (够不着 10.8 止盈档), low 8.5 (破 9.0 止损) → 只 SL 可达
    close, high, low = _arrays([(10.1, 9.9, 10.0), (10.3, 8.5, 10.0)])
    degraded = _degraded(1, 2)
    raw = np.array([_row(0, 47, 60, 10.0, 9.0, 100, 3.0)])
    rep = compute_impact_report(
        raw, degraded, high, low, B,
        cost_enabled=True, cost_threshold=-0.10,
        ladder_enabled=True, ladder_profits=[0.08],
        initial_capital=100000.0)
    assert rep["ambiguous_trades"] == 0
    assert rep["impact_amount"]["pessimistic"] == 0.0
    assert rep["impact_amount"]["optimistic"] == 0.0


def test_ladder_done_replay_only_remaining_level_counts():
    """ladder_done 重放: 历史日已触发的档位不再算; 模糊日只判剩余档。"""
    # day0 high 10.6 → 一档 10.5 已触发; day1 (降级) high 11.1 → 只剩二档 11.0 可达
    close, high, low = _arrays([(10.6, 9.9, 10.0), (11.1, 8.5, 10.5)])
    degraded = _degraded(1, 2)
    raw = np.array([_row(0, 47, 60, 10.0, 11.0, 100, 5.0)])  # 实际: 二档 11.0 出
    rep = compute_impact_report(
        raw, degraded, high, low, B,
        cost_enabled=True, cost_threshold=-0.10,  # 止损 9.0, day1 low 8.5 可达
        ladder_enabled=True, ladder_profits=[0.05, 0.10],
        initial_capital=100000.0)
    assert rep["ambiguous_trades"] == 1
    # 乐观 = 剩余档 11.0 = 实际 → 0; 悲观 = 9.0 → (9.0-11.0)*100 = -200
    # (若一档 10.5 被误当可达, 乐观=10.5 < 实际 → 乐观额被压成 -50, 被抓)
    assert rep["impact_amount"]["optimistic"] == pytest.approx(0.0)
    assert rep["impact_amount"]["pessimistic"] == pytest.approx(-200.0)


# ─────────────────────────────────────────────────────────────
# close 价策略偏差 (审计 HIGH-1, 开工修正口径: [1d low, 1d high] vs 1d close)
# ─────────────────────────────────────────────────────────────

def test_close_based_stop_bias():
    """时间止损降级日按 1d 收盘成交: 偏差界 = (low-close)*sh ~ (high-close)*sh。"""
    close, high, low = _arrays([(10.1, 9.9, 10.0), (11.0, 10.0, 10.5)])
    degraded = _degraded(1, 2)
    raw = np.array([_row(0, 47, 60, 10.0, 10.5, 100, 6.0)])  # 时间止损按收盘 10.5 出
    rep = compute_impact_report(
        raw, degraded, high, low, B,
        cost_enabled=True, cost_threshold=-0.10,
        initial_capital=100000.0)
    bias = rep["close_based_stop_bias"]
    assert bias["count"] == 1
    assert bias["amount_pessimistic"] == pytest.approx((10.0 - 10.5) * 100)
    assert bias["amount_optimistic"] == pytest.approx((11.0 - 10.5) * 100)
    # close 价偏差不走模糊日通道
    assert rep["ambiguous_trades"] == 0
    # 但并入总影响范围
    assert rep["impact_amount"]["pessimistic"] == pytest.approx(-50.0)
    assert rep["impact_amount"]["optimistic"] == pytest.approx(50.0)


def test_close_based_reasons_all_covered():
    """时间止损(6)/cond_time(7)/时间止盈(9)/首日未达标(10) 都算 close 价策略。"""
    close, high, low = _arrays([(10.1, 9.9, 10.0), (11.0, 10.0, 10.5)])
    degraded = _degraded(1, 2)
    raw = np.array([
        _row(0, 47, 60, 10.0, 10.5, 100, 6.0),
        _row(0, 47, 61, 10.0, 10.5, 100, 7.0),
        _row(0, 47, 62, 10.0, 10.5, 100, 9.0),
        _row(0, 47, 63, 10.0, 10.5, 100, 10.0),
    ])
    rep = compute_impact_report(raw, degraded, high, low, B, initial_capital=100000.0)
    assert rep["close_based_stop_bias"]["count"] == 4


def test_exit_on_non_degraded_day_no_bias():
    """出场日不在降级日 → 无偏差无区间。"""
    close, high, low = _arrays([(10.1, 9.9, 10.0), (11.0, 10.0, 10.5), (10.8, 10.2, 10.6)])
    degraded = _degraded(1, 3)
    raw = np.array([_row(0, 47, 100, 10.0, 10.6, 100, 6.0)])  # day2 出场 (非降级)
    rep = compute_impact_report(raw, degraded, high, low, B, initial_capital=100000.0)
    assert rep["close_based_stop_bias"]["count"] == 0
    assert rep["ambiguous_trades"] == 0


# ─────────────────────────────────────────────────────────────
# 夏普/回撤范围 (equity 曲线三档)
# ─────────────────────────────────────────────────────────────

def test_sharpe_drawdown_ranges_present():
    """给 equity_arr 时输出夏普/回撤三档; 悲观曲线 ≤ 实际 ≤ 乐观。"""
    close, high, low = _arrays([(10.1, 9.9, 10.0), (11.0, 8.5, 10.5)])
    degraded = _degraded(1, 2)
    raw = np.array([_row(0, 47, 60, 10.0, 10.8, 100, 5.0)])
    equity = np.linspace(100000, 101080, 2 * B)  # 实际: 含 +180 乐观路径
    rep = compute_impact_report(
        raw, degraded, high, low, B,
        cost_enabled=True, cost_threshold=-0.10,
        ladder_enabled=True, ladder_profits=[0.08],
        initial_capital=100000.0,
        equity_arr=equity, periods_per_year=48 * 252)
    for key in ("sharpe_range", "max_drawdown_range"):
        assert key in rep
        assert set(rep[key]) == {"pessimistic", "actual", "optimistic"}
    # 悲观 (9.0 出, 少 180) 的累计收益 < 实际
    assert rep["return_range"]["pessimistic"] < rep["return_range"]["actual"]


# ─────────────────────────────────────────────────────────────
# engine 集成: result.degradation 含影响报告
# ─────────────────────────────────────────────────────────────

def test_engine_degradation_impact_report(monkeypatch):
    """engine.run() 的 result.degradation 含 impact_amount/ambiguous_trades/close_based_stop_bias。"""
    close_5m, mask, _, days_ts, selections = _engine_fixture()
    # 6.23 1d: 收 10.1 高 11.0 低 8.5 — high 够 10.8 止盈档, low 破 9.0 止损线 → 模糊日
    c1 = _mk_1d({"600001.SH": {"2026-06-22": 10.0, "2026-06-23": 10.1, "2026-06-24": 10.2}})
    h1 = c1.copy(); h1.iloc[1, 0] = 11.0
    l1 = c1.copy(); l1.iloc[1, 0] = 8.5
    kline_1d = {'Close': c1, 'High': h1, 'Low': l1, 'Open': c1 - 0.05}
    # entry 6.22 bar47 ep=10.0 → exit 6.23 (降级日) bar60, 实际成本止损 9.0 出
    raw = np.array([_row(0, 47, 60, 10.0, 9.0, 100, 3.0)])
    result = _run_engine_5m(
        monkeypatch, close_5m, mask, kline_1d, days_ts, selections,
        stop_config={
            'time_stop': {'enabled': True, 'max_hold_days': 20},
            'cost_stop': {'enabled': True, 'threshold': -0.10},
            'trailing_stop': {'enabled': False},
            'ladder_tp': {'enabled': True, 'levels': [{'profit': 0.08, 'sell_ratio': 1.0}]},
        },
        capture={}, core_raw_trades=raw)
    degr = result["degradation"]
    assert degr["ambiguous_trades"] == 1
    assert degr["impact_amount"]["optimistic"] == pytest.approx(180.0)
    assert degr["impact_amount"]["pessimistic"] == pytest.approx(0.0)
    assert "return_range" in degr and "close_based_stop_bias" in degr
