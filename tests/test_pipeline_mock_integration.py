"""Mock TDX 集成测试 (T-H-2, 2026-07-15).

用 FakeConnector/FakeTq mock TDX 双边界 (FormulaRunner + DataFetcher),
覆盖: 选股返回 → 引擎交易 → 买入价校验 → TDX 故障路径.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.data_fetcher import DataFetcher
from core.formula_runner import FormulaRunner
from backtest.engine import BacktestEngine
from tests.conftest import FakeConnector


# ── helpers ────────────────────────────────────────────────


def _make_ohlc(codes, n_bars=10, base_prices=None):
    """构造合成 OHLC 数据."""
    idx = pd.bdate_range("2026-01-05", periods=n_bars)
    close_data = {}
    for i, code in enumerate(codes):
        base = base_prices[code] if base_prices and code in base_prices else 10.0
        close_data[code] = [base + j * 0.5 for j in range(n_bars)]
    close = pd.DataFrame(close_data, index=idx)
    open_df = close * 0.99
    high = close * 1.02
    low = close * 0.98
    volume = pd.DataFrame(1e6, index=idx, columns=codes)
    return {
        "Close": close, "Open": open_df, "High": high, "Low": low,
        "Volume": volume, "Amount": volume * close,
        "ErrorId": "0",
    }


def _make_formula_result(codes, dates):
    """构造 TDX formula_process_mul_xg 返回 (模拟真实格式).

    真实格式: {stock_code: {indicator_name: [{'Date': '...', 'Value': '1'}]}, 'ErrorId': '0'}
    """
    result = {"ErrorId": "0"}
    for code in codes:
        indicators = {}
        # formula_process_mul_xg 返回的 key 是指标名 (通常与 formula_name 相同)
        records = [{"Date": str(d), "Value": "1"} for d in dates]
        indicators["QUANTQQ"] = records
        result[code] = indicators
    return result


# ── tests ──────────────────────────────────────────────────


class TestFormulaRunnerMock:
    """FormulaRunner + mock connector — 选股边界."""

    def test_empty_selection_returns_empty_df(self):
        """mock 返空 → FormulaRunner 返空 DataFrame."""
        mock = FakeConnector(formula_result=[])
        FormulaRunner.set_connector(mock)

        picks = FormulaRunner.run_stock_selection_with_dates(
            "QUANTQQ", stock_list=["000001.SZ"],
            start_time="20260101", end_time="20260115",
        )
        assert picks.empty

    def test_selection_returns_picks_with_dates(self):
        """mock 有入选 → DataFrame 含 stock_code + select_date."""
        mock = FakeConnector(
            formula_result=_make_formula_result(["000001.SZ", "600519.SH"],
                                                [20260105, 20260108]),
        )
        FormulaRunner.set_connector(mock)

        picks = FormulaRunner.run_stock_selection_with_dates(
            "QUANTQQ", stock_list=["000001.SZ", "600519.SH"],
            start_time="20260101", end_time="20260115",
        )
        assert len(picks) >= 1
        assert "stock_code" in picks.columns
        assert "select_date" in picks.columns


class TestEngineWithMockData:
    """BacktestEngine + mock DataFetcher — 回测边界."""

    def test_engine_runs_with_mock_ohlc(self):
        """mock OHLC + picks → 引擎不崩溃, equity_curve 非空."""
        codes = ["000001.SZ", "600519.SH"]
        base = {"000001.SZ": 10.0, "600519.SH": 1800.0}

        mock = FakeConnector(ohlc_data=_make_ohlc(codes, 10, base))
        DataFetcher.set_connector(mock)

        picks = pd.DataFrame({
            "stock_code": codes,
            "select_date": pd.to_datetime(["2026-01-05", "2026-01-08"]),
            "formula_name": "QUANTQQ",
        })
        engine = BacktestEngine({
            "initial_capital": 10000000, "commission": 0.0003,
            "slippage": 0.001, "freq": "1d",
            "position_sizing": {"min_buy_amount": 2000, "max_buy_amount": 20000,
                                "lot_size": 100, "min_lots": 1},
        })
        result = engine.run(
            selections=picks,
            start_time="20260101", end_time="20260115",
            stop_config={
                "cost_stop": {"enabled": False},
                "trailing_stop": {"enabled": False},
                "ladder_tp": {"enabled": False},
                "time_stop": {"enabled": False},
            },
        )
        # 引擎不崩溃, 有合理的返回值
        assert result is not None
        assert "metrics" in result
        eq = result.get("equity_curve")
        assert eq is not None and len(eq) > 0, "equity_curve 不应为空"

    def test_entry_price_is_close_not_open(self):
        """业务铁律 2: 回测买入价 = 信号日 T 收盘价 (不是开盘价)."""
        codes = ["000001.SZ"]
        n_bars = 10
        base = 10.0

        ohlc = _make_ohlc(codes, n_bars, {"000001.SZ": base})
        close_series = ohlc["Close"]["000001.SZ"]

        mock = FakeConnector(ohlc_data=ohlc)
        DataFetcher.set_connector(mock)

        # 信号日: 2026-01-05 = bdate_range 的第 0 个 bar
        signal_date = pd.Timestamp("2026-01-05")
        expected_close = float(close_series.loc[signal_date])

        picks = pd.DataFrame({
            "stock_code": codes * 3,
            "select_date": pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"]),
            "formula_name": "QUANTQQ",
        })
        engine = BacktestEngine({
            "initial_capital": 10000000, "commission": 0.0003,
            "slippage": 0.001, "freq": "1d",
            "position_sizing": {"min_buy_amount": 2000, "max_buy_amount": 20000,
                                "lot_size": 100, "min_lots": 1},
        })
        result = engine.run(
            selections=picks,
            start_time="20260101", end_time="20260115",
            stop_config={
                "cost_stop": {"enabled": False},
                "trailing_stop": {"enabled": False},
                "ladder_tp": {"enabled": False},
                "time_stop": {"enabled": False},
            },
        )
        trades = result.get("trades", pd.DataFrame())
        assert len(trades) > 0, "应至少产生一笔交易"
        # 业务铁律 2: entry_price 必须等于信号日 close（非开盘价）
        first_trade = trades.iloc[0]
        assert first_trade["entry_price"] == pytest.approx(expected_close, rel=0.01), (
            f"entry_price={first_trade['entry_price']}, "
            f"expected close={expected_close} (信号日收盘价)"
        )


class TestPipelineFaultTolerance:
    """Pipeline 故障路径 — TDX 连接失败."""

    def test_pipeline_tdx_failure_returns_error(self, monkeypatch):
        """TdxConnector.initialize() 抛异常 → PipelineResult.error 非空."""
        from core import connector as conn_module
        from pipeline.pipeline import Pipeline
        from utils.config_loader import ConfigLoader

        def fake_init():
            raise ConnectionError("TDX 连接失败 (模拟)")

        monkeypatch.setattr(conn_module.TdxConnector, "initialize",
                            staticmethod(fake_init))

        # 用最小合法 YAML 内容让 Pipeline 构造成功
        monkeypatch.setattr(ConfigLoader, "load_strategy",
                            lambda path, default=None: {
                                "strategy": {"name": "test"},
                                "selection": {"formula_name": "QUANTQQ",
                                              "period": "1d", "dividend_type": 1,
                                              "universe": {"type": "50", "exclude_st": False}},
                                "backtest": {"initial_capital": 100000,
                                             "commission": 0.0003, "slippage": 0.001,
                                             "freq": "1d"},
                                "time_range": {"start": "20260101", "end": "20260115"},
                            })

        pipe = Pipeline("dummy.yaml")
        result = pipe.run()

        assert result.error is not None
        assert "TDX" in result.error.upper() or "连接" in result.error
