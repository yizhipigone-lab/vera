"""入场信号预计算测试 — 直接测生产 helper (与 BacktestLoop.run 共用同一实现)。

2026-07-18 审计修复: 旧版测的是 idiom 副本(与生产脱钩), 现直测
backtest/loop/signals.py::precompute_signal_lists。
覆盖: 空 bar(首/中/尾)/全零/单 bar/非二值矩阵(F1 前视修复)。
"""
from __future__ import annotations

import numpy as np
import pytest

from backtest.loop.signals import precompute_signal_lists


def test_split_matches_per_row_nonzero():
    rng = np.random.default_rng(3)
    entry = rng.random((50, 20)) < 0.1
    segs = precompute_signal_lists(entry)
    assert len(segs) == 50
    for i in range(50):
        assert list(segs[i]) == list(np.nonzero(entry[i])[0]), f"bar {i} 段不一致"


def test_empty_bars_first_middle_last():
    entry = np.zeros((5, 4), dtype=bool)
    entry[1, 2] = True
    entry[3, 0] = True
    entry[3, 3] = True
    segs = precompute_signal_lists(entry)
    assert len(segs) == 5
    assert list(segs[0]) == []          # 首 bar 空
    assert list(segs[1]) == [2]
    assert list(segs[2]) == []          # 中间空
    assert list(segs[3]) == [0, 3]      # 同 bar 多信号升序
    assert list(segs[4]) == []          # 尾 bar 空


def test_all_zero_matrix():
    segs = precompute_signal_lists(np.zeros((7, 5), dtype=bool))
    assert len(segs) == 7
    assert all(len(s) == 0 for s in segs)


def test_single_bar():
    entry = np.zeros((1, 3), dtype=bool)
    entry[0, 1] = True
    segs = precompute_signal_lists(entry)
    assert len(segs) == 1
    assert list(segs[0]) == [1]


def test_non_binary_int_matrix_no_misalignment():
    """F1 回归: int 型非二值(值=2)信号不得被挪到更早的 bar (静默前视修复)。

    修复前: cuts 按 sum(取值) 切, nonzero 按个数切 → bar2 的两个信号被划给
    bar0/bar1。修复后: count_nonzero 按个数切, 与 nonzero 来源严格一致。
    """
    entry = np.array([[2, 0], [0, 2], [2, 2]])
    segs = precompute_signal_lists(entry)
    assert list(segs[0]) == [0]
    assert list(segs[1]) == [1]
    assert list(segs[2]) == [0, 1]


def test_non_binary_matches_legacy_truthy_semantics():
    """任意 truthy 值都算信号, 与 legacy `if not entry_np[i, ci]` 一致。"""
    entry = np.array([[0.5, 0.0, -3.0], [0.0, 0.0, 0.0]], dtype=float)
    segs = precompute_signal_lists(entry)
    assert list(segs[0]) == [0, 2]
    assert list(segs[1]) == []


def test_loop_reuse_fails_fast():
    """F2 回归: BacktestLoop 实例二次 run → RuntimeError (脏状态防呆)。"""
    from backtest.loop.builder import build_backtest_loop
    price = 10.0 * np.ones((4, 2))
    entry = np.zeros((4, 2), dtype=bool)
    loop = build_backtest_loop(
        100000.0, 0.0, 2000.0, 20000.0, 100, 1,
        False, -0.12, False, 0.05, 0.03,
        False, np.array([0.06]), np.array([0.5]), 1,
        False, 20, False, 7, 0.01)
    loop.run(price, entry, None, None, None, None, None, None)
    with pytest.raises(RuntimeError, match="不可复用"):
        loop.run(price, entry, None, None, None, None, None, None)
