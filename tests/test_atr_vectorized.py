"""ATR 矩阵向量化等价性测试 (2026-07-18)。

锁定: 新版全矩阵实现与旧逐列 pandas 实现(甲骨文内联)数值一致(含 NaN 位置),
覆盖随机数据 + NaN 停牌散布 + period 边界。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.engine import _compute_atr_matrix


def _old_atr(high_np, low_np, close_np, period=14):
    """旧逐列实现 (甲骨文, 复制自向量化前 engine.py)。"""
    n, k = close_np.shape
    atr = np.full((n, k), np.nan, dtype=np.float64)
    for ci in range(k):
        h = pd.Series(high_np[:, ci])
        l = pd.Series(low_np[:, ci])
        c = pd.Series(close_np[:, ci])
        prev_c = c.shift(1)
        tr = pd.concat(
            [(h - l).abs(), (h - prev_c).abs(), (l - prev_c).abs()], axis=1
        ).max(axis=1)
        atr[:, ci] = tr.rolling(period, min_periods=1).mean().values
    return atr


def _make(n=300, k=20, nan_ratio=0.0, seed=3):
    rng = np.random.default_rng(seed)
    close = 10.0 * np.cumprod(1.0 + rng.normal(0, 0.02, (n, k)), axis=0)
    high = close * (1 + np.abs(rng.normal(0, 0.01, (n, k))))
    low = close * (1 - np.abs(rng.normal(0, 0.01, (n, k))))
    if nan_ratio > 0:
        mask = rng.random((n, k)) < nan_ratio
        high[mask] = np.nan
        low[mask] = np.nan
        close[mask] = np.nan
    return high, low, close


def _assert_atr_equal(a, b):
    both_nan = np.isnan(a) & np.isnan(b)
    assert (np.isnan(a) == np.isnan(b)).all(), "NaN 位置不一致"
    assert ((np.abs(a - b) < 1e-12) | both_nan).all(), "数值不一致"


def test_atr_vectorized_equals_old():
    h, l, c = _make()
    _assert_atr_equal(_compute_atr_matrix(h, l, c), _old_atr(h, l, c))


def test_atr_vectorized_equals_old_with_nan():
    h, l, c = _make(nan_ratio=0.08)
    _assert_atr_equal(_compute_atr_matrix(h, l, c), _old_atr(h, l, c))


def test_atr_period_1_and_large():
    h, l, c = _make()
    _assert_atr_equal(_compute_atr_matrix(h, l, c, period=1), _old_atr(h, l, c, period=1))
    _assert_atr_equal(_compute_atr_matrix(h, l, c, period=200), _old_atr(h, l, c, period=200))


def test_atr_single_column():
    h, l, c = _make(k=1)
    _assert_atr_equal(_compute_atr_matrix(h, l, c), _old_atr(h, l, c))
