"""safe_serialize 全类型覆盖测试 (迭代 3, 2026-07-15).

锁住 JSON 序列化器所有边界:
- np.integer / np.floating / float / np.ndarray / pd.Timestamp / None / 字符串
- NaN / Inf / -Inf
- 嵌套字典
- 空容器
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.result_writer import safe_serialize


# ═══════════════════════════════════════════════════════════════
# 标量路径
# ═══════════════════════════════════════════════════════════════


def test_serialize_python_int():
    """int → int (直通)."""
    assert safe_serialize(42) == 42


def test_serialize_np_int8():
    """np.int8 → int."""
    assert safe_serialize(np.int8(10)) == 10
    assert isinstance(safe_serialize(np.int8(10)), int)


def test_serialize_np_int64():
    """np.int64 → int."""
    val = np.int64(9999999999)
    out = safe_serialize(val)
    assert out == 9999999999
    assert isinstance(out, int)


def test_serialize_python_float():
    """普通 float → float (直通)."""
    assert safe_serialize(3.14) == 3.14


def test_serialize_np_float32():
    """np.float32 → float."""
    val = np.float32(1.5)
    out = safe_serialize(val)
    assert out == pytest.approx(1.5)
    assert isinstance(out, float)


def test_serialize_np_float64():
    """np.float64 → float."""
    val = np.float64(2.71828)
    out = safe_serialize(val)
    assert out == pytest.approx(2.71828)
    assert isinstance(out, float)


def test_serialize_string_passthrough():
    """str → str."""
    assert safe_serialize("平安银行") == "平安银行"


def test_serialize_none_passthrough():
    """None → None."""
    assert safe_serialize(None) is None


def test_serialize_bool_passthrough():
    """bool → bool (np.bool_ 走 np.integer 分支)."""
    assert safe_serialize(True) is True
    assert safe_serialize(False) is False
    # np.bool_ → Python int (numpy 2.x 兼容: 显式捕获, 不再走 catch-all)
    out_true = safe_serialize(np.bool_(True))
    assert out_true == 1
    assert isinstance(out_true, int), f"np.bool_ 应转为 int, 实际 {type(out_true)}"
    out_false = safe_serialize(np.bool_(False))
    assert out_false == 0
    assert isinstance(out_false, int)


# ═══════════════════════════════════════════════════════════════
# NaN / Inf 边界
# ═══════════════════════════════════════════════════════════════


def test_serialize_nan_float_to_none():
    """NaN float → None (JSON 标准, 不允许 NaN)."""
    assert safe_serialize(float("nan")) is None


def test_serialize_inf_float_to_none():
    """+Inf → None."""
    assert safe_serialize(float("inf")) is None


def test_serialize_neg_inf_float_to_none():
    """-Inf → None."""
    assert safe_serialize(float("-inf")) is None


def test_serialize_nan_np_float_to_none():
    """np.nan → None."""
    assert safe_serialize(np.float64("nan")) is None


def test_serialize_inf_np_float_to_none():
    """np.inf → None."""
    assert safe_serialize(np.float64("inf")) is None


def test_serialize_zero_passes_through():
    """0.0 (合法) → 0.0 (不容差误判)."""
    assert safe_serialize(0.0) == 0.0
    assert safe_serialize(np.float64(0.0)) == 0.0


# ═══════════════════════════════════════════════════════════════
# 容器路径
# ═══════════════════════════════════════════════════════════════


def test_serialize_1d_ndarray_to_list():
    """np.ndarray 1D → list."""
    arr = np.array([1.0, 2.0, 3.0])
    out = safe_serialize(arr)
    assert out == [1.0, 2.0, 3.0]
    assert isinstance(out, list)


def test_serialize_2d_ndarray_to_list():
    """np.ndarray 2D → list of list."""
    arr = np.array([[1, 2], [3, 4]])
    out = safe_serialize(arr)
    assert out == [[1, 2], [3, 4]]


def test_serialize_timestamp_to_isoformat():
    """pd.Timestamp → isoformat 字符串."""
    ts = pd.Timestamp("2024-01-15 09:30:00")
    out = safe_serialize(ts)
    assert isinstance(out, str)
    assert "2024-01-15" in out
    assert "T" in out  # ISO 8601


def test_serialize_nan_in_list_to_none():
    """list 中含 NaN → 递归处理，NaN→None."""
    lst = [1.0, float("nan"), 3.0]
    out = safe_serialize(lst)
    assert out == [1.0, None, 3.0]  # 递归: NaN→None


# ═══════════════════════════════════════════════════════════════
# 异常 / 边界输入
# ═══════════════════════════════════════════════════════════════


def test_serialize_empty_string():
    """空字符串 → 空字符串."""
    assert safe_serialize("") == ""


def test_serialize_list_passthrough():
    """list 递归处理 — 无 NaN 时结构等价 (== 仍通过)."""
    lst = [1, 2.0, "three"]
    out = safe_serialize(lst)
    assert out == lst  # 递归但无 NaN, == 仍成立 (新 list, 值相同)
    assert out is not lst  # 不可变: 返回新对象


def test_serialize_dict_passthrough():
    """dict 递归处理 — 无 NaN 时结构等价 (== 仍通过)."""
    d = {"a": 1, "b": 2.0}
    out = safe_serialize(d)
    assert out == d  # 递归但无 NaN, == 仍成立 (新 dict, 值相同)
    assert out is not d  # 不可变: 返回新对象


def test_serialize_object_no_pickle_fallback():
    """未知对象原样返回 (不调 pickle)."""
    class Custom:
        def __repr__(self):
            return "<Custom>"

    c = Custom()
    assert safe_serialize(c) is c  # 不递归不 pickle, 直通


# ═══════════════════════════════════════════════════════════════
# 真实 metrics 场景
# ═══════════════════════════════════════════════════════════════


def test_serialize_realistic_metrics_dict():
    """模拟 metrics dict 序列化 — 递归后全部字段转为 JSON 安全类型."""
    metrics = {
        "cumulative_return": 0.25,
        "sharpe_ratio": 1.5,
        "max_drawdown": -0.10,
        # 极端值
        "win_rate": float("nan"),
        "profit_factor": float("inf"),
        # np 类型
        "total_trades": np.int64(42),
        "avg_hold_days": np.float64(15.3),
        # 时间戳
        "start_date": pd.Timestamp("2024-01-01"),
    }
    out = safe_serialize(metrics)
    assert isinstance(out, dict)
    # 标量正常值不变
    assert out["cumulative_return"] == 0.25
    assert out["sharpe_ratio"] == 1.5
    assert out["max_drawdown"] == -0.10
    # NaN/Inf → None
    assert out["win_rate"] is None
    assert out["profit_factor"] is None
    # np 类型 → Python 原生类型
    assert out["total_trades"] == 42
    assert isinstance(out["total_trades"], int)
    assert out["avg_hold_days"] == pytest.approx(15.3)
    assert isinstance(out["avg_hold_days"], float)
    # Timestamp → isoformat 字符串
    assert isinstance(out["start_date"], str)
    assert "2024-01-01" in out["start_date"]


def test_safe_serialize_does_not_mutate_input():
    """safe_serialize 必须不修改入参."""
    original = [1.0, 2.0, 3.0]
    arr = np.array(original)
    safe_serialize(arr)
    assert list(arr) == original  # 原 array 未被改


# ═══════════════════════════════════════════════════════════════
# 递归容器测试 (T-H-1, 2026-07-15)
# ═══════════════════════════════════════════════════════════════


def test_recursive_dict_nan():
    """嵌套 dict 中 NaN → None."""
    out = safe_serialize({"a": float("nan")})
    assert out == {"a": None}


def test_recursive_list_nan():
    """嵌套 list 中 NaN/Inf 递归处理."""
    out = safe_serialize([float("nan"), 1.0, [float("inf"), 2.0]])
    assert out == [None, 1.0, [None, 2.0]]


def test_recursive_deep_nesting():
    """深层嵌套 dict 中 NaN → None."""
    out = safe_serialize({"a": {"b": {"c": float("nan")}}})
    assert out == {"a": {"b": {"c": None}}}


def test_recursive_tuple_to_list():
    """tuple 转 list 并递归 NaN→None."""
    out = safe_serialize({"a": (1.0, float("nan"))})
    assert out == {"a": [1.0, None]}
    assert isinstance(out["a"], list)  # tuple → list


def test_recursive_set_to_list():
    """set 转 list 并递归 NaN→None."""
    out = safe_serialize({1.0, float("nan")})
    assert isinstance(out, list)
    assert None in out
    assert 1.0 in out


def test_recursive_empty_containers():
    """空容器递归不变."""
    assert safe_serialize({}) == {}
    assert safe_serialize([]) == []
    assert safe_serialize(()) == []


def test_recursive_does_not_mutate():
    """递归不修改入参 (不可变)."""
    original = {"a": [1.0, {"b": float("nan")}]}
    safe_serialize(original)
    # 原 dict/list 结构未被原地修改
    import math
    assert math.isnan(original["a"][1]["b"])


def test_json_dumps_no_error():
    """json.dumps 不因 NaN/Inf 抛异常."""
    import json
    data = {
        "metrics": {
            "monthly": {"jan": float("nan"), "feb": 0.05},
            "ratios": [float("inf"), 1.5, float("-inf")],
        }
    }
    cleaned = safe_serialize(data)
    result = json.dumps(cleaned)  # 不应抛 ValueError
    assert "null" in result  # NaN/Inf → null


def test_recursive_nat_to_none():
    """pd.NaT → None."""
    out = safe_serialize({"a": pd.NaT})
    assert out == {"a": None}


def test_recursive_frozenset_to_list():
    """frozenset 转 list 并递归 NaN→None."""
    out = safe_serialize(frozenset([1.0, float("nan")]))
    assert isinstance(out, list)
    assert None in out
    assert 1.0 in out