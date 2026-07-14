"""ATR 接生产路径测试 — stop_config['atr_stop'] 经 run_cached 端到端。

验证 run_cached 内部从 high/low/close 预算 ATR 矩阵, ATR 触发产生 reason=13 交易。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.engine import BacktestEngine


def _engine():
    return BacktestEngine({
        "initial_capital": 1_000_000.0, "commission": 0.0003,
        "enable_realistic_costs": False, "period": "1d",
        "position_sizing": {"min_buy_amount": 1000.0, "max_buy_amount": 200_000.0,
                            "lot_size": 100, "min_lots": 1},
    })


def _make_data(n=30):
    """构造冲高后回撤的数据, 让 ATR 触发。"""
    rng = np.random.default_rng(42)
    dates = pd.bdate_range("2026-01-02", periods=n)
    cols = ["000001.SZ"]
    # 前 15 根温和上行到 12, 后续回撤到 9
    price = np.concatenate([
        np.linspace(10, 12, 15), np.linspace(12, 9, n - 15)])
    price = price.reshape(-1, 1)
    high = price * (1 + np.abs(rng.normal(0, 0.01, price.shape)))
    low = price * (1 - np.abs(rng.normal(0, 0.01, price.shape)))
    op = price * (1 + rng.normal(0, 0.005, price.shape))
    close = pd.DataFrame(price, index=dates, columns=cols)
    high_df = pd.DataFrame(high, index=dates, columns=cols)
    low_df = pd.DataFrame(low, index=dates, columns=cols)
    op_df = pd.DataFrame(op, index=dates, columns=cols)
    entries = pd.DataFrame(False, index=dates, columns=cols)
    entries.iloc[0, 0] = True
    return close, high_df, low_df, op_df, entries


def _stop(atr_enabled=True, priority="stop_first"):
    return {
        "priority": priority,
        "cost_stop": {"enabled": True, "threshold": -0.30},  # 阈值深, 让 ATR 先
        "trailing_stop": {"enabled": False},
        "ladder_tp": {"enabled": False, "levels": []},
        "time_stop": {"enabled": False, "max_hold_days": 60},
        "cond_time_stop": {"enabled": False},
        "first_day": {"enabled": False},
        "formula_sell": {"enabled": False, "formula_name": "", "formula_arg": "",
                         "sell_ratio": 1.0, "priority": 0},
        "capabilities": {"formula_exit": True, "gap_protection": True, "delisting": True},
        "atr_stop": {"enabled": atr_enabled, "period": 14, "multiplier": 3.0},
    }


class TestATRviaRunCached:
    def test_atr_fires_through_run_cached(self):
        """stop_config['atr_stop'] 经 run_cached → ATR 触发 reason=13。"""
        close, high, low, op, entries = _make_data()
        eng = _engine()
        res = eng.run_cached(
            close, entries, high.values.astype(np.float64), low.values.astype(np.float64),
            _stop(), None, np.array([]), np.array([]), 0,
            filter_limit_up=False, return_raw=True,
            open_np=op.values.astype(np.float64),
        )
        trades = res["raw_trades"]
        assert trades.shape[0] >= 1, "应有交易"
        reasons = trades[:, 8]
        assert 13.0 in reasons, f"应含 ATR(reason=13) 交易, reasons={reasons}"

    def test_atr_disabled_no_reason_13(self):
        """atr_stop.enabled=False → 不产 reason=13。"""
        close, high, low, op, entries = _make_data()
        eng = _engine()
        res = eng.run_cached(
            close, entries, high.values.astype(np.float64), low.values.astype(np.float64),
            _stop(atr_enabled=False), None, np.array([]), np.array([]), 0,
            filter_limit_up=False, return_raw=True,
            open_np=op.values.astype(np.float64),
        )
        trades = res["raw_trades"]
        reasons = trades[:, 8] if trades.shape[0] else np.array([])
        assert 13.0 not in reasons, f"ATR 禁用不应有 reason=13, reasons={reasons}"

    def test_atr_without_high_low_disables(self):
        """atr_stop.enabled=True 但 high_np/low_np=None → ATR 强制禁用, 不崩。"""
        close, _, _, _, entries = _make_data()
        eng = _engine()
        res = eng.run_cached(
            close, entries, None, None,
            _stop(atr_enabled=True), None, np.array([]), np.array([]), 0,
            filter_limit_up=False, return_raw=True,
        )
        # 不崩 + 无 reason=13
        trades = res["raw_trades"]
        reasons = trades[:, 8] if trades.shape[0] else np.array([])
        assert 13.0 not in reasons

    def test_atr_reason_13_labeled_in_trades_df(self):
        """reason=13 在 trades_df 显示 'ATR止损' (reason_map 13.0)。"""
        close, high, low, op, entries = _make_data()
        eng = _engine()
        res = eng.run_cached(
            close, entries, high.values.astype(np.float64), low.values.astype(np.float64),
            _stop(), None, np.array([]), np.array([]), 0,
            filter_limit_up=False,
            open_np=op.values.astype(np.float64),
        )
        trades_df = res["trades"]
        if not trades_df.empty:
            reasons = set(trades_df["exit_reason"].unique())
            assert "ATR止损" in reasons, f"应含 'ATR止损' label, got {reasons}"
