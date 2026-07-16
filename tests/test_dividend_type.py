"""
候选 D 修复权口径分裂 — helper 边界 + 一致性测试 (GREEN 守卫)

覆盖:
- to_tdx_str: str/int/数字str/未知 全部归一化到 TDX get_market_data 用字符串
- to_formula_int: str/int/数字str/未知 全部归一化到 TDX formula_process 用整数
- assert_consistent: 一致不 raise / 不一致 raise ValueError
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.dividend_type import (
    ADJ_NONE, ADJ_FRONT, ADJ_BACK, ADJ_FRONT_RAW,
    ADJ_TO_TDX_STR, ADJ_TO_FORMULA_INT,
    to_tdx_str, to_formula_int, assert_consistent,
)


# === to_tdx_str: str 枚举直通 ===
@pytest.mark.parametrize("adj,expected", [
    (ADJ_NONE, "none"),
    (ADJ_FRONT, "front"),
    (ADJ_BACK, "back"),
    (ADJ_FRONT_RAW, "front_raw"),
])
def test_to_tdx_str_enum(adj, expected):
    assert to_tdx_str(adj) == expected


# === to_tdx_str: int → str (修复核心: 选股传 int 给回测 API 也能用) ===
@pytest.mark.parametrize("adj_int,expected", [
    (0, "none"),
    (1, "front"),
    (2, "back"),
])
def test_to_tdx_str_from_int(adj_int, expected):
    assert to_tdx_str(adj_int) == expected


# === to_tdx_str: 字符串数字兜底 ===
@pytest.mark.parametrize("adj_str,expected", [
    ("0", "none"),
    ("1", "front"),
    ("2", "back"),
])
def test_to_tdx_str_from_numeric_string(adj_str, expected):
    assert to_tdx_str(adj_str) == expected


# === to_tdx_str: 未知值兜底为前复权 ===
@pytest.mark.parametrize("adj", [3, 99, "未知", "xyz", None])
def test_to_tdx_str_unknown_falls_back_to_front(adj):
    assert to_tdx_str(adj) == "front"


# === to_formula_int: int 直通 ===
@pytest.mark.parametrize("adj_int,expected", [
    (0, 0),
    (1, 1),
    (2, 2),
])
def test_to_formula_int_int(adj_int, expected):
    assert to_formula_int(adj_int) == expected


# === to_formula_int: str 枚举 → int (修复核心: 回测传 str 给选股 API 也能用) ===
@pytest.mark.parametrize("adj_str,expected", [
    (ADJ_NONE, 0),
    (ADJ_FRONT, 1),
    (ADJ_BACK, 2),
    (ADJ_FRONT_RAW, 1),
])
def test_to_formula_int_from_str(adj_str, expected):
    assert to_formula_int(adj_str) == expected


# === to_formula_int: 字符串数字兜底 ===
@pytest.mark.parametrize("adj_str,expected", [
    ("0", 0),
    ("1", 1),
    ("2", 2),
])
def test_to_formula_int_from_numeric_string(adj_str, expected):
    assert to_formula_int(adj_str) == expected


# === to_formula_int: 未知值兜底为前复权 ===
@pytest.mark.parametrize("adj", [3, 99, "未知", "xyz", None])
def test_to_formula_int_unknown_falls_back_to_front(adj):
    assert to_formula_int(adj) == 1


# === to_formula_int: 超出 {0,1,2} 的 int 兜底 ===
def test_to_formula_int_out_of_range_int_falls_back_to_front():
    assert to_formula_int(3) == 1
    assert to_formula_int(-1) == 1


# === assert_consistent: 一致不 raise (两端同语义, 不论 str/int) ===
@pytest.mark.parametrize("sel_adj,bt_adj", [
    (ADJ_FRONT, ADJ_FRONT),
    ("front", "front"),
    (1, "front"),       # 选股 int vs 回测 str — 一致
    ("front", 1),       # 反向 — 一致
    (ADJ_NONE, ADJ_NONE),
    (0, "none"),        # 0 vs "none" — 一致
    (ADJ_BACK, ADJ_BACK),
])
def test_assert_consistent_same_semantics(sel_adj, bt_adj):
    assert_consistent(sel_adj, bt_adj)  # 不 raise


# === assert_consistent: 不一致 raise ValueError ===
@pytest.mark.parametrize("sel_adj,bt_adj", [
    (ADJ_FRONT, ADJ_NONE),       # 前 vs 不
    (1, "none"),                  # 1 vs none
    ("front", "back"),
    (ADJ_FRONT, ADJ_FRONT_RAW),  # 前 vs 前含当日 — 严格语义不同
])
def test_assert_consistent_mismatch_raises(sel_adj, bt_adj):
    with pytest.raises(ValueError, match="复权口径不一致"):
        assert_consistent(sel_adj, bt_adj)


# === ADJ_TO_TDX_STR / ADJ_TO_FORMULA_INT 映射一致性 ===
def test_adj_mappings_consistent():
    """每个枚举值在两个映射表中都有, 不出现"一边有另一边没有"的悬空值."""
    for adj in (ADJ_NONE, ADJ_FRONT, ADJ_BACK, ADJ_FRONT_RAW):
        assert adj in ADJ_TO_TDX_STR, f"{adj} 缺 TDX 字符串映射"
        assert adj in ADJ_TO_FORMULA_INT, f"{adj} 缺 formula 整数映射"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


# === 覆盖率靶向 (2026-07-15) ===

def test_to_tdx_str_invalid_int_fallback():
    """非法 int (如 99) → 兜底 'front'."""
    assert to_tdx_str(99) == "front"


def test_to_formula_int_invalid_str_fallback():
    """非法字符串 → 兜底 1."""
    assert to_formula_int("未知值") == 1


def test_assert_consistent_mixed_types_same_semantic():
    """int 1 和 str 'front' 语义相同 → 不抛."""
    assert_consistent(1, "front")  # 不抛即过


def test_to_tdx_str_digit_str():
    """字符串数字 '0'/'1'/'2' → 对应字符串."""
    assert to_tdx_str("0") == "none"
    assert to_tdx_str("1") == "front"
    assert to_tdx_str("2") == "back"


def test_to_formula_int_digit_str():
    """字符串数字 '2' → 整数 2."""
    assert to_formula_int("2") == 2


def test_to_formula_int_out_of_range():
    """越界整数 99 → 兜底 1."""
    assert to_formula_int(99) == 1