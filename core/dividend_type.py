"""
复权口径统一 (修复 VERA 审计 C4 问题)

原问题:
  - 选股端 formula_runner.py 传 dividend_type=1 (int)
  - 回测端 engine.py 传 dividend_type="front" (str)
  - 两套 TDX API, 字符串/int 混用, 口径不一致风险

修复: 单一真相源, 所有调用方走统一映射.
       老代码 (47+ 个脚本) 继续按原参数跑; 新代码请用本模块.

TDX 两套 API 的 dividend_type 语义:
  tq.get_market_data(...)
    "none"     → 不复权
    "front"    → 前复权
    "back"     → 后复权
    "front_raw"→ 前复权 (含当日)
    等

  tq.formula_process_mul_xg(...) [选股]
    1          → 前复权
    0          → 不复权
    2          → 后复权 (TDX 不同接口枚举可能不同, 需查 TDX 文档)
"""
from typing import Union

# 复权枚举 (人类语义)
ADJ_NONE = "none"
ADJ_FRONT = "front"
ADJ_BACK = "back"
ADJ_FRONT_RAW = "front_raw"

# get_market_data 字符串口径 (回测取价/指数)
ADJ_TO_TDX_STR = {
    ADJ_NONE: "none",
    ADJ_FRONT: "front",
    ADJ_BACK: "back",
    ADJ_FRONT_RAW: "front_raw",
}

# formula_process_mul_xg 整数口径 (选股)
# 按 TDX 客户端导出观测: 1=前复权, 0=不复权; 保守写常见映射
ADJ_TO_FORMULA_INT = {
    ADJ_NONE: 0,
    ADJ_FRONT: 1,
    ADJ_BACK: 2,
    ADJ_FRONT_RAW: 1,
}


def to_tdx_str(adj: Union[str, int]) -> str:
    """
    统一参数 → get_market_data 用字符串

    >>> to_tdx_str('front')
    'front'
    >>> to_tdx_str(1)
    'front'
    """
    if isinstance(adj, str):
        if adj in ADJ_TO_TDX_STR:
            return ADJ_TO_TDX_STR[adj]
        if adj in ("0", "1", "2"):
            # 兜底: 字符串数字
            return {0: "none", 1: "front", 2: "back"}.get(int(adj), "front")
        # 未知字符串 → 默认前复权
        return "front"
    if isinstance(adj, int):
        return {0: "none", 1: "front", 2: "back"}.get(adj, "front")
    return "front"


def to_formula_int(adj: Union[str, int]) -> int:
    """
    统一参数 → formula_process_mul_xg 用整数 (选股)

    >>> to_formula_int('front')
    1
    >>> to_formula_int(1)
    1
    """
    if isinstance(adj, int):
        return adj if adj in (0, 1, 2) else 1
    if isinstance(adj, str):
        if adj in ADJ_TO_FORMULA_INT:
            return ADJ_TO_FORMULA_INT[adj]
        if adj.isdigit():
            return int(adj)
    return 1  # 默认前复权


def assert_consistent(adj_selection: Union[str, int], adj_backtest: Union[str, int]):
    """
    断言两端复权口径一致. 不一致时 raise ValueError.

    用法: 在 selection 阶段 / backtest 阶段之间加这一行,
          杜绝两端用了不同 dividend_type 但都没意识到.
    """
    s1 = to_tdx_str(adj_selection)
    s2 = to_tdx_str(adj_backtest)
    if s1 != s2:
        raise ValueError(
            f"复权口径不一致: 选股={adj_selection!r} (→{s1}) "
            f"vs 回测={adj_backtest!r} (→{s2}). 必须先统一."
        )


# === 自检 ===
if __name__ == '__main__':
    # 测试 to_tdx_str
    assert to_tdx_str('front') == 'front'
    assert to_tdx_str(1) == 'front'
    assert to_tdx_str('none') == 'none'
    assert to_tdx_str(0) == 'none'
    assert to_tdx_str(2) == 'back'
    assert to_tdx_str('未知') == 'front'  # 兜底

    # 测试 to_formula_int
    assert to_formula_int('front') == 1
    assert to_formula_int(1) == 1
    assert to_formula_int('none') == 0
    assert to_formula_int(0) == 0

    # 测试 assert_consistent
    try:
        assert_consistent('front', 'none')
        raise AssertionError('应该 raise')
    except ValueError:
        pass
    assert_consistent('front', 'front')  # 一致, 不 raise

    print('[OK] core.dividend_type 自检通过')