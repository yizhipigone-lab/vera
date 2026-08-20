"""EntryEngine buy_price_np (open_t1 买入价矩阵) 单元测试 (计划书 Task 2).

计划书: docs/plan/2026-08-20_回测次日开盘买入模式_计划书.md
"""
import numpy as np
import pytest

from backtest._entry_basis import EntryPath
from backtest.loop.entry import ENTRY_PATH, EntryEngine
from backtest.loop.state import BacktestParams, PositionBook, TradeBuffer


def _params():
    return BacktestParams(initial_capital=100000, commission=0.0,
                          slippage=0.0, stamp_tax=0.0,
                          min_buy_amount=1000, max_buy_amount=50000,
                          lot_size=100, min_lots=1)


def test_buy_price_np_overrides_close():
    """buy_price_np 提供时按开盘价成交, 不是收盘价。"""
    p = _params()
    price = np.array([[10.0], [10.5]])      # 收盘价矩阵
    buy_px = np.array([[9.8], [10.0]])      # 开盘价矩阵(买入价)
    entry = np.array([[True], [False]])
    eng = EntryEngine(p, buy_price_np=buy_px)
    book, buf = PositionBook(), TradeBuffer(2, 1)
    eng.run_bar(0, 100000.0, book, buf, price, entry, None, 100000.0)
    assert book.count == 1
    assert book.get(0).entry_px == pytest.approx(9.8)


def test_default_zero_change():
    """不传 buy_price_np → 老口径收盘价, 零行为变化。"""
    p = _params()
    price = np.array([[10.0], [10.5]])
    entry = np.array([[True], [False]])
    eng = EntryEngine(p)
    book, buf = PositionBook(), TradeBuffer(2, 1)
    eng.run_bar(0, 100000.0, book, buf, price, entry, None, 100000.0)
    assert book.get(0).entry_px == pytest.approx(10.0)


def test_entry_path_per_instance():
    """entry_path 默认 BACKTEST_T_CLOSE; 传 buy_price_np 自动转 T1_OPEN。"""
    assert EntryEngine(_params()).entry_path is EntryPath.BACKTEST_T_CLOSE
    eng = EntryEngine(_params(), buy_price_np=np.zeros((1, 1)))
    assert eng.entry_path is EntryPath.BACKTEST_T1_OPEN
    assert ENTRY_PATH is EntryPath.BACKTEST_T_CLOSE   # 模块常量不变 (向后兼容)


def test_buy_price_requires_t1_path():
    """buy_price_np + 显式非 T1 路径 → fail-fast (防口径混用)。"""
    with pytest.raises(ValueError):
        EntryEngine(_params(), buy_price_np=np.zeros((1, 1)),
                    entry_path=EntryPath.BACKTEST_T_CLOSE)


def test_buy_price_nan_skipped():
    """开盘价缺失(NaN)的信号照旧 skip (不静默吞信号, 计数)。"""
    p = _params()
    price = np.array([[10.0], [10.5]])
    buy_px = np.array([[np.nan], [10.0]])
    entry = np.array([[True], [False]])
    eng = EntryEngine(p, buy_price_np=buy_px)
    book, buf = PositionBook(), TradeBuffer(2, 1)
    eng.run_bar(0, 100000.0, book, buf, price, entry, None, 100000.0)
    assert book.count == 0
    assert eng.skipped_signal_count == 1
