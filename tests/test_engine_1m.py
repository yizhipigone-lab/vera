# -*- coding: utf-8 -*-
"""引擎 1m 支持测试 (2026-07-26, 计划书 §5.3/5.4/5.5)。

钉死:
1. degrade 守卫: period=1m + degrade_5m=true → 不走降级 (无 degrade_res)
   — 审计 HIGH-1: 原 bpday>1 条件会被 1m 踩中静默全错
2. 区间硬限制: period=1m + start<20260126 → 截断 20260126
3. alias: _drop_nonstandard_5m_bars 外部调用方行为不变 (gs/quantqq sweeps)
4. _drop_nonstandard_intraday_bars 用 1m 表: 12:30 杂 bar 被丢, 09:31 保留
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backtest.engine as engine_mod
from backtest._constants import STD_1M_BAR_TIMES, STD_5M_BAR_TIMES
from backtest.engine import BacktestEngine


def _bars_1m(days, n_stocks=2):
    """合成 1m 网格 (09:31~11:30 + 13:01~15:00, 120+120 根/天)。"""
    parts = []
    for d in days:
        am = pd.date_range(f"{d} 09:31", periods=120, freq="1min")
        pm = pd.date_range(f"{d} 13:01", periods=120, freq="1min")
        parts.append(am.append(pm))
    return parts[0].append(parts[1:]) if len(parts) > 1 else parts[0]


def _sel():
    return pd.DataFrame({"stock_code": ["600001.SH"],
                         "select_date": pd.to_datetime(["2026-06-02"]),
                         "formula_name": ["F"]})


class TestIntradayDrop:
    def test_1m_table_drops_odd_bar(self):
        idx = list(pd.date_range("2026-06-02 09:31", periods=3, freq="1min"))
        idx.append(pd.Timestamp("2026-06-02 12:30"))   # 午休杂 bar
        close = pd.DataFrame({"600001.SH": [1.0] * 4}, index=idx)
        out, *_ = BacktestEngine._drop_nonstandard_intraday_bars(
            close, None, None, None, STD_1M_BAR_TIMES)
        assert len(out) == 3 and pd.Timestamp("2026-06-02 12:30") not in out.index

    def test_alias_5m_unchanged(self):
        idx = list(pd.date_range("2026-06-02 09:35", periods=3, freq="5min"))
        idx.append(pd.Timestamp("2026-06-02 13:01"))   # 非 5m 槽位
        close = pd.DataFrame({"600001.SH": [1.0] * 4}, index=idx)
        out, *_ = BacktestEngine._drop_nonstandard_5m_bars(close, None, None, None)
        assert len(out) == 3 and pd.Timestamp("2026-06-02 13:01") not in out.index


class TestGuards:
    def test_degrade_off_for_1m(self, monkeypatch):
        """degrade_5m=true + period=1m → _apply_5m_degradation 不得被调用。"""
        called = {"n": 0}
        monkeypatch.setattr(BacktestEngine, "_apply_5m_degradation",
                            lambda self, *a, **k: called.__setitem__("n", called["n"] + 1))
        # 合成 1m kline: 窗口拉取走 get_kline_windowed (bpday>1)
        days = ["2026-06-01", "2026-06-02", "2026-06-03"]
        idx = _bars_1m(days)
        close = pd.DataFrame({"600001.SH": np.linspace(10, 11, len(idx))}, index=idx)
        mask = pd.DataFrame(True, index=idx, columns=["600001.SH"])
        monkeypatch.setattr(
            engine_mod.DataFetcher, "get_kline_windowed",
            staticmethod(lambda *a, **k: (
                {"Close": close, "Open": close, "High": close * 1.01,
                 "Low": close * 0.99, "Volume": close * 0 + 1e5,
                 "Amount": close * 1e5}, mask)))

        eng = BacktestEngine({"period": "1m", "degrade_5m": True,
                              "matrix_cache": False, "initial_capital": 100000})
        prep = eng._prepare_run_matrices(_sel(), "20260601", "20260630", 5)
        assert called["n"] == 0, "1m 不应触发 5m 降级"
        assert prep is not None and prep["degrade_res"] is None
        assert prep["close"].shape[0] == 240 * 3   # 网格未被 48 槽位化

    def test_start_truncated_for_1m(self, monkeypatch):
        captured = {}

        def fake_prep(self, selections, start_time, end_time, win_td):
            captured["start"] = start_time
            return None

        monkeypatch.setattr(BacktestEngine, "_prepare_run_matrices", fake_prep)
        eng = BacktestEngine({"period": "1m", "matrix_cache": False,
                              "initial_capital": 100000})
        eng.run(_sel(), start_time="20250101", end_time="20260630",
                stop_config={})
        assert captured["start"] == "20260126"

    def test_start_not_truncated_when_ok(self, monkeypatch):
        captured = {}

        def fake_prep(self, selections, start_time, end_time, win_td):
            captured["start"] = start_time
            return None

        monkeypatch.setattr(BacktestEngine, "_prepare_run_matrices", fake_prep)
        eng = BacktestEngine({"period": "1m", "matrix_cache": False,
                              "initial_capital": 100000})
        eng.run(_sel(), start_time="20260301", end_time="20260630",
                stop_config={})
        assert captured["start"] == "20260301"

    def test_use_mc_condition_5m_unchanged(self):
        """矩阵缓存开关条件回归: 5m+degrade → 跳过缓存; 1m+degrade → 可用缓存。"""
        eng5 = BacktestEngine({"period": "5m", "degrade_5m": True})
        eng1 = BacktestEngine({"period": "1m", "degrade_5m": True})
        assert (eng5.degrade_5m and eng5.bars_per_day == 48) is True
        assert (eng1.degrade_5m and eng1.bars_per_day == 48) is False
