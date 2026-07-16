"""去重器单元测试 (覆盖率靶向, 2026-07-15)."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from selection.deduplicator import Deduplicator


def test_deduplicate_normal():
    """正常去重: 重复 (stock_code, select_date) 保留第一条."""
    d = Deduplicator()
    df = pd.DataFrame({
        "stock_code": ["000001.SZ", "000001.SZ", "600519.SH"],
        "select_date": pd.to_datetime(["2026-01-05", "2026-01-05", "2026-01-10"]),
        "formula_name": ["XG", "XG", "XG"],
    })
    result = d.deduplicate(df)
    assert len(result) == 2
    assert result.iloc[0]["stock_code"] == "000001.SZ"


def test_deduplicate_empty():
    """空 DataFrame 原样返回."""
    d = Deduplicator()
    empty = pd.DataFrame()
    result = d.deduplicate(empty)
    assert result.empty


def test_deduplicate_keep_first():
    """去重时 keep='first' 保留首次出现的行."""
    d = Deduplicator()
    df = pd.DataFrame({
        "stock_code": ["000001.SZ", "000001.SZ"],
        "select_date": pd.to_datetime(["2026-01-05", "2026-01-05"]),
        "value": [100, 200],
    })
    result = d.deduplicate(df)
    assert len(result) == 1
    assert result.iloc[0]["value"] == 100  # keep first
