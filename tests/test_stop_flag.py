"""协作式停止标志 (core/stop_flag.py) + 停止回测按钮链路测试 (2026-07-18)。

覆盖:
1. stop_flag 基本语义 (clear/request/raise)
2. BacktestLoop.run 在标志置位时抛 BacktestStoppedError (web「停止回测」核心链路)
3. 标志未置位时 loop 正常跑完 (向后兼容 — 批量脚本/CLI 从不置位)
"""

import numpy as np
import pytest

from core.stop_flag import (
    BacktestStoppedError, clear_stop, request_stop, stop_requested,
    raise_if_stopped,
)


@pytest.fixture(autouse=True)
def _clean_flag():
    """每个用例前后都清标志, 防串扰 (全局单例)。"""
    clear_stop()
    yield
    clear_stop()


class TestStopFlagBasics:
    def test_default_not_requested(self):
        assert not stop_requested()
        raise_if_stopped()  # 不抛

    def test_request_then_raise(self):
        request_stop()
        assert stop_requested()
        with pytest.raises(BacktestStoppedError):
            raise_if_stopped()

    def test_clear_resets(self):
        request_stop()
        clear_stop()
        assert not stop_requested()
        raise_if_stopped()  # 不抛

    def test_request_idempotent(self):
        request_stop()
        request_stop()  # 重复置位不炸
        assert stop_requested()


class TestBacktestLoopStops:
    """web 停止按钮核心链路: 标志置位 → 逐 bar 循环下一轮即抛。"""

    def _build_loop(self):
        from backtest.loop import build_backtest_loop
        return build_backtest_loop(
            1_000_000.0, 0.0003, 1000.0, 200_000.0, 100, 1,
            True, -0.20,        # cost_stop
            False, 0.05, 0.10,  # trailing 禁用
            False, np.array([0.06, 0.15], dtype=np.float64),
            np.array([0.5, 0.5], dtype=np.float64), 2,  # ladder 禁用
            False, 10,          # time 禁用
            False, 3, 0.08,     # cond_time 禁用
            False, 0.03,        # first_day 禁用
            1, 0.0, 0.001, 1.0, False, False, None, 1.0, 1,
        )

    def _data(self, n=10):
        price = np.full((n, 1), 10.0)
        entry = np.zeros((n, 1), dtype=bool)
        entry[0, 0] = True
        return price, entry, price.copy(), price.copy(), price.copy()

    def test_loop_raises_when_stopped(self):
        request_stop()
        loop = self._build_loop()
        price, entry, high, low, op = self._data()
        with pytest.raises(BacktestStoppedError):
            loop.run(price, entry, high, low, op, None, None, None)

    def test_loop_runs_clean_when_not_stopped(self):
        """标志未置位 → 行为与加检查点前完全一致 (向后兼容)。

        bar3 跌 30% 触发硬止损 (-20%), 应产生一笔卖出交易。
        """
        loop = self._build_loop()
        price, entry, high, low, op = self._data()
        price[3:, 0] = 7.0; high[3:, 0] = 7.0; low[3:, 0] = 7.0; op[3:, 0] = 7.0
        eq, trades = loop.run(price, entry, high, low, op, None, None, None)
        assert eq.shape == (10,)
        assert trades.shape[0] >= 1  # 硬止损卖出应落账
