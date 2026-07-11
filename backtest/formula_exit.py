"""公式卖出信号矩阵 — 把 TDX 返回的 (stock, date) 信号集合转成 (n_dates, n_stocks) bool 矩阵。

主要职责：
  1. build_formula_exit_matrix — 矩阵构造（核心纯函数）
  2. cache_key / load_cached / save_cached — 24h TTL 磁盘缓存（避免每次回测都跑 TDX）

矩阵形状约定（与 backtest/engine.py:_simulate_core_v3 主循环一致）：
  matrix[i, j] = True 表示"在 date_index[i] 这天，stock_columns[j] 这只股票，TDX 公式命中"
  dtype: bool（节省内存 + 直接当 numpy bool 索引用）
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from utils.code_normalizer import normalize_list
from utils.logger import get_logger

logger = get_logger(__name__)


# 缓存根目录（用 ~/.cache/vera/formula_exit/，跨平台统一）
_CACHE_ROOT = Path(os.environ.get("VERA_CACHE_DIR", str(Path.home() / ".cache" / "vera" / "formula_exit")))


@dataclass(frozen=True)
class FormulaExitResult:
    """公式卖出矩阵 + meta 信息。"""

    matrix: np.ndarray            # shape (n_dates, n_stocks), dtype=bool
    meta: dict = field(default_factory=dict)   # {'formula_name', 'fetched_at', 'total_signals', ...}


def build_formula_exit_matrix(
    signals_df: pd.DataFrame,
    date_index: pd.DatetimeIndex,
    stock_columns: pd.Index,
    *,
    dtype: np.dtype = np.bool_,
) -> np.ndarray:
    """
    将 TDX 返回的 (stock_code, select_date) 信号集合转成 (n_dates, n_stocks) bool 矩阵。

    Args:
        signals_df: 至少含 stock_code (str) 和 select_date (datetime/str) 两列
        date_index: 回测时间索引（与 close.index 对齐）
        stock_columns: 回测股票列（与 close.columns 对齐）
        dtype: numpy dtype，默认 bool

    Returns:
        shape = (len(date_index), len(stock_columns)) 的 bool 数组。
        缺失位置为 False（无信号）。任何错误都不抛异常，最差返回全 False 矩阵。
    """
    n_dates = len(date_index)
    n_stocks = len(stock_columns)
    matrix = np.zeros((n_dates, n_stocks), dtype=dtype)

    if signals_df is None or signals_df.empty:
        return matrix
    if n_dates == 0 or n_stocks == 0:
        return matrix
    if "stock_code" not in signals_df.columns or "select_date" not in signals_df.columns:
        logger.warning(
            "build_formula_exit_matrix: signals_df 缺少 stock_code/select_date 列, 返回全 False"
        )
        return matrix

    # 1. 股票代码 → 列索引映射（用 normalize_list 处理大小写 / 后缀）
    stock_columns_list = list(stock_columns)
    code_to_col_idx: dict[str, int] = {}
    for idx, raw_code in enumerate(stock_columns_list):
        normalized = normalize_list([str(raw_code)])
        if normalized:
            code_to_col_idx[normalized[0]] = idx

    if not code_to_col_idx:
        logger.warning("build_formula_exit_matrix: 没有可识别的股票代码, 返回全 False")
        return matrix

    # 2. 日期索引 → 行号映射（用 searchsorted 做 O(log n) 二分）
    #    关键: pd.DatetimeIndex 必须 sorted, _ensure_index 在 engine.py:677 已排序
    date_values = date_index.values  # numpy datetime64[ns]
    n_mapped = 0
    n_skipped_code = 0
    n_skipped_date = 0

    for _, row in signals_df.iterrows():
        raw_code = row.get("stock_code")
        if raw_code is None:
            continue
        normalized_codes = normalize_list([str(raw_code)])
        if not normalized_codes:
            n_skipped_code += 1
            continue
        col_idx = code_to_col_idx.get(normalized_codes[0])
        if col_idx is None:
            n_skipped_code += 1
            continue

        # 日期转换
        select_date = pd.to_datetime(row["select_date"])
        if pd.isna(select_date):
            continue
        # searchsorted 找最近 bar（信号日 ≤ idx[k] 则映射到 idx[k]，即"T+1 同 bar 可触发出信号"语义；
        # 引擎里另有 formula_exit_lag_bars=1 处理 T+1 偏移）
        date_val = np.datetime64(select_date.to_pydatetime())
        pos = np.searchsorted(date_values, date_val, side="left")
        if pos >= n_dates:
            n_skipped_date += 1
            continue
        # 严格大于 pos-1 对应的日期才接受（防止重复映射）
        if pos > 0 and date_values[pos - 1] == date_val:
            row_idx = pos - 1
        else:
            row_idx = pos

        matrix[row_idx, col_idx] = True
        n_mapped += 1

    logger.info(
        "build_formula_exit_matrix: mapped=%d skipped_code=%d skipped_date=%d "
        "matrix.shape=%s signal_density=%.4f",
        n_mapped, n_skipped_code, n_skipped_date,
        matrix.shape, float(matrix.sum()) / matrix.size if matrix.size else 0.0,
    )
    return matrix


# ---------------------------------------------------------------------------
# 缓存层（24h TTL, SHA-256 key, 原子写）
# ---------------------------------------------------------------------------

def cache_key(
    formula_name: str,
    formula_arg: str,
    codes: tuple[str, ...],
    start_time: str,
    end_time: str,
    period: str = "1d",
) -> str:
    """
    生成稳定的 SHA-256 缓存键。

    把所有入参序列化到 JSON（key 排序保证稳定），再 hash。
    """
    payload = {
        "formula_name": formula_name,
        "formula_arg": formula_arg,
        "codes": sorted(codes),       # 排序：调用方传 tuple 顺序无关
        "start_time": start_time,
        "end_time": end_time,
        "period": period,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_path(key: str) -> Path:
    """给定 key 返回 .npz 文件路径。"""
    return _CACHE_ROOT / f"{key}.npz"


def load_cached_formula_exit(
    cache_key_str: str,
    max_age_hours: int = 24,
) -> Optional[FormulaExitResult]:
    """
    读缓存。返回 None 表示"未命中 / 已过期 / 损坏"。
    任何错误不抛异常, 仅记 warning。
    """
    path = _cache_path(cache_key_str)
    if not path.exists():
        return None

    try:
        # 超期检查（mtime vs now）
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if datetime.now() - mtime > timedelta(hours=max_age_hours):
            logger.info("formula_exit cache expired: %s", path.name)
            return None

        # 读 .npz（numpy 自带格式）
        with np.load(path, allow_pickle=True) as data:
            matrix = data["matrix"]
            meta_raw = data["meta"]
            # numpy 0-d array of object → 解包 dict
            meta = json.loads(str(meta_raw)) if meta_raw.size > 0 else {}

        return FormulaExitResult(matrix=matrix.astype(bool), meta=meta)
    except Exception as e:
        logger.warning("formula_exit cache read failed (%s): %s", path.name, e)
        return None


def save_cached_formula_exit(
    cache_key_str: str,
    result: FormulaExitResult,
) -> Path:
    """
    原子写缓存。先写到临时文件再 rename，避免半写状态。
    """
    path = _cache_path(cache_key_str)
    try:
        _CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        # 临时文件写在同目录, 避免跨文件系统 rename 失败
        fd, tmp_path = tempfile.mkstemp(
            dir=_CACHE_ROOT, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as f:
                np.savez(
                    f,
                    matrix=result.matrix.astype(bool),
                    meta=np.array(json.dumps(result.meta, ensure_ascii=False), dtype=object),
                )
            os.replace(tmp_path, path)
            logger.info("formula_exit cache saved: %s (signals=%d)", path.name, int(result.matrix.sum()))
        except Exception:
            # 清理临时文件
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
    except Exception as e:
        logger.warning("formula_exit cache write failed (%s): %s", cache_key_str[:12], e)
    return path


__all__ = [
    "FormulaExitResult",
    "build_formula_exit_matrix",
    "cache_key",
    "load_cached_formula_exit",
    "save_cached_formula_exit",
]
