"""
formula_exit 矩阵构造 + 缓存 — 纯函数单测

被测模块: backtest/formula_exit.py
  - build_formula_exit_matrix(signals_df, date_index, stock_columns) -> np.ndarray
  - cache_key(formula_name, formula_arg, codes, start_time, end_time, period) -> str
  - load_cached_formula_exit(cache_key_str, max_age_hours) -> Optional[FormulaExitResult]
  - save_cached_formula_exit(cache_key_str, result) -> Path
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.formula_exit import (
    FormulaExitResult,
    _CACHE_ROOT,
    build_formula_exit_matrix,
    cache_key,
    load_cached_formula_exit,
    save_cached_formula_exit,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def sample_idx():
    """10 个交易日的 DatetimeIndex."""
    return pd.DatetimeIndex(
        pd.bdate_range("2024-01-02", periods=10), name="date"
    )


@pytest.fixture
def sample_cols():
    """3 只股票."""
    return pd.Index(["600519.SH", "000002.SZ", "300750.SZ"])


# ---------------------------------------------------------------------------
# Test 1: 空 signals
# ---------------------------------------------------------------------------
def test_build_matrix_empty_signals(sample_idx, sample_cols):
    """空 signals DataFrame 返回全 False 矩阵."""
    matrix = build_formula_exit_matrix(pd.DataFrame(), sample_idx, sample_cols)
    assert matrix.shape == (10, 3)
    assert matrix.dtype == bool
    assert not matrix.any(), "空 signals 应返回全 False"


# ---------------------------------------------------------------------------
# Test 2: 单信号精确写位
# ---------------------------------------------------------------------------
def test_build_matrix_single_signal(sample_idx, sample_cols):
    """单信号精确写到 (i=5, j=1)."""
    signals = pd.DataFrame([
        {"stock_code": "000002.SZ", "select_date": sample_idx[5]},
    ])
    matrix = build_formula_exit_matrix(signals, sample_idx, sample_cols)
    assert matrix.shape == (10, 3)
    assert matrix[5, 1], "信号应写到 (5, 1)"
    assert matrix.sum() == 1, "只有 1 个信号"


# ---------------------------------------------------------------------------
# Test 3: 日期精确匹配 (searchsorted side='left' 语义)
# ---------------------------------------------------------------------------
def test_build_matrix_exact_date_match(sample_idx, sample_cols):
    """信号日 = 某个交易日 → 直接映射到该 bar."""
    signals = pd.DataFrame([
        {"stock_code": "600519.SH", "select_date": sample_idx[5]},
    ])
    matrix = build_formula_exit_matrix(signals, sample_idx, sample_cols)
    assert matrix[5, 0], "信号日 = idx[5] 应精确映射到 (5, 0)"
    assert matrix.sum() == 1


def test_build_matrix_date_before_index_aligns_to_first_bar(sample_idx, sample_cols):
    """信号日 < idx[0] → 应映射到 idx[0] (最早一个 bar)."""
    sig_date = sample_idx[0] - pd.Timedelta(days=30)
    signals = pd.DataFrame([
        {"stock_code": "600519.SH", "select_date": sig_date},
    ])
    matrix = build_formula_exit_matrix(signals, sample_idx, sample_cols)
    assert matrix[0, 0], "idx 之前的信号应映射到第一个 bar"
    assert matrix.sum() == 1


def test_build_matrix_date_after_index_is_skipped(sample_idx, sample_cols):
    """信号日 > idx[-1] → 跳过（不抛异常, 不写入矩阵)."""
    sig_date = sample_idx[-1] + pd.Timedelta(days=30)
    signals = pd.DataFrame([
        {"stock_code": "600519.SH", "select_date": sig_date},
    ])
    matrix = build_formula_exit_matrix(signals, sample_idx, sample_cols)
    assert not matrix.any(), "idx 之后的信号应被丢弃"


# ---------------------------------------------------------------------------
# Test 4: 代码大小写 + 后缀 normalize
# ---------------------------------------------------------------------------
def test_build_matrix_code_normalized(sample_idx, sample_cols):
    """小写 + 后缀变体都应被 normalize 到标准格式."""
    signals = pd.DataFrame([
        {"stock_code": "600519.sh", "select_date": sample_idx[3]},   # 小写
        {"stock_code": "000002",    "select_date": sample_idx[4]},   # 无后缀
        {"stock_code": "300750.sz", "select_date": sample_idx[6]},   # 标准小写
    ])
    matrix = build_formula_exit_matrix(signals, sample_idx, sample_cols)
    assert matrix[3, 0], "600519.sh → 600519.SH"
    assert matrix[4, 1], "000002 → 000002.SZ (自动推断后缀)"
    assert matrix[6, 2], "300750.sz → 300750.SZ"
    assert matrix.sum() == 3


# ---------------------------------------------------------------------------
# Test 5: 缓存键稳定
# ---------------------------------------------------------------------------
def test_cache_key_stable():
    """同输入 → 同 SHA-256."""
    codes = ("600519.SH", "000002.SZ")
    k1 = cache_key("卖出XG", "", codes, "20240101", "20250630", "1d")
    k2 = cache_key("卖出XG", "", codes, "20240101", "20250630", "1d")
    assert k1 == k2
    assert len(k1) == 64, "SHA-256 应为 64 个 hex 字符"


def test_cache_key_order_insensitive():
    """codes 顺序不影响 key (内部 sort)."""
    k1 = cache_key("卖出XG", "", ("600519.SH", "000002.SZ"), "20240101", "20250630")
    k2 = cache_key("卖出XG", "", ("000002.SZ", "600519.SH"), "20240101", "20250630")
    assert k1 == k2


def test_cache_key_differs_on_input():
    """输入不同 → key 不同."""
    k1 = cache_key("卖出XG", "", ("600519.SH",), "20240101", "20250630")
    k2 = cache_key("买入XG", "", ("600519.SH",), "20240101", "20250630")
    assert k1 != k2


# ---------------------------------------------------------------------------
# Test 6: 缓存往返
# ---------------------------------------------------------------------------
def test_cache_roundtrip(tmp_path, monkeypatch, sample_idx, sample_cols):
    """写缓存 → 读出来 matrix 一致."""
    monkeypatch.setattr("backtest.formula_exit._CACHE_ROOT", tmp_path)

    signals = pd.DataFrame([
        {"stock_code": "600519.SH", "select_date": sample_idx[3]},
        {"stock_code": "000002.SZ", "select_date": sample_idx[7]},
    ])
    matrix = build_formula_exit_matrix(signals, sample_idx, sample_cols)
    key = cache_key("卖出XG", "", tuple(sample_cols), "20240101", "20240131", "1d")
    meta = {"formula_name": "卖出XG", "fetched_at": datetime.now().isoformat()}
    save_cached_formula_exit(key, FormulaExitResult(matrix=matrix, meta=meta))

    loaded = load_cached_formula_exit(key)
    assert loaded is not None, "缓存应命中"
    assert np.array_equal(loaded.matrix, matrix), "matrix 内容应一致"
    assert loaded.meta["formula_name"] == "卖出XG"


# ---------------------------------------------------------------------------
# Test 7: 损坏 .npz → 读为 None
# ---------------------------------------------------------------------------
def test_cache_miss_on_corrupt_file(tmp_path, monkeypatch):
    """损坏的 .npz 不应让 load 抛异常, 应返回 None."""
    monkeypatch.setattr("backtest.formula_exit._CACHE_ROOT", tmp_path)
    key = cache_key("test", "", ("000001.SZ",), "20240101", "20250101")
    corrupt_path = tmp_path / f"{key}.npz"
    corrupt_path.write_bytes(b"this is not a valid npz file")

    loaded = load_cached_formula_exit(key)
    assert loaded is None, "损坏缓存应返回 None"


# ---------------------------------------------------------------------------
# Test 8: 缓存超期
# ---------------------------------------------------------------------------
def test_cache_expires(tmp_path, monkeypatch, sample_idx, sample_cols):
    """mtime 超过 max_age_hours → 视为未命中."""
    monkeypatch.setattr("backtest.formula_exit._CACHE_ROOT", tmp_path)

    signals = pd.DataFrame([
        {"stock_code": "600519.SH", "select_date": sample_idx[3]},
    ])
    matrix = build_formula_exit_matrix(signals, sample_idx, sample_cols)
    key = cache_key("卖出XG", "", tuple(sample_cols), "20240101", "20240131", "1d")
    save_cached_formula_exit(key, FormulaExitResult(matrix=matrix, meta={"formula_name": "卖出XG"}))

    # 把文件 mtime 倒推 25 小时
    cache_file = tmp_path / f"{key}.npz"
    old_time = time.time() - 25 * 3600
    os.utime(cache_file, (old_time, old_time))

    loaded = load_cached_formula_exit(key, max_age_hours=24)
    assert loaded is None, "25 小时前的缓存应被视为过期"


# ---------------------------------------------------------------------------
# Test 9: 缺列 / 错列 → 返回全 False 不抛异常
# ---------------------------------------------------------------------------
def test_build_matrix_missing_columns_returns_all_false(sample_idx, sample_cols):
    """signals_df 缺 stock_code 列 → 返回全 False 不抛异常."""
    bad_signals = pd.DataFrame([
        {"code": "600519.SH", "date": sample_idx[3]},   # 错的列名
    ])
    matrix = build_formula_exit_matrix(bad_signals, sample_idx, sample_cols)
    assert matrix.shape == (10, 3)
    assert not matrix.any()


def test_build_matrix_none_signals_returns_all_false(sample_idx, sample_cols):
    """None 输入 → 全 False."""
    matrix = build_formula_exit_matrix(None, sample_idx, sample_cols)
    assert matrix.shape == (10, 3)
    assert not matrix.any()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
