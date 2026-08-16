"""PreparedMatrix — run_cached 的预取矩阵束 (2026-08-16, P2-1 前门收敛)。

把 run_cached 的 7 个 K 线/能力矩阵打包成单一 frozen dataclass, 消除
「9 位置参数顺序是隐性契约、传错不报错」的陷阱, 并把 tradable_np 与
last_tradable_idx 的配对不变量从"runtime 只 warning"升级为"构造期 fail-fast"。

照 backtest/result.py 的 BacktestResult 先例放独立文件: engine.py 已 1095 行,
调用方 import 更轻, 且无 import 循环 (本模块只依赖 pandas/numpy/dataclasses)。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True, slots=True)
class PreparedMatrix:
    """run_cached 预取矩阵束。7 个 K 线/能力矩阵打包, 消除位置顺序陷阱 +
    配对不变量 (tradable_np 与 last_tradable_idx 恒成对存在于同一对象)。

    close / entries 必须是 DataFrame (engine 里 entries.values、close.index 都
    按 DataFrame 语义用); high_np/low_np/open_np/tradable_np/last_tradable_idx
    是 ndarray; 后三者可为 None (对应能力关闭)。
    """

    close: pd.DataFrame
    entries: pd.DataFrame
    high_np: np.ndarray
    low_np: np.ndarray
    open_np: np.ndarray | None = None        # 跳空保护能力, None=off
    tradable_np: np.ndarray | None = None    # 退市检测, 与 last_tradable_idx 成对
    last_tradable_idx: np.ndarray | None = None

    def __post_init__(self):
        # 陷阱 2 从"靠人背"变"靠类型约束": 配对不变量在构造期 fail-fast,
        # 而非 runtime 只 warning 不报错 (旧行为见 engine.py:735-737)。
        if (self.tradable_np is None) != (self.last_tradable_idx is None):
            raise ValueError("tradable_np 与 last_tradable_idx 必须成对出现")
