"""窗口终点截断测试 (2026-07-21, 用户决策: 执行窗口=请求区间)。

被测: core/data_fetcher.py compute_window_bounds(end_time=...) 截断 +
get_kline_windowed 透传 + engine.run() 把请求 end_time 传进窗口拉取。
mock DataFetcher, 不触 TDX。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backtest.engine as engine_module
from backtest.engine import BacktestEngine
from core.data_fetcher import DataFetcher


# ── compute_window_bounds: end_time 截断 ─────────────────────

def _tdays(start="2024-01-02", n=120):
    return list(pd.bdate_range(start, periods=n))


def test_window_end_clipped_to_end_time():
    """最晚信号日 + win_td 超出 end_time → 截断到 end_time。"""
    tdays = _tdays()
    sel = pd.DataFrame([
        {"stock_code": "600001.SH", "select_date": tdays[0]},
        {"stock_code": "600001.SH", "select_date": tdays[10]},
    ])
    end_ts = tdays[30]
    win_start, win_end = DataFetcher.compute_window_bounds(
        sel, 45, trading_days=tdays, end_time=end_ts.strftime("%Y%m%d"))
    assert win_start["600001.SH"] == tdays[0]
    # 未截断应为 tdays[10] + 45 交易日 (远超 tdays[30])
    assert win_end["600001.SH"] == end_ts


def test_window_end_not_clipped_when_within_end_time():
    """信号 + win_td 在 end_time 之内 → 保持窗口尾部 (用于中途退出的自然平仓)。"""
    tdays = _tdays()
    sel = pd.DataFrame([
        {"stock_code": "600001.SH", "select_date": tdays[0]},
        {"stock_code": "600001.SH", "select_date": tdays[10]},
    ])
    win_start, win_end = DataFetcher.compute_window_bounds(
        sel, 5, trading_days=tdays, end_time=tdays[100].strftime("%Y%m%d"))
    assert win_end["600001.SH"] == tdays[15]  # tdays[10] + 5 个交易日


def test_window_end_none_backward_compat():
    """不传 end_time → 行为与旧版一致 (信号 + win_td)。"""
    tdays = _tdays()
    sel = pd.DataFrame([
        {"stock_code": "600001.SH", "select_date": tdays[10]},
    ])
    _, win_end = DataFetcher.compute_window_bounds(sel, 45, trading_days=tdays)
    assert win_end["600001.SH"] == tdays[55]


def test_window_end_accepts_non_trading_day():
    """end_time 为节假日 → 截断到该自然日 (下游日历/取数对齐到最后交易日)。"""
    tdays = _tdays()
    sel = pd.DataFrame([
        {"stock_code": "600001.SH", "select_date": tdays[10]},
    ])
    # 2024-02-17 是周六, 早于 tdays[10]+45td (2024-03-19) → 截断到该自然日
    _, win_end = DataFetcher.compute_window_bounds(
        sel, 45, trading_days=tdays, end_time="20240217")
    assert win_end["600001.SH"] == pd.Timestamp("2024-02-17")


# ── get_kline_windowed: end_time 透传 ────────────────────────

def test_get_kline_windowed_passes_end_time(monkeypatch):
    captured = {}

    def fake_bounds(selections, window_trading_days, trading_days=None, end_time=None):
        captured["end_time"] = end_time
        return {}, {}

    monkeypatch.setattr(DataFetcher, "compute_window_bounds", fake_bounds)
    sel = pd.DataFrame([{"stock_code": "600001.SH", "select_date": "2024-01-02"}])
    DataFetcher.get_kline_windowed(sel, "5m", 45, end_time="20250101")
    assert captured["end_time"] == "20250101"

    DataFetcher.get_kline_windowed(sel, "5m", 45)
    assert captured["end_time"] is None


# ── engine.run(): 请求 end_time 传进窗口拉取 ─────────────────

def _grid_index(days):
    """一天的 48 根 5m bar (9:35..11:30 + 13:05..15:00)。"""
    out = []
    for d in days:
        t = pd.Timestamp(f"{d} 09:35")
        for _ in range(24):
            out.append(t)
            t += pd.Timedelta(minutes=5)
        t = pd.Timestamp(f"{d} 13:05")
        for _ in range(24):
            out.append(t)
            t += pd.Timedelta(minutes=5)
    return pd.DatetimeIndex(out)


def test_engine_run_passes_end_time_to_windowed(monkeypatch):
    """5m run: get_kline_windowed 收到 run() 的 end_time (窗口截断的入口)。"""
    days = ["2024-01-02", "2024-01-03"]
    idx = _grid_index(days)
    close = pd.DataFrame(10.0, index=idx, columns=["600001.SH"])
    kline = {"Close": close, "High": close * 1.01, "Low": close * 0.99,
             "Open": close.copy()}
    mask = pd.DataFrame(True, index=idx, columns=close.columns)
    captured = {}

    def fake_windowed(selections, period, window_trading_days, dividend_type,
                      fill_data, *, use_cache=False, end_time=None):
        captured["end_time"] = end_time
        return kline, mask

    monkeypatch.setattr(engine_module.DataFetcher, "get_kline_windowed", fake_windowed)
    monkeypatch.setattr(BacktestEngine, "_filter_limit_up",
                        lambda self, entries, prices: entries)
    monkeypatch.setattr(
        engine_module, "_simulate_core_v3",
        lambda price_np, entry_np, *a, **kw: (
            np.full(price_np.shape[0], 100000.0), np.empty((0, 9))))

    eng = BacktestEngine({"period": "5m"})
    sel = pd.DataFrame([{"select_date": idx[0], "stock_code": "600001.SH"}])
    eng.run(selections=sel, start_time="20240102", end_time="20240103",
            stop_config={"time_stop": {"enabled": True, "max_hold_days": 20}})
    assert captured["end_time"] == "20240103"


def test_window_end_never_before_window_start():
    """审计修复: end_time 早于信号日 (异常输入) 时窗口不倒置, win_end 钳到 win_start。"""
    tdays = _tdays()
    sel = pd.DataFrame([
        {"stock_code": "600001.SH", "select_date": tdays[10]},
    ])
    win_start, win_end = DataFetcher.compute_window_bounds(
        sel, 45, trading_days=tdays, end_time=tdays[5].strftime("%Y%m%d"))
    assert win_end["600001.SH"] == win_start["600001.SH"] == tdays[10]
