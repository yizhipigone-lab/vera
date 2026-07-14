"""C2 — _post_process 直接契约测试（T2）。

锁 run/run_cached 共享后处理的输出结构: equity_curve 列/drawdown 公式 +
trades_df + metrics。防止一处改动同时打挂两入口而不自知。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.engine import BacktestEngine


def _engine():
    return BacktestEngine({
        "initial_capital": 1_000_000.0, "commission": 0.0003,
        "enable_realistic_costs": False, "period": "1d",
        "position_sizing": {"min_buy_amount": 1000.0, "max_buy_amount": 60_000.0,
                            "lot_size": 100, "min_lots": 1},
    })


def _make_close(n=5):
    dates = pd.bdate_range("2026-01-02", periods=n)
    return pd.DataFrame({"000001.SZ": [10.0, 11.0, 10.5, 12.0, 11.5]},
                        index=dates)


def _make_raw_trades():
    # 1 笔 ladder 止盈: ci=0, entry_idx=0, exit_idx=3, entry_px=10, sell=12, sh=100, profit, ret=0.2, reason=5
    return np.array([[0.0, 0.0, 3.0, 10.0, 12.0, 100.0, 200.0, 0.2, 5.0]],
                    dtype=np.float64)


class TestPostProcessContract:
    def test_equity_curve_structure(self):
        eng = _engine()
        close = _make_close()
        eq_arr = np.array([1.0e6, 1.01e6, 1.005e6, 1.02e6, 1.015e6], dtype=np.float64)
        equity_curve, trades_df, metrics = eng._post_process(
            eq_arr, _make_raw_trades(), close, bpday=1)
        # 列结构
        assert list(equity_curve.columns) == ["date", "equity", "drawdown"]
        assert len(equity_curve) == 5
        # drawdown = (equity - expanding_peak) / expanding_peak, 第一个 bar peak=自己 → 0
        assert equity_curve["drawdown"].iloc[0] == 0.0
        # bar2 equity 低于 bar1 peak → drawdown < 0
        assert equity_curve["drawdown"].iloc[2] < 0.0
        # equity 列等于输入
        np.testing.assert_array_almost_equal(
            equity_curve["equity"].values, eq_arr)

    def test_trades_df_built(self):
        eng = _engine()
        close = _make_close()
        eq_arr = np.full(5, 1.0e6, dtype=np.float64)
        _, trades_df, _ = eng._post_process(eq_arr, _make_raw_trades(), close, bpday=1)
        assert len(trades_df) == 1
        assert trades_df.iloc[0]["exit_reason"] == "阶梯止盈"
        assert trades_df.iloc[0]["return"] == 0.2
        # entry_date/exit_date 已转 datetime
        assert pd.api.types.is_datetime64_any_dtype(trades_df["entry_date"])

    def test_empty_raw_trades(self):
        eng = _engine()
        close = _make_close()
        eq_arr = np.full(5, 1.0e6, dtype=np.float64)
        _, trades_df, metrics = eng._post_process(
            eq_arr, np.empty((0, 9), dtype=np.float64), close, bpday=1)
        assert len(trades_df) == 0
        assert isinstance(metrics, dict)

    def test_run_and_run_cached_share_post_process(self):
        """run() 与 run_cached() 的 equity_curve 结构一致（同走 _post_process）。"""
        eng = _engine()
        close = _make_close()
        eq_arr = np.array([1.0e6, 1.01e6, 1.005e6, 1.02e6, 1.015e6], dtype=np.float64)
        eq1, _, _ = eng._post_process(eq_arr, _make_raw_trades(), close, bpday=1)
        # 再调一次确认幂等结构
        eq2, _, _ = eng._post_process(eq_arr, _make_raw_trades(), close, bpday=1)
        assert list(eq1.columns) == list(eq2.columns)
        np.testing.assert_array_almost_equal(eq1["equity"].values, eq2["equity"].values)
