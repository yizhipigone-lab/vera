"""入场信号预计算 helper — 生产与测试共用同一实现。

2026-07-18 审计 F1 修复: 抽出独立函数前, 该 idiom 内联在 BacktestLoop.run,
测试只能复制粘贴一份(脱钩风险)。cuts 必须用 count_nonzero(按个数)而非
sum(按取值) — 否则非二值矩阵(如 int 2)切段错位, 信号被挪到更早的 bar,
即静默前视。count_nonzero 与 legacy 的 truthy 语义完全一致。
"""

from __future__ import annotations

import numpy as np


def precompute_signal_lists(entry_np: np.ndarray) -> list:
    """把 (n_dates, n_stocks) 信号矩阵切成每 bar 的信号列索引列表。

    返回 list[ndarray], 长度恒等于 n_dates:
    - np.nonzero 行主序 → 每 bar 内列升序(与旧 range(n_stocks) 扫描顺序一致)
    - 空 bar(首/中/尾) 得空段
    - 任意 truthy 值(非 0/False/NaN... 注意 NaN 也是 truthy)都算信号,
      与 legacy `if not entry_np[i, ci]` 语义一致
    """
    sig_cols = np.nonzero(entry_np)[1]
    cuts = np.cumsum(np.count_nonzero(entry_np, axis=1))[:-1]
    return np.split(sig_cols, cuts)
