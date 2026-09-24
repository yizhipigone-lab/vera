"""day_lo 日首哨兵守卫测试 (2026-08-06 审计 HIGH#4, loop 级).

bug 场景: 5m 日频确认模式 (low/close) 用 ctx.day_lo 对回撤线。旧逻辑在日首
bar 重置 day_lo, 但临时停牌 continue 在重置之前 —— 日首 bar 停牌、当日后续
bar 恢复交易时, 当天 day_lo 沿用昨日低点: 昨日低点若破线, 末根 bar 误判触发
(拿昨天的低点审判今天的线)。

修复 (2026-08-06, 用户实现): 日首哨兵 —— 循环顶 (停牌 continue 之前) 先把
day_lo 置 inf, 当日首个可交易 bar 用 min 填充 (loop.py:185/217)。

本测试场景 (bpday=2, 3 天, 仅 trailing confirm="low", 激活 5% 回撤 10%):
  day0: 10 买入, high 11.0 创峰 (线 9.9), low 9.85 (昨日低点, 破线)
  day1 bar0: 停牌 (tradable=False); bar1: 恢复, low 10.2 (> 9.9, 不碰线)
  旧逻辑: day1 bar0 停牌跳过重置 → bar1 用陈旧 9.85 ≤ 9.9 → 误卖 @ 10.4
  哨兵修复: bar0 先置 inf → bar1 填 10.2 → 不触发, 持仓到底
"""
from __future__ import annotations

import numpy as np

from backtest.loop import build_backtest_loop


def _m(rows):
    return np.array(rows, dtype=np.float64).reshape(-1, 1)


def test_suspended_day_first_bar_stale_day_lo_no_false_trigger():
    close = _m([10.0, 10.5, 10.3, 10.4, 10.4, 10.4])
    high = _m([10.2, 11.0, 10.3, 10.5, 10.5, 10.5])
    low = _m([9.95, 9.85, 10.3, 10.2, 10.2, 10.2])
    open_ = _m([10.0, 10.3, 10.3, 10.3, 10.4, 10.4])
    entry = np.zeros((6, 1), dtype=bool)
    entry[0, 0] = True
    # day1 bar0 (i=2) 停牌, 当日 bar1 (i=3) 恢复
    tradable = np.ones((6, 1), dtype=bool)
    tradable[2, 0] = False
    last_tradable = np.array([5], dtype=np.int64)

    loop = build_backtest_loop(
        1_000_000.0, 0.0,
        100.0, 1e9, 100, 1,
        False, -0.12,                       # cost_stop 关
        True, 0.05, 0.10,                   # trailing: 激活 5%, 回撤 10%
        False, np.array([]), np.array([]), 0,   # ladder 关
        False, 20,                          # time_stop 关
        False, 7, 0.01,                     # cond_time 关
        bpday=2,
        trailing_confirm="low",             # 日频最低价确认 (用 day_lo 的模式)
    )
    _, trades = loop.run(close, entry, high, low, open_,
                         tradable, last_tradable, None)
    # 哨兵修复: day1 末根 bar 的 day_lo=10.2 > 线 9.9 → 不触发, 零成交
    assert len(trades) == 0, (
        "day_lo 哨兵失效? 停牌日次日误用了昨日低点 9.85 判线 "
        f"(trades={trades.tolist()})")
