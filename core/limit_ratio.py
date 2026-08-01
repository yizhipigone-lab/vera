"""core/limit_ratio.py — 涨停幅度规则，全项目唯一真相源。

2026-08-01 P1: 合并 trade/executor.py:limit_ratio 与
backtest/engine.py:_limit_ratio_vector —— 两处规则同语义但实现独立、
ST 标记来源已漂移 (回测用 TDX IsSTGP 真实标记, 实盘靠外部传 bool),
此处统一为单一函数 + 批量向量化变体。
"""

from __future__ import annotations

import numpy as np


def limit_ratio(code: str, st: bool = False) -> float:
    """单票涨停幅度: 主板 10% / 创业 300·301 与科创 688 20% /
    北交所 (4/8/920 开头) 30% / ST 5%。

    与 TDX 规则对齐: 普通 A 股前日收盘价 ±10%、科创/创业 ±20%、
    北交所 ±30%、ST ±5%。
    """
    if st:
        return 0.05
    num = code.split(".")[0]
    if num.startswith(("688", "300", "301")):
        return 0.20
    if num.startswith(("4", "8", "920")):
        return 0.30
    return 0.10


def limit_ratio_vector(
    codes: np.ndarray, st_flags: np.ndarray | None = None
) -> np.ndarray:
    """列向量批量涨停幅度 (回测引擎用)。
    codes: (n_stocks,) 字符串数组; st_flags: (n_stocks,) bool 数组或 None。
    返回 (n_stocks,) float64 数组。
    """
    n = len(codes)
    ratios = np.full(n, 0.10, dtype=np.float64)
    for i in range(n):
        code = str(codes[i])
        is_st = bool(st_flags[i]) if st_flags is not None else False
        ratios[i] = limit_ratio(code, is_st)
    return ratios
