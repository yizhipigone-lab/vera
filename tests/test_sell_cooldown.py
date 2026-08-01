"""卖出冷却 (sell_cooldown_bars, 2026-07-23) 回测循环测试。

语义: 全清仓后 N bar 内禁止同票重新买入 (仅约束空仓后的新买;
持仓中的换股 reason=1 不受影响)。默认 0=关闭, 零行为变化。
"""
from __future__ import annotations

import numpy as np

from backtest.loop import build_backtest_loop
from backtest.loop.state import TradeColumns


def _run(cooldown_bars: int, n_dates: int = 40):
    """1 只票恒定 10 元, 信号在 bar 0/10/30, 仅时间止损 5 bar。"""
    price = np.full((n_dates, 1), 10.0, dtype=np.float64)
    entry = np.zeros((n_dates, 1), dtype=bool)
    entry[0, 0] = entry[10, 0] = entry[30, 0] = True
    loop = build_backtest_loop(
        100000.0, 0.0,                # capital, commission
        1000.0, 5000.0, 100, 1,       # min/max buy, lot, min_lots
        False, -0.12,                 # cost_stop off
        False, 0.035, 0.01,           # trailing off
        False, np.array([]), np.array([]), 0,   # ladder off
        True, 5,                      # time_stop on, 5 bar
        False, 7, 0.01,               # cond_time off
        sell_cooldown_bars=cooldown_bars,
    )
    _, trades = loop.run(price, entry, None, None, None, None, None, None)
    return trades


class TestSellCooldown:
    def test_cooldown_off_buys_every_signal(self):
        """冷却关闭 (默认): 0/10/30 三个信号都成交。"""
        trades = _run(0)
        entries = sorted(int(t[TradeColumns.ENTRY_IDX]) for t in trades)
        assert entries == [0, 10, 30]

    def test_cooldown_blocks_rebuy_within_window(self):
        """冷却 20 bar: bar5 卖出 → bar10 信号被压 (距卖出 5<20), bar30 放行 (25>=20)。"""
        trades = _run(20)
        entries = sorted(int(t[TradeColumns.ENTRY_IDX]) for t in trades)
        assert entries == [0, 30]

    def test_cooldown_counts_skips(self):
        loop = build_backtest_loop(
            100000.0, 0.0,
            1000.0, 5000.0, 100, 1,
            False, -0.12,
            False, 0.035, 0.01,
            False, np.array([]), np.array([]), 0,
            True, 5,
            False, 7, 0.01,
            sell_cooldown_bars=20,
        )
        price = np.full((40, 1), 10.0, dtype=np.float64)
        entry = np.zeros((40, 1), dtype=bool)
        entry[0, 0] = entry[10, 0] = entry[30, 0] = True
        loop.run(price, entry, None, None, None, None, None, None)
        assert loop.entry_engine.cooldown_skip_count == 1

    def test_swap_not_gated_by_cooldown(self):
        """持仓中的换股 (同票新信号卖旧买新) 不受冷却约束:
        冷却 20, 信号 bar 0 和 bar 3 (持仓中) → bar3 换股成立, 之后 bar6 卖出,
        bar10 信号距卖出 4 < 20 → 被压。只有 2 次买入 (bar0 新买 + bar3 换股)。"""
        n = 40
        price = np.full((n, 1), 10.0, dtype=np.float64)
        entry = np.zeros((n, 1), dtype=bool)
        entry[0, 0] = entry[3, 0] = entry[10, 0] = True
        loop = build_backtest_loop(
            100000.0, 0.0,
            1000.0, 5000.0, 100, 1,
            False, -0.12,
            False, 0.035, 0.01,
            False, np.array([]), np.array([]), 0,
            True, 5,
            False, 7, 0.01,
            sell_cooldown_bars=20,
        )
        _, trades = loop.run(price, entry, None, None, None, None, None, None)
        entries = sorted(int(t[TradeColumns.ENTRY_IDX]) for t in trades)
        reasons = sorted(int(t[TradeColumns.REASON]) for t in trades)
        # 换股 (reason=1) 在 bar3 发生 → 证明换股未被冷却拦截
        assert 1 in reasons
        assert entries == [0, 3]
