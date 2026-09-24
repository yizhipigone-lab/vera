"""FormulaSellStrategy — 公式卖出绝对优先级 (reason=12)。

候选 A 阶段 2 — stage 2。实现 AbsoluteStrategy 协议, 先于 ExitDispatcher 评估。

对齐 engine.py:24（"formula_sell (reason=12) 始终最高优先级, 不受 priority 开关影响"）
+ engine.py:177-181 触发条件 + engine.py:411-434 执行（按 formula_exit_ratio 部分卖）。

触发: formula_exit_np[i - lag_bars, ci] 为 True
执行价: 当根 Close
卖出比例: formula_exit_ratio（<1.0 为部分卖）
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np

from .state import Bar, Context, Position, PrefilterInputs
from .strategies.base import TriggerResult


class FormulaSellStrategy:
    """公式卖出: TDX 信号驱动的绝对优先级卖出。"""

    name = "formula_sell"

    def __init__(self, formula_exit_np: Optional[np.ndarray],
                 ratio: float = 1.0, lag_bars: int = 1):
        self.signal = formula_exit_np   # (n_dates, n_stocks) bool ndarray, 可为 None
        self.ratio = float(ratio)
        self.lag_bars = int(lag_bars)

    def prefilter(self, x: PrefilterInputs) -> bool:
        """预筛: 公式信号在 (i-lag_bars, ci) 命中即触发 (条件即触发本身)。"""
        if self.signal is None:
            return False
        if x.i < self.lag_bars:
            return False
        if not (0 <= x.ci < self.signal.shape[1]):
            return False
        return bool(self.signal[x.i - self.lag_bars, x.ci])

    def check(self, pos: Position, bar: Bar,
              ctx: Context) -> List[TriggerResult]:
        if self.signal is None:
            return []
        i = ctx.bar_index
        if i < self.lag_bars:
            return []
        ci = ctx.ci
        if not (0 <= ci < self.signal.shape[1]):
            return []
        if not bool(self.signal[i - self.lag_bars, ci]):
            return []
        return [TriggerResult(
            reason=12, strategy_name=self.name, execution_price=bar.close,
            sell_ratio=self.ratio, is_partial=(self.ratio < 1.0),
        )]
