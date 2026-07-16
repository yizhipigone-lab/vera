"""代码标准化工具单元测试 (覆盖率靶向, 2026-07-15)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.code_normalizer import normalize, normalize_list, to_market_format, get_market


def test_normalize_with_suffix():
    """已带后缀的代码原样标准化."""
    assert normalize("600519.SH") == "600519.SH"
    assert normalize("000001.SZ") == "000001.SZ"
    assert normalize("300750 sz") == "300750.SZ"


def test_normalize_without_suffix():
    """无后缀代码根据首位数字推断."""
    assert normalize("600519") == "600519.SH"  # 6开头→SH
    assert normalize("000001") == "000001.SZ"  # 0开头→SZ
    assert normalize("300750") == "300750.SZ"  # 3开头→SZ
    assert normalize("830799") == "830799.BJ"  # 8开头→BJ


def test_normalize_list_dedup():
    """批量标准化并去重."""
    result = normalize_list(["600519", "600519.SH", "000001"])
    assert result == ["600519.SH", "000001.SZ"]


def test_to_market_format():
    """转换为 TDX 市场#代码格式."""
    result = to_market_format(["600519.SH", "000001.SZ"])
    assert "1#600519" in result
    assert "0#000001" in result


def test_get_market():
    """获取单个代码的市场后缀."""
    assert get_market("600519") == "SH"
    assert get_market("000001") == "SZ"
    assert get_market("830799") == "BJ"
    assert get_market("invalid") is None
