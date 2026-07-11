"""
阶梯止盈/成本止损优先级开关测试 (2026-07-05)

验证 _simulate_core_v3 的 ladder_tp_first 参数:
  - ladder_tp_first=False (stop_first, 历史默认): 同 bar 双触发 → cost_stop 先赢 → reason=3
  - ladder_tp_first=True  (ladder_tp_first 新模式): 同 bar 双触发 → ladder_tp 先赢 → reason=5

不依赖 TDX, 直接调 _simulate_core_v3 + 合成数据。

买入语义: bar 5 信号 → bar 5 当日收盘买入 (pos_entry_idx=5), bar 6 首次进卖出循环 (T+1).
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest

from backtest.engine import _simulate_core_v3


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _make_dual_trigger_market():
    """
    1 只股票 / 10 天 / 平盘 500 元。

    bar 5: entry 信号 → 当日收盘买入, ep=500
    bar 6: high=540 (hi_pp=+8%) + low=455 (lo_pp=-9%) → 同时触发 ladder_tp(6%) 和 cost_stop(-8%)
    其他 bar: high=505, low=495 (不触发任何机制)
    """
    n_dates, n_stocks = 10, 1
    dates = pd.bdate_range("2024-01-02", periods=n_dates)
    columns = pd.Index(["600519.SH"])

    close = pd.DataFrame(np.full((n_dates, n_stocks), 500.0), index=dates, columns=columns)
    high = pd.DataFrame(np.full((n_dates, n_stocks), 505.0), index=dates, columns=columns)
    low = pd.DataFrame(np.full((n_dates, n_stocks), 495.0), index=dates, columns=columns)
    # bar 6 双触发
    high.iloc[6, 0] = 540.0   # hi_pp = (540-500)/500 = 0.08 ≥ 0.06 → ladder_tp 触发
    low.iloc[6, 0] = 455.0    # lo_pp = (455-500)/500 = -0.09 ≤ -0.08 → cost_stop 触发

    entries = pd.DataFrame(False, index=dates, columns=columns)
    entries.iloc[5, 0] = True
    return close, high, low, entries


def _make_args(close, entries, high, low, **kwargs):
    """构造 _simulate_core_v3 的标准参数, 关闭除 cost_stop / ladder_tp 外的所有机制."""
    base = dict(
        price_np=close.values.astype(np.float64),
        entry_np=entries.values,
        initial_capital=1_000_000.0,
        commission=0.0003,
        min_buy_amount=1000.0,
        max_buy_amount=60_000.0,
        lot_size=100,
        min_lots=1,
        cost_stop_enabled=True, cost_stop_threshold=-0.08,    # bar 6 lo_pp=-0.09 触发
        trailing_enabled=False, trailing_activation=0.50, trailing_drawdown=0.30,
        ladder_enabled=True,
        ladder_profits=np.array([0.06], dtype=np.float64),    # bar 6 hi_pp=0.08 触发
        ladder_ratios=np.array([1.0], dtype=np.float64),      # 单档全卖, 触发即清仓
        n_ladder=1,
        time_enabled=False, max_hold_days=9999,
        cond_time_enabled=False, cond_time_days=9999, cond_time_profit=0.99,
        first_day_enabled=False, first_day_target=0.99, first_day_n_bars=1,
        high_np=high.values.astype(np.float64),
        low_np=low.values.astype(np.float64),
        bpday=1,
        slippage=0.0, stamp_tax=0.0,
        open_np=None,
    )
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Test 1: stop_first (历史默认) → 同 bar 双触发 → reason=3 (cost_stop)
#   简化模式 sell_price = ep * (1 + cost_stop_threshold) = 500 * 0.92 = 460
#   profit_pct = (sell_price - ep) / ep = -0.08
# ---------------------------------------------------------------------------
def test_stop_first_cost_stop_wins():
    """ladder_tp_first=False: cost_stop 先检查, 双触发时 reason=3, 成交价按 cost_stop 算."""
    close, high, low, entries = _make_dual_trigger_market()
    args = _make_args(close, entries, high, low, ladder_tp_first=False)
    equity_arr, raw_trades = _simulate_core_v3(**args)

    real_trades = [t for t in raw_trades if t[8] != 0.0 or t[0] != 0.0]
    assert len(real_trades) >= 1, f"应有至少 1 笔交易, 实际 {len(real_trades)}"

    reasons = [t[8] for t in real_trades]
    assert 3.0 in reasons, f"stop_first 模式应触发 cost_stop (reason=3), 实际 reasons={reasons}"
    assert 5.0 not in reasons, f"stop_first 模式不应让 ladder_tp 抢先 (reason=5), 实际 reasons={reasons}"

    # 2026-07-05: 断言成交价/收益率, 防止"reason 对但成交价错"的假阳性
    cost_stop_trade = next(t for t in real_trades if t[8] == 3.0)
    assert cost_stop_trade[3] == 500.0, f"入场价应=500, 实际 {cost_stop_trade[3]}"
    assert cost_stop_trade[4] == 460.0, f"cost_stop 成交价应=460 (ep*0.92), 实际 {cost_stop_trade[4]}"
    assert abs(cost_stop_trade[7] - (-0.08)) < 1e-9, f"cost_stop 收益率应=-8%, 实际 {cost_stop_trade[7]}"


# ---------------------------------------------------------------------------
# Test 2: ladder_tp_first (新模式) → 同 bar 双触发 → reason=5 (ladder_tp)
#   简化模式 sell_price = ep * (1 + ladder_profits[0]) = 500 * 1.06 = 530
#   profit_pct = (sell_price - ep) / ep = +0.06
# ---------------------------------------------------------------------------
def test_ladder_tp_first_ladder_tp_wins():
    """ladder_tp_first=True: ladder_tp 先检查, 双触发时 reason=5, 成交价按 ladder_tp 算."""
    close, high, low, entries = _make_dual_trigger_market()
    args = _make_args(close, entries, high, low, ladder_tp_first=True)
    equity_arr, raw_trades = _simulate_core_v3(**args)

    real_trades = [t for t in raw_trades if t[8] != 0.0 or t[0] != 0.0]
    assert len(real_trades) >= 1, f"应有至少 1 笔交易, 实际 {len(real_trades)}"

    reasons = [t[8] for t in real_trades]
    assert 5.0 in reasons, f"ladder_tp_first 模式应触发 ladder_tp (reason=5), 实际 reasons={reasons}"
    assert 3.0 not in reasons, f"ladder_tp_first 模式不应让 cost_stop 抢先 (reason=3), 实际 reasons={reasons}"

    # 2026-07-05: 断言成交价/收益率, 验证 ladder_tp_first 模式成交价确实按 ladder_tp 算
    ladder_trade = next(t for t in real_trades if t[8] == 5.0)
    assert ladder_trade[3] == 500.0, f"入场价应=500, 实际 {ladder_trade[3]}"
    assert ladder_trade[4] == 530.0, f"ladder_tp 成交价应=530 (ep*1.06), 实际 {ladder_trade[4]}"
    assert abs(ladder_trade[7] - 0.06) < 1e-9, f"ladder_tp 收益率应=+6%, 实际 {ladder_trade[7]}"


# ---------------------------------------------------------------------------
# Test 2b: ladder_tp_first 模式收益 > stop_first 模式 (业务动机验证)
#   同 bar 双触发场景: ladder_tp_first 锁利 +6%, stop_first 触发止损 -8%
#   → ladder_tp_first 的总收益应高于 stop_first (这是做 priority 开关的核心动机)
# ---------------------------------------------------------------------------
def test_ladder_tp_first_higher_profit_than_stop_first():
    """同 bar 双触发: ladder_tp_first 模式收益 (锁利+6%) 应 > stop_first 模式收益 (止损-8%)."""
    close, high, low, entries = _make_dual_trigger_market()

    _, trades_stop = _simulate_core_v3(**_make_args(close, entries, high, low, ladder_tp_first=False))
    _, trades_tp = _simulate_core_v3(**_make_args(close, entries, high, low, ladder_tp_first=True))

    real_stop = [t for t in trades_stop if t[8] != 0.0 or t[0] != 0.0]
    real_tp = [t for t in trades_tp if t[8] != 0.0 or t[0] != 0.0]

    profit_stop = sum(t[6] for t in real_stop)  # [6] = pnl
    profit_tp = sum(t[6] for t in real_tp)

    assert profit_tp > profit_stop, (
        f"ladder_tp_first 模式 pnl ({profit_tp:.0f}) 应 > stop_first 模式 pnl ({profit_stop:.0f}); "
        f"如果 ladder_tp_first 不是锁利而是高估收益, 说明成交价计算有 bug"
    )


# ---------------------------------------------------------------------------
# Test 3: 仅 cost_stop 触发 (ladder 不触发) → 两种模式都是 reason=3
# ---------------------------------------------------------------------------
def test_only_cost_stop_triggered_both_modes_same():
    """只有 cost_stop 触发时, ladder_tp_first 不影响结果, 两种模式都 reason=3."""
    close, high, low, entries = _make_dual_trigger_market()
    # 改 bar 6: 只跌不涨 → 只触发 cost_stop
    high.iloc[6, 0] = 505.0   # hi_pp=0.01, 不触发 ladder (0.06)
    low.iloc[6, 0] = 455.0    # lo_pp=-0.09, 触发 cost_stop

    for ladder_tp_first in (False, True):
        args = _make_args(close, entries, high, low, ladder_tp_first=ladder_tp_first)
        _, raw_trades = _simulate_core_v3(**args)
        real_trades = [t for t in raw_trades if t[8] != 0.0 or t[0] != 0.0]
        reasons = [t[8] for t in real_trades]
        assert 3.0 in reasons, f"ladder_tp_first={ladder_tp_first}: 单 cost_stop 触发应 reason=3, 实际 {reasons}"


# ---------------------------------------------------------------------------
# Test 4: 仅 ladder_tp 触发 (cost_stop 不触发) → 两种模式都是 reason=5
# ---------------------------------------------------------------------------
def test_only_ladder_tp_triggered_both_modes_same():
    """只有 ladder_tp 触发时, ladder_tp_first 不影响结果, 两种模式都 reason=5."""
    close, high, low, entries = _make_dual_trigger_market()
    # 改 bar 6: 只涨不跌 → 只触发 ladder
    high.iloc[6, 0] = 540.0   # hi_pp=0.08, 触发 ladder
    low.iloc[6, 0] = 495.0    # lo_pp=-0.01, 不触发 cost_stop (-0.08)

    for ladder_tp_first in (False, True):
        args = _make_args(close, entries, high, low, ladder_tp_first=ladder_tp_first)
        _, raw_trades = _simulate_core_v3(**args)
        real_trades = [t for t in raw_trades if t[8] != 0.0 or t[0] != 0.0]
        reasons = [t[8] for t in real_trades]
        assert 5.0 in reasons, f"ladder_tp_first={ladder_tp_first}: 单 ladder 触发应 reason=5, 实际 {reasons}"


# ===========================================================================
# 2026-07-05 v3: trailing 语义改造 + trailing_first 优先级 (专家审计后)
#   - trailing 触发: 盘中 Low 触及回撤线 (peak_hi*(1-drawdown))
#   - trailing 执行价: 回撤线价 (不是 Close)
#   - L3 跳空低开保护: open < 回撤线价时按 open 成交
#   - L1 时序: 用"更新前"的 peak_hi
# ===========================================================================

def _make_trailing_market(activation=0.03, drawdown=0.01, low_pct=-0.06, close_pct=None):
    """
    构造 trailing 触发场景:
      bar 5: entry 信号 → ep=100
      bar 6: high=103 (peak_hi_profit=+3% ≥ activation=3% 激活)
             low=94   (lo_pp=-6%, 触及回撤线 103*0.99=101.97 → trailing 触发)
             close=96 (默认, 远低于回撤线 101.97)
    关键: low=94 ≤ 101.97 (回撤线) → trailing 触发; 但执行价应是 101.97 不是 94
    """
    n_dates, n_stocks = 10, 1
    dates = pd.bdate_range("2024-01-02", periods=n_dates)
    columns = pd.Index(["600519.SH"])

    close = pd.DataFrame(np.full((n_dates, n_stocks), 100.0), index=dates, columns=columns)
    high = pd.DataFrame(np.full((n_dates, n_stocks), 101.0), index=dates, columns=columns)
    low = pd.DataFrame(np.full((n_dates, n_stocks), 99.0), index=dates, columns=columns)

    # bar 6: 激活 + 回撤触发
    high.iloc[6, 0] = 100.0 * (1.0 + activation)          # 103 (激活)
    low.iloc[6, 0] = 100.0 * (1.0 + low_pct)              # 94 (触及回撤线 101.97)
    close.iloc[6, 0] = 100.0 * (1.0 + (close_pct if close_pct is not None else -0.04))  # 96 默认

    entries = pd.DataFrame(False, index=dates, columns=columns)
    entries.iloc[5, 0] = True
    return close, high, low, entries


# ---------------------------------------------------------------------------
# Test 5: trailing_first + 同 bar (trailing + cost_stop 双触发) → reason=8/4, 不为 3
# ---------------------------------------------------------------------------
def test_trailing_first_beats_cost_stop():
    """trailing_first: trailing 触发 (Low 触及回撤线) + cost_stop 触发 (Low 跌破) → trailing 先赢."""
    close, high, low, entries = _make_trailing_market(low_pct=-0.09)  # low=91, 跌破 -8% cost_stop
    args = _make_args(close, entries, high, low,
                      trailing_enabled=True, trailing_activation=0.03, trailing_drawdown=0.01,
                      cost_stop_enabled=True, cost_stop_threshold=-0.08,
                      ladder_enabled=False,
                      trailing_first=True)
    _, raw_trades = _simulate_core_v3(**args)
    real_trades = [t for t in raw_trades if t[8] != 0.0 or t[0] != 0.0]
    reasons = [t[8] for t in real_trades]
    assert 3.0 not in reasons, f"trailing_first 应让 trailing 先赢, 不应 reason=3, 实际 {reasons}"
    assert (8.0 in reasons) or (4.0 in reasons), f"应触发 trailing (8 或 4), 实际 {reasons}"


# ---------------------------------------------------------------------------
# Test 6: trailing_first + 同 bar (ladder_tp + trailing 双触发) → reason=5 (ladder_tp 仍优先)
# ---------------------------------------------------------------------------
def test_trailing_first_ladder_tp_still_wins():
    """trailing_first: ladder_tp 仍优先于 trailing (只越过 cost_stop)."""
    close, high, low, entries = _make_trailing_market(low_pct=-0.09)
    # 加 ladder: 6% 档, high=103 (+3%) 不触发 ladder (要 +6%). 改 high=106
    high.iloc[6, 0] = 106.0   # hi_pp=+6% 触发 ladder
    args = _make_args(close, entries, high, low,
                      trailing_enabled=True, trailing_activation=0.03, trailing_drawdown=0.01,
                      cost_stop_enabled=True, cost_stop_threshold=-0.08,
                      ladder_enabled=True,
                      ladder_profits=np.array([0.06]), ladder_ratios=np.array([1.0]), n_ladder=1,
                      trailing_first=True)
    _, raw_trades = _simulate_core_v3(**args)
    real_trades = [t for t in raw_trades if t[8] != 0.0 or t[0] != 0.0]
    reasons = [t[8] for t in real_trades]
    assert 5.0 in reasons, f"ladder_tp 应仍优先于 trailing, 应 reason=5, 实际 {reasons}"


# ---------------------------------------------------------------------------
# Test 7: trailing 触发后执行价 = peak_hi*(1-drawdown), 不是 Close
# ---------------------------------------------------------------------------
def test_trailing_execution_price_is_trail_line():
    """trailing 触发 → 执行价 = 103*0.99 = 101.97, 不是 Close=96."""
    close, high, low, entries = _make_trailing_market(low_pct=-0.06, close_pct=-0.04)  # close=96
    args = _make_args(close, entries, high, low,
                      trailing_enabled=True, trailing_activation=0.03, trailing_drawdown=0.01,
                      cost_stop_enabled=False,  # 关闭 cost_stop 避免干扰
                      ladder_enabled=False,
                      trailing_first=True)
    _, raw_trades = _simulate_core_v3(**args)
    real_trades = [t for t in raw_trades if t[8] != 0.0 or t[0] != 0.0]
    trail_trades = [t for t in real_trades if t[8] in (4.0, 8.0)]
    assert len(trail_trades) >= 1, f"应有 trailing 触发, 实际 reasons={[t[8] for t in real_trades]}"
    # 回撤线价 = 103 * (1-0.01) = 101.97
    expected = 103.0 * (1.0 - 0.01)
    assert abs(trail_trades[0][4] - expected) < 1e-9, \
        f"trailing 执行价应={expected} (回撤线价), 实际 {trail_trades[0][4]} (Close={96})"


# ---------------------------------------------------------------------------
# Test 8: Low 触发 — Low 触及回撤线, Close 远低于回撤线时仍按回撤线价执行
# ---------------------------------------------------------------------------
def test_trailing_low_trigger_even_if_close_far_below():
    """Low=94 触及回撤线 101.97, Close=96 远低于回撤线, 仍按 101.97 成交 (盘中锁利)."""
    close, high, low, entries = _make_trailing_market(low_pct=-0.06, close_pct=-0.04)
    args = _make_args(close, entries, high, low,
                      trailing_enabled=True, trailing_activation=0.03, trailing_drawdown=0.01,
                      cost_stop_enabled=False, ladder_enabled=False,
                      trailing_first=True)
    _, raw_trades = _simulate_core_v3(**args)
    real_trades = [t for t in raw_trades if t[8] != 0.0 or t[0] != 0.0]
    trail_trades = [t for t in real_trades if t[8] in (4.0, 8.0)]
    assert len(trail_trades) >= 1, "应触发 trailing"
    # 关键: 执行价 101.97 >> Close 96, 证明按回撤线价而非 Close
    assert trail_trades[0][4] > 96.0, \
        f"执行价 {trail_trades[0][4]} 应 > Close 96 (按回撤线价 101.97, 不按 Close)"


# ---------------------------------------------------------------------------
# Test 9: (废弃) 跳空低开保护 — 改为"始终按回撤线价"后, 跳空也按回撤线价
#   2026-07-05: 用户决策 trailing 不做跳空保护, 始终按回撤线价成交
# ---------------------------------------------------------------------------
def test_trailing_no_gap_protection_always_trail_line():
    """trailing 不做跳空保护: 即使 open=95 < 回撤线 101.97, 仍按 101.97 成交 (锁利乐观假设)."""
    close, high, low, entries = _make_trailing_market(low_pct=-0.06, close_pct=-0.04)
    # bar 6 open = 95 (低于回撤线 101.97)
    open_np = np.full((10, 1), 100.0)
    open_np[6, 0] = 95.0
    args = _make_args(close, entries, high, low,
                      trailing_enabled=True, trailing_activation=0.03, trailing_drawdown=0.01,
                      cost_stop_enabled=False, ladder_enabled=False,
                      trailing_first=True,
                      open_np=open_np)
    _, raw_trades = _simulate_core_v3(**args)
    real_trades = [t for t in raw_trades if t[8] != 0.0 or t[0] != 0.0]
    trail_trades = [t for t in real_trades if t[8] in (4.0, 8.0)]
    assert len(trail_trades) >= 1, "应触发 trailing"
    # 不保护: 仍按回撤线价 101.97, 不按 open=95
    expected = 103.0 * (1.0 - 0.01)
    assert abs(trail_trades[0][4] - expected) < 1e-9, \
        f"不保护: 应按回撤线价 {expected}, 实际 {trail_trades[0][4]} (不应按 open=95)"


# ---------------------------------------------------------------------------
# Test 12: T 日(信号日)冲高不算 trailing 激活 — pos_high_hi 从买入价(bp)开始
#   场景: bar 5(信号日) high=103 close=100 → 买入 bp=100 (T 日冲高 103 不记入)
#         bar 6(T2) high=101 low=99 close=100 → trailing 不应激活 (peak_hi=100, profit=0%<3%)
#   若 T 日 high 被错误记入 pos_high_hi, bar 6 会 peak_hi=103 → 激活 → 误触发
# ---------------------------------------------------------------------------
def test_t_day_high_not_counted_for_trailing_activation():
    """T 日(信号日)的 high 不算 trailing 激活, pos_high_hi 从买入价 bp 开始."""
    n_dates, n_stocks = 10, 1
    dates = pd.bdate_range("2024-01-02", periods=n_dates)
    columns = pd.Index(["600519.SH"])
    close = pd.DataFrame(np.full((n_dates, n_stocks), 100.0), index=dates, columns=columns)
    high = pd.DataFrame(np.full((n_dates, n_stocks), 101.0), index=dates, columns=columns)
    low = pd.DataFrame(np.full((n_dates, n_stocks), 99.0), index=dates, columns=columns)

    # bar 5 (T 日, 信号日): 冲高 103, 收盘 100 → 买入价 bp=100
    high.iloc[5, 0] = 103.0
    close.iloc[5, 0] = 100.0
    # bar 6 (T2): 不冲高, 不触发 trailing
    high.iloc[6, 0] = 101.0   # peak_hi 仍=100 (bp), profit=0% < 3% → 不激活
    low.iloc[6, 0] = 99.0
    close.iloc[6, 0] = 100.0

    entries = pd.DataFrame(False, index=dates, columns=columns)
    entries.iloc[5, 0] = True

    args = _make_args(close, entries, high, low,
                      trailing_enabled=True, trailing_activation=0.03, trailing_drawdown=0.01,
                      cost_stop_enabled=False, ladder_enabled=False,
                      trailing_first=True)
    _, raw_trades = _simulate_core_v3(**args)
    real_trades = [t for t in raw_trades if t[8] != 0.0 or t[0] != 0.0]
    # bar 6 不应触发 trailing (T 日 high=103 不算)
    trail_trades = [t for t in real_trades if t[8] in (4.0, 8.0)]
    assert len(trail_trades) == 0, \
        f"T 日冲高 103 不应让 T2 触发 trailing (pos_high_hi 应从 bp=100 开始), 实际触发了 {len(trail_trades)} 笔"


# ---------------------------------------------------------------------------
# Test 9b: 平开场景 (open=ep=100) — 盘中冲高 103 回撤触发, 按 101.97 成交
#   这是用户的原始场景, 之前 bug 会因 open<回撤线 误按 100 成交
# ---------------------------------------------------------------------------
def test_trailing_flat_open_no_false_gap_protection():
    """平开 open=100, 盘中冲高 103 回撤触发 → 按 101.97 (不因 open<101.97 误保护)."""
    close, high, low, entries = _make_trailing_market(low_pct=-0.06, close_pct=-0.04)
    open_np = np.full((10, 1), 100.0)   # 平开 open=100
    args = _make_args(close, entries, high, low,
                      trailing_enabled=True, trailing_activation=0.03, trailing_drawdown=0.01,
                      cost_stop_enabled=False, ladder_enabled=False,
                      trailing_first=True,
                      open_np=open_np)
    _, raw_trades = _simulate_core_v3(**args)
    real_trades = [t for t in raw_trades if t[8] != 0.0 or t[0] != 0.0]
    trail_trades = [t for t in real_trades if t[8] in (4.0, 8.0)]
    assert len(trail_trades) >= 1, "应触发 trailing"
    expected = 103.0 * (1.0 - 0.01)  # 101.97
    assert abs(trail_trades[0][4] - expected) < 1e-9, \
        f"平开场景应按回撤线价 {expected}, 实际 {trail_trades[0][4]} (旧 bug 会按 open=100)"


# ---------------------------------------------------------------------------
# Test 11: reason 按回撤线价判断 (不是 Close) — 盈利的 trailing 应 reason=8
#   场景: ep=100, high=103, low=94, close=98
#   回撤线价=101.97 → pp_trail=+1.97% > 0 → reason=8 (移动止盈)
#   旧逻辑用 Close=98 → pp=-2% ≤ 0 → reason=4 (移动止损) — 错误
# ---------------------------------------------------------------------------
def test_trailing_reason_uses_trail_line_price():
    """trailing 触发后 reason 按回撤线价判断: 101.97 盈利 → reason=8 (移动止盈)."""
    close, high, low, entries = _make_trailing_market(low_pct=-0.06, close_pct=-0.04)  # close=96
    # 调整 close 让 Close 算 pp 为负, 但回撤线价算 pp 为正
    # ep=100, trail_line=101.97, pp_trail=+1.97% > 0 → reason=8
    # close=96, pp_close=-4% ≤ 0 → 旧逻辑 reason=4 (错)
    args = _make_args(close, entries, high, low,
                      trailing_enabled=True, trailing_activation=0.03, trailing_drawdown=0.01,
                      cost_stop_enabled=False, ladder_enabled=False,
                      trailing_first=True)
    _, raw_trades = _simulate_core_v3(**args)
    real_trades = [t for t in raw_trades if t[8] != 0.0 or t[0] != 0.0]
    trail_trades = [t for t in real_trades if t[8] in (4.0, 8.0)]
    assert len(trail_trades) >= 1, "应触发 trailing"
    assert trail_trades[0][8] == 8.0, \
        f"回撤线价 101.97 盈利 (+1.97%), 应 reason=8 (移动止盈), 实际 reason={trail_trades[0][8]} (旧逻辑用 Close 会判成 4)"


# ---------------------------------------------------------------------------
# Test 11b: 回撤线价亏损时 reason=4 (移动止损)
#   场景: ep=100, high=101 (激活 1%? 不够 3%). 改 activation=0.005, high=101
#   trail_line=101*0.99=99.99, pp_trail=-0.01% ≤ 0 → reason=4
# ---------------------------------------------------------------------------
def test_trailing_reason_loss_when_trail_line_below_ep():
    """回撤线价 < ep 时 reason=4 (移动止损)."""
    close, high, low, entries = _make_trailing_market(activation=0.005, drawdown=0.01, low_pct=-0.06, close_pct=-0.04)
    # peak_hi=103 (默认 high=103), trail_line=103*0.99=101.97 > ep=100 → pp_trail>0 → reason=8
    # 要让 trail_line < ep, 需 high 接近 ep. 改 high=100.5 (activation=0.005 即 0.5%)
    high.iloc[6, 0] = 100.5  # peak_hi_profit=0.5% ≥ 0.5% 激活
    # trail_line = 100.5 * 0.99 = 99.495 < 100 → pp_trail < 0 → reason=4
    args = _make_args(close, entries, high, low,
                      trailing_enabled=True, trailing_activation=0.005, trailing_drawdown=0.01,
                      cost_stop_enabled=False, ladder_enabled=False,
                      trailing_first=True)
    _, raw_trades = _simulate_core_v3(**args)
    real_trades = [t for t in raw_trades if t[8] != 0.0 or t[0] != 0.0]
    trail_trades = [t for t in real_trades if t[8] in (4.0, 8.0)]
    assert len(trail_trades) >= 1, "应触发 trailing"
    assert trail_trades[0][8] == 4.0, \
        f"回撤线价 99.495 亏损 (-0.5%), 应 reason=4 (移动止损), 实际 reason={trail_trades[0][8]}"
def test_trailing_new_semantics_all_modes():
    """trailing 语义改造是全局的: stop_first/ladder_tp_first/trailing_first 都用 Low 触发 + 回撤线价."""
    close, high, low, entries = _make_trailing_market(low_pct=-0.06, close_pct=-0.04)
    expected = 103.0 * (1.0 - 0.01)  # 回撤线价 101.97
    for trailing_first in (False, True):
        for ladder_tp_first in (False, True):
            if trailing_first and ladder_tp_first:
                continue  # 互斥, trailing_first 优先
            args = _make_args(close, entries, high, low,
                              trailing_enabled=True, trailing_activation=0.03, trailing_drawdown=0.01,
                              cost_stop_enabled=False, ladder_enabled=False,
                              trailing_first=trailing_first,
                              ladder_tp_first=ladder_tp_first)
            _, raw_trades = _simulate_core_v3(**args)
            real_trades = [t for t in raw_trades if t[8] != 0.0 or t[0] != 0.0]
            trail_trades = [t for t in real_trades if t[8] in (4.0, 8.0)]
            assert len(trail_trades) >= 1, \
                f"trailing_first={trailing_first} ladder_tp_first={ladder_tp_first}: 应触发 trailing"
            assert abs(trail_trades[0][4] - expected) < 1e-9, \
                f"trailing_first={trailing_first} ladder_tp_first={ladder_tp_first}: 执行价应={expected}, 实际 {trail_trades[0][4]}"