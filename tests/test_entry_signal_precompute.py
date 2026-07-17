"""Phase 1 项2 (2026-07-17): 入场信号预计算的切分正确性。

锁定 np.split(np.nonzero(entry)[1], cumsum 切点) 这一 idiom 的边界行为:
空 bar(首/中/尾) 必须得到空段, 信号列必须升序且归属正确 bar。
"""
from __future__ import annotations

import numpy as np


def _sig_by_bar(entry_np):
    """与 BacktestLoop.run 内相同的预计算 idiom。"""
    sig_rows, sig_cols = np.nonzero(entry_np)
    cuts = np.cumsum(entry_np.sum(axis=1))[:-1]
    return np.split(sig_cols, cuts)


def test_split_matches_per_row_nonzero():
    rng = np.random.default_rng(3)
    entry = rng.random((50, 20)) < 0.1
    segs = _sig_by_bar(entry)
    assert len(segs) == 50
    for i in range(50):
        assert list(segs[i]) == list(np.nonzero(entry[i])[0]), f"bar {i} 段不一致"


def test_empty_bars_first_middle_last():
    entry = np.zeros((5, 4), dtype=bool)
    entry[1, 2] = True
    entry[3, 0] = True
    entry[3, 3] = True
    segs = _sig_by_bar(entry)
    assert len(segs) == 5
    assert list(segs[0]) == []          # 首 bar 空
    assert list(segs[1]) == [2]
    assert list(segs[2]) == []          # 中间空
    assert list(segs[3]) == [0, 3]      # 同 bar 多信号升序
    assert list(segs[4]) == []          # 尾 bar 空


def test_all_zero_matrix():
    segs = _sig_by_bar(np.zeros((7, 5), dtype=bool))
    assert len(segs) == 7
    assert all(len(s) == 0 for s in segs)


def test_single_bar():
    entry = np.zeros((1, 3), dtype=bool)
    entry[0, 1] = True
    segs = _sig_by_bar(entry)
    assert len(segs) == 1
    assert list(segs[0]) == [1]
