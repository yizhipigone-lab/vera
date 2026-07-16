"""BacktestParams / Bar / Context / Position / TradeBuffer / PositionBook 单元测试 (迭代 3, 2026-07-15).

锁住所有 data class 的字段约束 + dtype 守卫 + 数组操作边界.
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.loop.state import (
    BacktestParams, Bar, Context, Position,
    TradeBuffer, PositionBook, TradeColumns,
    assert_state_dtype,
)


# ═══════════════════════════════════════════════════════════════
# BacktestParams 约束
# ═══════════════════════════════════════════════════════════════


def test_backtest_params_basic_construction():
    """正常构造应通过."""
    p = BacktestParams(
        initial_capital=1_000_000.0,
        commission=0.0003, slippage=0.001, stamp_tax=0.0005,
        min_buy_amount=2000.0, max_buy_amount=20000.0,
        lot_size=100, min_lots=1,
    )
    assert p.initial_capital == 1_000_000.0
    assert p.bpday == 1
    assert p.max_position_pct == 1.0


def test_backtest_params_rejects_zero_bpday():
    """bpday=0 必须抛 ValueError (防止除零)."""
    with pytest.raises(ValueError, match="bpday"):
        BacktestParams(
            initial_capital=1_000_000.0, commission=0.0003, slippage=0.001,
            stamp_tax=0.0005, min_buy_amount=2000.0, max_buy_amount=20000.0,
            lot_size=100, min_lots=1, bpday=0,
        )


def test_backtest_params_rejects_negative_bpday():
    """bpday=-1 必须抛."""
    with pytest.raises(ValueError):
        BacktestParams(
            initial_capital=1_000_000.0, commission=0.0003, slippage=0.001,
            stamp_tax=0.0005, min_buy_amount=2000.0, max_buy_amount=20000.0,
            lot_size=100, min_lots=1, bpday=-1,
        )


def test_backtest_params_rejects_zero_lot_size():
    """lot_size=0 必须抛."""
    with pytest.raises(ValueError, match="lot_size"):
        BacktestParams(
            initial_capital=1_000_000.0, commission=0.0003, slippage=0.001,
            stamp_tax=0.0005, min_buy_amount=2000.0, max_buy_amount=20000.0,
            lot_size=0, min_lots=1,
        )


def test_backtest_params_is_frozen():
    """BacktestParams 必须 frozen."""
    p = BacktestParams(
        initial_capital=1_000_000.0, commission=0.0003, slippage=0.001,
        stamp_tax=0.0005, min_buy_amount=2000.0, max_buy_amount=20000.0,
        lot_size=100, min_lots=1,
    )
    with pytest.raises(Exception):
        p.commission = 0.01


# ═══════════════════════════════════════════════════════════════
# Bar / Context / Position 字段约束
# ═══════════════════════════════════════════════════════════════


def test_bar_construction():
    """Bar 4 字段 (close/high/low/open) 必须 float."""
    b = Bar(close=10.0, high=11.0, low=9.5, open=10.2)
    assert b.close == 10.0
    assert b.high == 11.0
    assert b.low == 9.5
    assert b.open == 10.2


def test_bar_is_frozen():
    b = Bar(close=10.0, high=11.0, low=9.5, open=10.2)
    with pytest.raises(Exception):
        b.close = 11.0


def test_position_mutable_for_ladder_state():
    """Position 是 mutable (ladder_done 跨 bar 累计)."""
    p = Position(
        code=np.int32(0), shares=np.float64(100.0),
        entry_px=np.float64(10.0), entry_idx=np.int32(5),
        high_px=np.float64(11.0), high_hi=np.float64(11.5),
        ladder_done=np.int32(0),
    )
    p.ladder_done = 0b11  # 应允许
    assert p.ladder_done == 3


def test_context_ladder_views_are_arrays():
    """Context 的 ladder_profits/ratios 必须是 ndarray."""
    ctx = Context(
        bar_index=10, ci=0, bpday=1, hold_days=5,
        entry_px=10.0, pp=0.05, hp_profit=0.06,
        peak_hi=11.0, peak_hi_profit=0.10,
        pos_high_px=10.5, pos_high_hi=11.0,
        hi_pp=0.06, lo_pp=-0.03,
        ladder_profits=np.array([0.06, 0.15]),
        ladder_ratios=np.array([0.30, 0.30]),
        n_ladder=2,
    )
    assert isinstance(ctx.ladder_profits, np.ndarray)
    assert ctx.n_ladder == 2


def test_context_is_frozen():
    ctx = Context(
        bar_index=10, ci=0, bpday=1, hold_days=5,
        entry_px=10.0, pp=0.05, hp_profit=0.06,
        peak_hi=11.0, peak_hi_profit=0.10,
        pos_high_px=10.5, pos_high_hi=11.0,
        hi_pp=0.06, lo_pp=-0.03,
        ladder_profits=np.array([0.06, 0.15]),
        ladder_ratios=np.array([0.30, 0.30]),
        n_ladder=2,
    )
    with pytest.raises(Exception):
        ctx.hold_days = 100


# ═══════════════════════════════════════════════════════════════
# TradeBuffer 数组操作
# ═══════════════════════════════════════════════════════════════


def test_trade_buffer_initial_state():
    buf = TradeBuffer(n_dates=100, n_stocks=10)
    assert buf.count == 0
    assert buf.capacity > 0
    assert buf.dtype == np.float64


def test_trade_buffer_append_and_to_array():
    buf = TradeBuffer(n_dates=10, n_stocks=5)
    buf.append(code=0, entry_idx=1, exit_idx=5, entry_px=10.0,
               sell_px=11.0, shares=100.0, profit=100.0, ret=0.10, reason=5)
    assert buf.count == 1
    arr = buf.to_array()
    assert arr.shape == (1, TradeColumns.NCOLS)
    assert arr[0, TradeColumns.CODE] == 0
    assert arr[0, TradeColumns.ENTRY_PX] == 10.0
    assert arr[0, TradeColumns.SELL_PX] == 11.0
    assert arr[0, TradeColumns.REASON] == 5.0


def test_trade_buffer_grows_on_overflow():
    """超出初始 capacity 必须自动扩容."""
    buf = TradeBuffer(n_dates=2, n_stocks=2)  # max ≈ 2*2/4+1000 = 1001
    initial_cap = buf.capacity
    # 写满 + 1 行
    for i in range(initial_cap + 1):
        buf.append(code=i, entry_idx=i, exit_idx=i+1, entry_px=10.0,
                   sell_px=11.0, shares=1.0, profit=1.0, ret=0.10, reason=5)
    assert buf.count == initial_cap + 1
    assert buf.capacity >= 2 * initial_cap  # 至少翻倍


def test_trade_buffer_to_array_returns_copy():
    """to_array 必须返副本, 修改副本不污染内部."""
    buf = TradeBuffer(n_dates=10, n_stocks=5)
    buf.append(code=0, entry_idx=1, exit_idx=5, entry_px=10.0,
               sell_px=11.0, shares=100.0, profit=100.0, ret=0.10, reason=5)
    arr = buf.to_array()
    arr[0, 0] = 999.0
    assert buf.to_array()[0, 0] == 0.0  # 内部未变


# ═══════════════════════════════════════════════════════════════
# PositionBook swap-and-pop
# ═══════════════════════════════════════════════════════════════


def test_position_book_add_and_count():
    book = PositionBook(max_pos=10)
    book.add(code=0, shares=100.0, entry_px=10.0, entry_idx=5,
             high_px=10.5, high_hi=11.0)
    assert book.count == 1


def test_position_book_get_set_roundtrip():
    book = PositionBook(max_pos=10)
    p = book.add(code=2, shares=200.0, entry_px=20.0, entry_idx=3,
                 high_px=21.0, high_hi=22.0)
    pos = book.get(p)
    assert pos.code == 2
    assert pos.shares == 200.0
    assert pos.entry_px == 20.0
    assert pos.entry_idx == 3
    assert pos.ladder_done == 0


def test_position_book_remove_swap_pop_last():
    """删除最后一个槽位 (无需 swap)."""
    book = PositionBook(max_pos=10)
    p = book.add(code=0, shares=100.0, entry_px=10.0, entry_idx=5,
                 high_px=10.5, high_hi=11.0)
    book.remove_swap_pop(p)
    assert book.count == 0


def test_position_book_remove_swap_pop_middle():
    """删除中间槽位 — 最后槽位挪到 p."""
    book = PositionBook(max_pos=10)
    p0 = book.add(code=0, shares=100.0, entry_px=10.0, entry_idx=5,
                  high_px=10.5, high_hi=11.0)
    p1 = book.add(code=1, shares=200.0, entry_px=20.0, entry_idx=6,
                  high_px=21.0, high_hi=22.0)
    p2 = book.add(code=2, shares=300.0, entry_px=30.0, entry_idx=7,
                  high_px=31.0, high_hi=32.0)
    book.remove_swap_pop(p1)  # 删中间
    assert book.count == 2
    # p2 应被 swap 到 p1 位置
    assert book.get(p1).code == 2
    assert book.get(p1).shares == 300.0


def test_position_book_overflow_raises():
    """超过 max_pos 必须抛."""
    book = PositionBook(max_pos=2)
    book.add(code=0, shares=100.0, entry_px=10.0, entry_idx=5,
             high_px=10.5, high_hi=11.0)
    book.add(code=1, shares=200.0, entry_px=20.0, entry_idx=6,
             high_px=21.0, high_hi=22.0)
    with pytest.raises(RuntimeError, match="满"):
        book.add(code=2, shares=300.0, entry_px=30.0, entry_idx=7,
                 high_px=31.0, high_hi=32.0)


def test_position_book_update_high_only_when_higher():
    """update_high 必须仅在更高时更新."""
    book = PositionBook(max_pos=10)
    p = book.add(code=0, shares=100.0, entry_px=10.0, entry_idx=5,
                 high_px=10.5, high_hi=11.0)
    # 更新更小值 — 不应改
    book.update_high(p, high_px=10.0, high_hi=10.5)
    pos = book.get(p)
    assert pos.high_px == 10.5
    assert pos.high_hi == 11.0
    # 更新更大值 — 应改
    book.update_high(p, high_px=12.0, high_hi=12.5)
    pos = book.get(p)
    assert pos.high_px == 12.0
    assert pos.high_hi == 12.5


def test_position_book_dtype_code_int32():
    """code 字段 dtype 必须 int32 (与 engine parity 锁定)."""
    book = PositionBook(max_pos=10)
    assert book.dtype_code == np.int32


def test_position_book_dtype_shares_float64():
    """shares 字段 dtype 必须 float64."""
    book = PositionBook(max_pos=10)
    assert book.dtype_shares == np.float64


# ═══════════════════════════════════════════════════════════════
# assert_state_dtype 守卫 (T-CR-2 修复, 2026-07-15)
# ═══════════════════════════════════════════════════════════════


def test_assert_state_dtype_passes():
    """合法 dtype 不抛异常."""
    pos = Position(
        code=np.int32(3), shares=np.float64(100.0), entry_px=np.float64(10.0),
        entry_idx=np.int32(5), high_px=np.float64(11.0), high_hi=np.float64(12.0),
        ladder_done=np.int32(0),
    )
    bar = Bar(close=10.0, high=11.0, low=9.0, open=10.0)
    assert_state_dtype(pos, bar)  # 无异常即 PASS


def test_assert_state_dtype_catches_wrong_code_type():
    """Position.code 传 float → TypeError."""
    pos = Position(
        code=3.0,  # 错误: float 而非 int
        shares=np.float64(100.0), entry_px=np.float64(10.0),
        entry_idx=np.int32(5), high_px=np.float64(11.0), high_hi=np.float64(12.0),
        ladder_done=np.int32(0),
    )
    bar = Bar(close=10.0, high=11.0, low=9.0, open=10.0)
    with pytest.raises(TypeError, match="Position.code"):
        assert_state_dtype(pos, bar)


def test_assert_state_dtype_catches_wrong_ladder_type():
    """Position.ladder_done 传 float → TypeError."""
    pos = Position(
        code=np.int32(3), shares=np.float64(100.0), entry_px=np.float64(10.0),
        entry_idx=np.int32(5), high_px=np.float64(11.0), high_hi=np.float64(12.0),
        ladder_done=1.5,  # 错误: float 而非 int
    )
    bar = Bar(close=10.0, high=11.0, low=9.0, open=10.0)
    with pytest.raises(TypeError, match="Position.ladder_done"):
        assert_state_dtype(pos, bar)


# ═══════════════════════════════════════════════════════════════
# TradeColumns 列索引锁定
# ═══════════════════════════════════════════════════════════════


def test_trade_columns_order_locked():
    """列索引顺序锁 — 防列错位导致 silent bug."""
    assert TradeColumns.CODE == 0
    assert TradeColumns.ENTRY_IDX == 1
    assert TradeColumns.EXIT_IDX == 2
    assert TradeColumns.ENTRY_PX == 3
    assert TradeColumns.SELL_PX == 4
    assert TradeColumns.SHARES == 5
    assert TradeColumns.PROFIT == 6
    assert TradeColumns.RETURN == 7
    assert TradeColumns.REASON == 8
    assert TradeColumns.NCOLS == 9


def test_trade_columns_ncols_matches_max_index():
    """NCOLS 必须 = 最大索引 + 1 (一致性锁)."""
    max_idx = max(
        TradeColumns.CODE, TradeColumns.ENTRY_IDX, TradeColumns.EXIT_IDX,
        TradeColumns.ENTRY_PX, TradeColumns.SELL_PX, TradeColumns.SHARES,
        TradeColumns.PROFIT, TradeColumns.RETURN, TradeColumns.REASON,
    )
    assert TradeColumns.NCOLS == max_idx + 1