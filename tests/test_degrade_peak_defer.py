"""降级天推迟峰值刷新 (2026-08-06 审计 HIGH#2, 用户拍板"推迟峰值") 守卫测试.

问题: 5m 降级天 48 根 bar 广播同一 1d OHLC (degrade_5m.py:202), 旧逻辑
首根 bar 就把峰值刷成当日 1d high (loop.py 原 :238) → real 模式
`peak_hi == bar.high` 对全天 48 根成立 (trailing.py:123 创峰值跳过) →
冲高回落的峰顶日整天不判 trailing (漏判)。

修复: 降级 bar 不刷新 high_hi (loop._sell_bar deg 守卫), 旧线继续值班,
次日正常 bar 恢复刷新 —— 等同 1D 模式 "峰值日多观察一天" 的既有代价。

场景统一: bpday=2, 3 天 6 根 bar, 1 只股票, 仅开 trailing
(activation 5%, drawdown 10%, confirm="real"), T+1 (day0 买入 day1 起可卖)。
"""
from __future__ import annotations

import numpy as np
import pytest

from backtest.loop import build_backtest_loop
from backtest.loop.state import TradeColumns


def _run(close, entry, high, low, open_, degraded):
    """6 根 bar 单票最小回测, 只开 real 模式 trailing。"""
    loop = build_backtest_loop(
        1_000_000.0, 0.0,                 # 资金, 佣金
        100.0, 1e9, 100, 1,               # min/max 买入额, 一手, 最小手数
        False, -0.12,                     # cost_stop 关
        True, 0.05, 0.10,                 # trailing: 激活 5%, 回撤 10%
        False, np.array([]), np.array([]), 0,   # ladder 关
        False, 20,                        # time_stop 关
        False, 7, 0.01,                   # cond_time 关
        bpday=2,
        trailing_confirm="real",
    )
    return loop.run(close, entry, high, low, open_, None, None, None,
                    degraded_np=degraded)


def _m(rows):
    """list[list] → (6,1) float64 列矩阵。"""
    return np.array(rows, dtype=np.float64).reshape(-1, 1)


# day0: 10 买入, 盘中 high 11.0 (峰值 +10% ≥ 激活 5%)
# day1 (降级): 1d OHLC = 开 11.5 / 高 12.0 / 低 9.8 / 收 10.5 广播两根
# day2: 正常回落
_CLOSE = _m([10.0, 10.5, 10.5, 10.5, 10.3, 10.2])
_HIGH = _m([10.2, 11.0, 12.0, 12.0, 10.6, 10.5])
_LOW = _m([9.9, 10.2, 9.8, 9.8, 10.1, 10.0])
_OPEN = _m([10.0, 10.3, 11.5, 11.5, 10.4, 10.3])
_DEGRADED = np.array([False, False, True, True, False, False]).reshape(-1, 1)
_NO_DEGRADE = np.zeros((6, 1), dtype=bool)


def _entry():
    e = np.zeros((6, 1), dtype=bool)
    e[0, 0] = True
    return e


def test_degraded_day_old_line_on_duty():
    """降级天不刷峰值: 旧峰值 11 的线 9.9 继续值班, 当日 low 9.8 触线 →
    按线价 9.9 卖出 (bar2)。旧逻辑: 峰值被刷成 12, 全天 peak==high 跳过,
    次日才以 10.4 成交 —— 峰顶日漏判实锤。"""
    _, trades = _run(_CLOSE, _entry(), _HIGH, _LOW, _OPEN, _DEGRADED)
    assert len(trades) == 1
    tr = trades[0]
    assert tr[TradeColumns.EXIT_IDX] == 2            # 降级当天就判 (用旧线)
    assert tr[TradeColumns.SELL_PX] == pytest.approx(9.9)   # 11 × 0.9 线价
    assert tr[TradeColumns.REASON] == 4              # 低于成本 → 移动止损


def test_degraded_day_high_never_enters_peak():
    """降级日的 1d high (12.0) 永不进入峰值: 次日正常 bar high 11.2 创峰 →
    线 = 11.2×0.9 = 10.08 (而非 12×0.9 = 10.8)。bar5 low 9.9 触线 →
    按 10.08 止盈。若峰值含 12, 会早一根 bar 以 10.5 跳空价成交。"""
    close = _m([10.0, 10.5, 10.6, 10.6, 10.8, 10.0])
    high = _m([10.2, 11.0, 12.0, 12.0, 11.2, 10.6])
    low = _m([9.9, 10.2, 10.2, 10.2, 10.3, 9.9])
    open_ = _m([10.0, 10.3, 10.8, 10.8, 10.5, 10.4])
    _, trades = _run(close, _entry(), high, low, open_, _DEGRADED)
    assert len(trades) == 1
    tr = trades[0]
    assert tr[TradeColumns.EXIT_IDX] == 5
    assert tr[TradeColumns.SELL_PX] == pytest.approx(11.2 * 0.9)
    assert tr[TradeColumns.REASON] == 8              # 高于成本 → 移动止盈


def test_non_degraded_day_unchanged():
    """对照组: 同一价格序列但 degraded_np 全 False → 维持旧行为
    (当天 high 12 刷峰值, 创峰值 bar 跳过, 次日跳空分支按开盘价 10.4 出)。
    证明行为切换完全由 degraded 标志驱动, 非降级路径零影响。"""
    _, trades = _run(_CLOSE, _entry(), _HIGH, _LOW, _OPEN, _NO_DEGRADE)
    assert len(trades) == 1
    tr = trades[0]
    assert tr[TradeColumns.EXIT_IDX] == 4
    assert tr[TradeColumns.SELL_PX] == pytest.approx(10.4)
    assert tr[TradeColumns.REASON] == 8
