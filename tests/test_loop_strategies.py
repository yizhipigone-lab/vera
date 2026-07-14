"""候选 A 阶段 2 — stage 1 策略 adapter 单测。

用 5-bar 假数据验证每个 ExitStrategy 的触发条件 + 执行价计算,
对照 engine.py 原始逻辑（行号见各策略文件 docstring）。
"""

from __future__ import annotations

import numpy as np
import pytest

from backtest.loop import (
    BacktestParams, Context, Position, PositionBook, TradeBuffer, Bar,
    TradeColumns, assert_state_dtype,
    CostStopStrategy, LadderTpStrategy, TrailingStrategy,
    TimeStopStrategy, CondTimeStrategy, FirstDayStrategy,
    AtrStopStrategy,
)


# ─────────────────────────────────────────────────────────────
# 工厂
# ─────────────────────────────────────────────────────────────
def make_pos(code=3, shares=1000.0, entry_px=10.0, entry_idx=0,
             high_px=10.0, high_hi=10.0, ladder_done=0) -> Position:
    return Position(code=code, shares=shares, entry_px=entry_px,
                    entry_idx=entry_idx, high_px=high_px, high_hi=high_hi,
                    ladder_done=ladder_done)


def make_bar(close=10.0, high=10.0, low=10.0, open_=10.0) -> Bar:
    return Bar(close=close, high=high, low=low, open=open_)


def make_ctx(**kw) -> Context:
    """构造 Context, 未传字段用安全默认。"""
    base = dict(
        bar_index=5, ci=0, bpday=1, hold_days=3,
        entry_px=10.0, pp=0.0, hp_profit=0.0,
        peak_hi=10.0, peak_hi_profit=0.0,
        pos_high_px=10.0, pos_high_hi=10.0,
        hi_pp=0.0, lo_pp=0.0,
        ladder_profits=np.array([0.06, 0.15], dtype=np.float64),
        ladder_ratios=np.array([0.5, 0.5], dtype=np.float64),
        n_ladder=2,
    )
    base.update(kw)
    return Context(**base)


# ─────────────────────────────────────────────────────────────
# dtype 断言（R3/M1 验收门槛）
# ─────────────────────────────────────────────────────────────
class TestStateDtype:
    def test_assert_state_dtype_passes(self):
        assert_state_dtype()  # 不抛即过

    def test_position_int_fields(self):
        pos = make_pos()
        assert isinstance(pos.code, (int, np.integer))
        assert isinstance(pos.entry_idx, (int, np.integer))
        assert isinstance(pos.ladder_done, (int, np.integer))

    def test_position_float_fields(self):
        pos = make_pos()
        assert isinstance(pos.shares, (float, np.floating))
        assert isinstance(pos.entry_px, (float, np.floating))
        assert isinstance(pos.high_px, (float, np.floating))
        assert isinstance(pos.high_hi, (float, np.floating))

    def test_bar_all_float(self):
        b = make_bar()
        for f in ("close", "high", "low", "open"):
            assert isinstance(getattr(b, f), (float, np.floating))

    def test_tradebuffer_dtype_float64(self):
        buf = TradeBuffer(n_dates=10, n_stocks=5)
        assert buf.dtype == np.float64
        assert buf.capacity == 10 * 5 // 4 + 1000

    def test_positionbook_dtype(self):
        book = PositionBook()
        assert book.dtype_code == np.int32
        assert book.dtype_shares == np.float64


# ─────────────────────────────────────────────────────────────
# TradeBuffer + TradeColumns
# ─────────────────────────────────────────────────────────────
class TestTradeBuffer:
    def test_append_and_to_array(self):
        buf = TradeBuffer(n_dates=10, n_stocks=5)
        buf.append(code=3, entry_idx=0, exit_idx=5, entry_px=10.0,
                   sell_px=11.0, shares=1000, profit=1000.0, ret=0.1, reason=5)
        arr = buf.to_array()
        assert arr.shape == (1, TradeColumns.NCOLS)
        assert arr[0, TradeColumns.CODE] == 3.0
        assert arr[0, TradeColumns.REASON] == 5.0
        assert arr[0, TradeColumns.SELL_PX] == 11.0
        assert buf.count == 1

    def test_grow(self):
        buf = TradeBuffer(n_dates=2, n_stocks=1)  # capacity = 2//4+1000 = 1000
        # 写 1001 行触发扩容
        for i in range(1001):
            buf.append(i, 0, 1, 10.0, 11.0, 100, 100.0, 0.1, 5)
        assert buf.count == 1001
        assert buf.capacity >= 1001
        arr = buf.to_array()
        assert arr[1000, TradeColumns.CODE] == 1000.0


class TestTradeColumns:
    def test_column_indices(self):
        assert TradeColumns.NCOLS == 9
        assert TradeColumns.CODE == 0
        assert TradeColumns.ENTRY_IDX == 1
        assert TradeColumns.EXIT_IDX == 2
        assert TradeColumns.ENTRY_PX == 3
        assert TradeColumns.SELL_PX == 4
        assert TradeColumns.SHARES == 5
        assert TradeColumns.PROFIT == 6
        assert TradeColumns.RETURN == 7
        assert TradeColumns.REASON == 8


# ─────────────────────────────────────────────────────────────
# CostStopStrategy (reason=3)
# ─────────────────────────────────────────────────────────────
class TestCostStop:
    def test_trigger_when_low_below_threshold(self):
        s = CostStopStrategy(threshold=-0.05)
        pos = make_pos(entry_px=10.0)
        bar = make_bar(close=9.5, high=9.6, low=9.4, open_=9.8)
        ctx = make_ctx(lo_pp=-0.06)  # low=9.4 → -6% <= -5%
        res = s.check(pos, bar, ctx)
        assert len(res) == 1
        assert res[0].reason == 3
        # stop_price = 10*(1-0.05)=9.5; open=9.8 > 9.5 → 不跳空 → 9.5
        assert res[0].execution_price == pytest.approx(9.5)

    def test_gap_down_uses_open(self):
        s = CostStopStrategy(threshold=-0.05)
        pos = make_pos(entry_px=10.0)
        bar = make_bar(close=9.0, high=9.1, low=8.9, open_=9.2)  # open < stop_price(9.5)
        ctx = make_ctx(lo_pp=-0.11)
        res = s.check(pos, bar, ctx)
        assert res[0].execution_price == pytest.approx(9.2)  # min(9.5, 9.2)

    def test_no_trigger_when_above_threshold(self):
        s = CostStopStrategy(threshold=-0.05)
        pos = make_pos(entry_px=10.0)
        ctx = make_ctx(lo_pp=-0.02)  # -2% > -5%
        assert s.check(pos, make_bar(), ctx) == []


# ─────────────────────────────────────────────────────────────
# LadderTpStrategy (reason=5)
# ─────────────────────────────────────────────────────────────
class TestLadderTp:
    def test_trigger_first_rung_partial_sell(self):
        s = LadderTpStrategy()
        pos = make_pos(entry_px=10.0, ladder_done=0)
        # hi_pp=0.08 >= 0.06(第一档) 但 < 0.15(第二档) → 只置位第一档
        ctx = make_ctx(hi_pp=0.08)
        res = s.check(pos, make_bar(), ctx)
        assert len(res) == 1
        assert res[0].reason == 5
        assert res[0].is_partial is True
        assert res[0].sell_ratio == pytest.approx(0.5)  # 第一档 ratio=0.5
        # exec_price = 10*(1+0.06) = 10.6
        assert res[0].execution_price == pytest.approx(10.6)
        assert pos.ladder_done == 1  # bitmask 第一位置位

    def test_trigger_both_rungs_full_sell(self):
        s = LadderTpStrategy()
        pos = make_pos(entry_px=10.0, ladder_done=0)
        # hi_pp=0.20 >= 0.06 且 >= 0.15 → 两档都置位, ratio=1.0
        ctx = make_ctx(hi_pp=0.20)
        res = s.check(pos, make_bar(), ctx)
        assert res[0].is_partial is False
        assert res[0].sell_ratio == pytest.approx(1.0)
        # exec_price = 10*(1+0.15) = 11.5（取最大档位）
        assert res[0].execution_price == pytest.approx(11.5)
        assert pos.ladder_done == 3  # 两档都置位

    def test_no_trigger_when_already_done(self):
        s = LadderTpStrategy()
        pos = make_pos(entry_px=10.0, ladder_done=3)  # 两档都已触发
        ctx = make_ctx(hi_pp=0.20)
        assert s.check(pos, make_bar(), ctx) == []
        assert pos.ladder_done == 3  # 不变

    def test_no_trigger_below_first_rung(self):
        s = LadderTpStrategy()
        pos = make_pos(entry_px=10.0, ladder_done=0)
        ctx = make_ctx(hi_pp=0.03)  # < 0.06
        assert s.check(pos, make_bar(), ctx) == []


# ─────────────────────────────────────────────────────────────
# TrailingStrategy (reason=4/8)
# ─────────────────────────────────────────────────────────────
class TestTrailing:
    def test_trigger_profit_when_low_hits_trail_line(self):
        s = TrailingStrategy(activation=0.05, drawdown=0.10)
        pos = make_pos(entry_px=10.0)
        # peak_hi=11.5 → peak_hi_profit=0.15 >= 0.05 激活
        # trail_line = 11.5*0.9 = 10.35; low=10.3 <= 10.35 触发
        # (10.35-10)/10 = 0.035 > 0 → reason=8（止盈）
        ctx = make_ctx(peak_hi=11.5, peak_hi_profit=0.15)
        bar = make_bar(low=10.3)
        res = s.check(pos, bar, ctx)
        assert len(res) == 1
        assert res[0].reason == 8
        assert res[0].execution_price == pytest.approx(10.35)

    def test_trigger_loss_when_trail_line_below_entry(self):
        s = TrailingStrategy(activation=0.05, drawdown=0.20)
        pos = make_pos(entry_px=10.0)
        # peak_hi=10.5 → profit=0.05 激活; trail_line=10.5*0.8=8.4
        # (8.4-10)/10 = -0.16 < 0 → reason=4（止损）
        ctx = make_ctx(peak_hi=10.5, peak_hi_profit=0.05)
        bar = make_bar(low=8.0)
        res = s.check(pos, bar, ctx)
        assert res[0].reason == 4
        assert res[0].execution_price == pytest.approx(8.4)

    def test_no_trigger_below_activation(self):
        s = TrailingStrategy(activation=0.05, drawdown=0.10)
        ctx = make_ctx(peak_hi=10.2, peak_hi_profit=0.02)  # < 0.05
        assert s.check(make_pos(), make_bar(low=9.0), ctx) == []

    def test_no_trigger_when_low_above_trail_line(self):
        s = TrailingStrategy(activation=0.05, drawdown=0.10)
        ctx = make_ctx(peak_hi=11.5, peak_hi_profit=0.15)
        # trail_line=10.35; low=10.4 > 10.35
        assert s.check(make_pos(), make_bar(low=10.4), ctx) == []


# ─────────────────────────────────────────────────────────────
# TimeStopStrategy (reason=6/9)
# ─────────────────────────────────────────────────────────────
class TestTimeStop:
    def test_trigger_profit_when_pp_positive(self):
        s = TimeStopStrategy(max_hold_days=5)
        ctx = make_ctx(hold_days=5, pp=0.03)
        bar = make_bar(close=10.3)
        res = s.check(make_pos(), bar, ctx)
        assert res[0].reason == 9  # 止盈
        assert res[0].execution_price == pytest.approx(10.3)

    def test_trigger_loss_when_pp_negative(self):
        s = TimeStopStrategy(max_hold_days=5)
        ctx = make_ctx(hold_days=5, pp=-0.02)
        res = s.check(make_pos(), make_bar(close=9.8), ctx)
        assert res[0].reason == 6  # 止损

    def test_no_trigger_before_max_hold(self):
        s = TimeStopStrategy(max_hold_days=5)
        ctx = make_ctx(hold_days=4, pp=0.03)
        assert s.check(make_pos(), make_bar(), ctx) == []


# ─────────────────────────────────────────────────────────────
# CondTimeStrategy (reason=7)
# ─────────────────────────────────────────────────────────────
class TestCondTime:
    def test_trigger_when_days_and_profit_met(self):
        s = CondTimeStrategy(days=3, profit=0.08)
        ctx = make_ctx(hold_days=3, hi_pp=0.10)
        res = s.check(make_pos(), make_bar(close=11.0), ctx)
        assert res[0].reason == 7
        assert res[0].execution_price == pytest.approx(11.0)

    def test_no_trigger_when_profit_below(self):
        s = CondTimeStrategy(days=3, profit=0.08)
        ctx = make_ctx(hold_days=3, hi_pp=0.05)
        assert s.check(make_pos(), make_bar(), ctx) == []

    def test_no_trigger_when_days_below(self):
        s = CondTimeStrategy(days=3, profit=0.08)
        ctx = make_ctx(hold_days=2, hi_pp=0.10)
        assert s.check(make_pos(), make_bar(), ctx) == []


# ─────────────────────────────────────────────────────────────
# FirstDayStrategy (reason=10)
# ─────────────────────────────────────────────────────────────
class TestFirstDay:
    def test_trigger_when_day1_return_below_target(self):
        # bpday=1: entry_idx=0 → entry_day=0; bar_index=1 → current_day=1 = entry_day+1 ✓
        # i%bpday == bpday-1 == 0 ✓ (最后一根 bar)
        s = FirstDayStrategy(target=0.03)
        pos = make_pos(entry_px=10.0, entry_idx=0, high_hi=10.2)  # day_high=10.2 → 2% < 3%
        ctx = make_ctx(bar_index=1, bpday=1)
        res = s.check(pos, make_bar(close=10.2), ctx)
        assert res[0].reason == 10
        assert res[0].execution_price == pytest.approx(10.2)

    def test_no_trigger_when_day1_return_meets_target(self):
        s = FirstDayStrategy(target=0.03)
        pos = make_pos(entry_px=10.0, entry_idx=0, high_hi=10.5)  # 5% >= 3%
        ctx = make_ctx(bar_index=1, bpday=1)
        assert s.check(pos, make_bar(), ctx) == []

    def test_no_trigger_when_not_first_day(self):
        s = FirstDayStrategy(target=0.03)
        pos = make_pos(entry_px=10.0, entry_idx=0, high_hi=10.2)
        ctx = make_ctx(bar_index=2, bpday=1)  # current_day=2 != 1
        assert s.check(pos, make_bar(), ctx) == []


# ─────────────────────────────────────────────────────────────
# PositionBook swap-and-pop（CR2）
# ─────────────────────────────────────────────────────────────
class TestPositionBook:
    def test_add_get_set(self):
        book = PositionBook()
        p0 = book.add(code=3, shares=100, entry_px=10.0, entry_idx=0,
                       high_px=10.0, high_hi=10.0)
        assert book.count == 1
        pos = book.get(p0)
        assert pos.code == 3 and pos.shares == 100

    def test_swap_and_pop(self):
        book = PositionBook()
        book.add(code=1, shares=100, entry_px=10.0, entry_idx=0, high_px=10.0, high_hi=10.0)
        book.add(code=2, shares=200, entry_px=20.0, entry_idx=1, high_px=20.0, high_hi=20.0)
        book.add(code=3, shares=300, entry_px=30.0, entry_idx=2, high_px=30.0, high_hi=30.0)
        # 删除 p=1（code=2）, swap-and-pop 把 last(code=3) 挪到 p=1
        book.remove_swap_pop(1)
        assert book.count == 2
        assert book.get(1).code == 3  # last 挪过来
        assert book.get(0).code == 1

    def test_update_high(self):
        book = PositionBook()
        p0 = book.add(code=3, shares=100, entry_px=10.0, entry_idx=0, high_px=10.0, high_hi=10.0)
        book.update_high(p0, high_px=11.0, high_hi=12.0)
        pos = book.get(p0)
        assert pos.high_px == 11.0
        assert pos.high_hi == 12.0


# ─────────────────────────────────────────────────────────────
# AtrStopStrategy (reason=13) — 加新策略范例
# ─────────────────────────────────────────────────────────────
class TestAtrStop:
    def test_trigger_when_low_below_atr_line(self):
        # peak_hi=11.0, ATR=0.5, multiplier=3 → trail_line = 11 - 1.5 = 9.5
        atr_matrix = np.full((10, 2), 0.5, dtype=np.float64)
        s = AtrStopStrategy(atr_matrix=atr_matrix, multiplier=3.0)
        pos = make_pos(entry_px=10.0)
        ctx = make_ctx(bar_index=5, ci=0, peak_hi=11.0)
        bar = make_bar(low=9.4)  # <= 9.5 触发
        res = s.check(pos, bar, ctx)
        assert len(res) == 1
        assert res[0].reason == 13
        assert res[0].execution_price == pytest.approx(9.5)
        assert res[0].sell_ratio == 1.0  # 全卖

    def test_no_trigger_when_low_above_atr_line(self):
        atr_matrix = np.full((10, 2), 0.5, dtype=np.float64)
        s = AtrStopStrategy(atr_matrix=atr_matrix, multiplier=3.0)
        ctx = make_ctx(bar_index=5, ci=0, peak_hi=11.0)  # trail_line=9.5
        assert s.check(make_pos(), make_bar(low=9.6), ctx) == []

    def test_no_trigger_when_atr_zero(self):
        atr_matrix = np.zeros((10, 2), dtype=np.float64)
        s = AtrStopStrategy(atr_matrix=atr_matrix, multiplier=3.0)
        ctx = make_ctx(bar_index=5, ci=0, peak_hi=11.0)
        assert s.check(make_pos(), make_bar(low=5.0), ctx) == []

    def test_none_matrix_no_fire(self):
        s = AtrStopStrategy(atr_matrix=None, multiplier=3.0)
        assert s.check(make_pos(), make_bar(low=5.0), make_ctx(peak_hi=11.0)) == []

    def test_out_of_bounds_no_fire(self):
        atr_matrix = np.full((5, 2), 0.5, dtype=np.float64)
        s = AtrStopStrategy(atr_matrix=atr_matrix, multiplier=3.0)
        ctx = make_ctx(bar_index=99, ci=0, peak_hi=11.0)  # 越界
        assert s.check(make_pos(), make_bar(low=5.0), ctx) == []


# ─────────────────────────────────────────────────────────────
# ATR 端到端集成（build_backtest_loop + BacktestLoop.run, reason=13）
# ─────────────────────────────────────────────────────────────
class TestAtrIntegration:
    def test_atr_fires_through_full_loop(self):
        """建带 ATR 的 loop, 跑 crafted 数据, 验 reason=13 交易产生。"""
        from backtest.loop import build_backtest_loop
        # bar0 入场@10; bar1 T+1; bar2 冲高 peak=11; bar3 跌 low=9.4
        n = 6
        price = np.full((n, 1), 10.0)
        high = np.full((n, 1), 10.0)
        low = np.full((n, 1), 10.0)
        op = np.full((n, 1), 10.0)
        price[2, 0] = 11.0; high[2, 0] = 11.0; low[2, 0] = 10.8
        price[3, 0] = 9.5;  high[3, 0] = 9.8;  low[3, 0] = 9.4
        entry = np.zeros((n, 1), dtype=bool)
        entry[0, 0] = True
        atr_matrix = np.full((n, 1), 0.5, dtype=np.float64)  # ATR=0.5
        # trail_line @ bar3 = peak_hi(11) - 3*0.5 = 9.5; low=9.4 <= 9.5 → ATR 触发
        loop = build_backtest_loop(
            1_000_000.0, 0.0003, 1000.0, 200_000.0, 100, 1,
            True, -0.20,        # cost_stop 阈值 -20% (不触发, 让 ATR 先)
            False, 0.05, 0.10,  # trailing 禁用
            False, np.array([0.06, 0.15], dtype=np.float64),
            np.array([0.5, 0.5], dtype=np.float64), 2,  # ladder 禁用
            False, 10,          # time 禁用
            False, 3, 0.08,     # cond_time 禁用
            False, 0.03,        # first_day 禁用
            1, 0.0, 0.001, 1.0, False, False, None, 1.0, 1,
            atr_enabled=True, atr_matrix=atr_matrix, atr_multiplier=3.0,
        )
        eq, trades = loop.run(price, entry, high, low, op, None, None, None)
        assert trades.shape[0] >= 1, f"应产生交易, got {trades.shape}"
        # 至少一笔 reason=13
        reasons = trades[:, 8]
        assert 13.0 in reasons, f"应含 ATR(reason=13) 交易, reasons={reasons}"
        atr_trade = trades[reasons == 13.0][0]
        assert atr_trade[4] == pytest.approx(9.5)  # exec_price = trail_line
