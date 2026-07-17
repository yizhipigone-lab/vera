# -*- coding: utf-8 -*-
"""信号日数据缺失 → 顺延 bug 的回归测试 (2026-07-17)。

背景: 002008 大族激光 6.23 信号因 5m K线 6.23-6.29 缺口, 被 _build_entry_signals
else 分支顺延到 6.30 入场, 用未来价格成交旧信号, 违反"信号日 T 收盘价买入"铁律。

修复策略: 信号日整天不在价格 index (跨日缺口) → WARN+丢弃, 不顺延。
同日顺延 (5m: 信号日 00:00 不在 9:35-15:00 bar 里, 但当天有 bar) → 保留, 取当天尾盘 bar。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest

from backtest.engine import BacktestEngine


def _engine():
    """_build_entry_signals 不依赖 self 状态, 最小构造即可。"""
    return BacktestEngine({})


def _sel(code, date):
    return pd.DataFrame([{"stock_code": code, "select_date": date, "formula_name": "T"}])


# ───────────────────────── 跨日缺口: 必须丢弃 ─────────────────────────

def test_signal_day_in_gap_drops_entry_no_defer():
    """【回归锁】信号日 6.23 整天不在价格 index (6.22→6.30 有缺口),
    信号必须被丢弃, 不允许顺延到 6.30 成交。"""
    prices = pd.DataFrame(
        {"002008.SZ": [131.93, 150.11]},
        index=pd.to_datetime(["2026-06-22", "2026-06-30"]),
    )
    sel = _sel("002008.SZ", "2026-06-23")
    entries = _engine()._build_entry_signals(sel, prices)
    # 6.30 不应被设 True (旧 bug 会顺延到这里)
    assert not bool(entries.loc[pd.Timestamp("2026-06-30"), "002008.SZ"]), (
        "信号日 6.23 缺失时, 信号被顺延到 6.30 —— 这是 002008 bug, 必须丢弃而非顺延"
    )
    # 整列应全 False (无任何入场)
    assert int(entries["002008.SZ"].sum()) == 0


def test_signal_day_in_gap_logs_warning(caplog):
    """信号日缺失丢弃时, 必须打 WARNING 告知用户 (不能静默)。"""
    prices = pd.DataFrame(
        {"002008.SZ": [131.93, 150.11]},
        index=pd.to_datetime(["2026-06-22", "2026-06-30"]),
    )
    sel = _sel("002008.SZ", "2026-06-23")
    with caplog.at_level("WARNING", logger="backtest.engine"):
        _engine()._build_entry_signals(sel, prices)
    assert any("002008" in r.message and ("丢弃" in r.message or "缺失" in r.message)
               for r in caplog.records), (
        "信号日缺失丢弃时必须告警, 不能静默吞掉"
    )


# ───────────────────────── 同日顺延 (5m): 必须保留 ─────────────────────────

def test_5m_same_day_shift_preserved():
    """5m 模式: 信号日 00:00 不在 5m index, 但当天有 bar → 取当天尾盘 bar (15:00)。
    这是 intended 行为, 修复不能破坏。"""
    bars = pd.date_range("2026-06-23 09:35", "2026-06-23 15:00", freq="5min")
    prices = pd.DataFrame({"002008.SZ": np.linspace(140.0, 145.0, len(bars))}, index=bars)
    sel = _sel("002008.SZ", "2026-06-23")
    entries = _engine()._build_entry_signals(sel, prices)
    # 应在当天最后一根 bar (15:00) 入场, 不丢弃, 不跨日
    assert bool(entries.loc[pd.Timestamp("2026-06-23 15:00"), "002008.SZ"]), (
        "5m 同日顺延到尾盘 bar 的 intended 行为被破坏"
    )
    assert int(entries["002008.SZ"].sum()) == 1


# ───────────────────────── 1d 精确命中: 不变 ─────────────────────────

def test_1d_exact_match_unchanged():
    """1d 模式: 信号日在 index → 直接置 True, 行为不变。"""
    prices = pd.DataFrame(
        {"002008.SZ": [131.93, 145.11, 150.11]},
        index=pd.to_datetime(["2026-06-22", "2026-06-23", "2026-06-30"]),
    )
    sel = _sel("002008.SZ", "2026-06-23")
    entries = _engine()._build_entry_signals(sel, prices)
    assert bool(entries.loc[pd.Timestamp("2026-06-23"), "002008.SZ"])
    assert int(entries["002008.SZ"].sum()) == 1


def test_signal_day_after_last_bar_drops_with_warning(caplog):
    """【复审 MEDIUM 补圆】信号日晚于价格 index 最后一根 bar → 丢弃 + 告警, 不静默吞。"""
    prices = pd.DataFrame(
        {"002008.SZ": [131.93, 145.11]},
        index=pd.to_datetime(["2026-06-22", "2026-06-23"]),
    )
    sel = _sel("002008.SZ", "2026-06-30")  # 信号日 6.30, 但价格数据只到 6.23
    with caplog.at_level("WARNING", logger="backtest.engine"):
        entries = _engine()._build_entry_signals(sel, prices)
    assert int(entries["002008.SZ"].sum()) == 0
    assert any("002008" in r.message and "丢弃" in r.message for r in caplog.records), (
        "信号日晚于最后一根 bar 也必须告警, 不能静默吞"
    )


# ───────────────────────── pipeline period 一致性告警 ─────────────────────────

def test_pipeline_warns_on_period_mismatch(caplog):
    """selection.period=1d, backtest.period=5m → 打 WARNING, 不中断。
    (002008 问题就是 period 不一致 + 5m 缺口共同导致)"""
    from pipeline.pipeline import Pipeline
    pipe = Pipeline.__new__(Pipeline)  # 跳过 __init__ 的 TDX/yaml 加载
    pipe.config = {
        "selection": {"period": "1d", "dividend_type": 1},
        "backtest": {"period": "5m"},
        "time_range": {"start": "20260101", "end": "20260110"},
        "stop_loss": {},
    }
    # 空 selections → run() 立即返回空, 不触 TDX; period 检查在 run 之前触发
    empty_sel = pd.DataFrame(columns=["stock_code", "select_date", "formula_name"])
    with caplog.at_level("WARNING", logger="pipeline.pipeline"):
        pipe.step2_backtest(empty_sel)
    assert any("period_mismatch" in r.message for r in caplog.records), (
        "selection/backtest period 不一致时必须告警"
    )


def test_pipeline_no_warn_on_period_match(caplog):
    """period 一致时不告警。"""
    from pipeline.pipeline import Pipeline
    pipe = Pipeline.__new__(Pipeline)
    pipe.config = {
        "selection": {"period": "1d", "dividend_type": 1},
        "backtest": {"period": "1d"},
        "time_range": {"start": "20260101", "end": "20260110"},
        "stop_loss": {},
    }
    empty_sel = pd.DataFrame(columns=["stock_code", "select_date", "formula_name"])
    with caplog.at_level("WARNING", logger="pipeline.pipeline"):
        pipe.step2_backtest(empty_sel)
    assert not any("period_mismatch" in r.message for r in caplog.records)

