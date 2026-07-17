"""BacktestResult — 回测结果的 typed 返回（C3）。

把 engine.run()/run_cached() 原 plain dict 返回改成 frozen dataclass,
对齐 pipeline.PipelineResult 模式。

向后兼容（规则3）: 实现 dict-like 访问且**精确复刻 dict 语义** ——
只有构造时显式传入的字段才算 `in result` / 出现在 keys()。
未传字段用 _UNSET 哨兵, get 返回 default, __getitem__ 抛 KeyError。
这样 run() 与 run_cached() 各自的 key 集合与老 dict 完全一致。

新增 .field 访问（result.equity_curve）。
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Iterator, List, Optional, Tuple

import numpy as np
import pandas as pd

_UNSET = object()  # 哨兵: 字段未设置（等价于老 dict 没有这个 key）


@dataclass(frozen=True)
class BacktestResult:
    """回测结果。frozen; dict-like 兼容老代码（精确 dict 语义）。

    字段覆盖 run() 与 run_cached() 两个入口的全部 key:
      run()        → equity_curve, trades, metrics, stop_config_summary, selections, stock_count
      run_cached() → metrics, trades, cumulative_return, equity_curve (+ raw_equity/raw_trades if return_raw)

    ⚠️ _UNSET 语义: 未构造时传入的字段值为 _UNSET 哨兵（不是 None）。
    访问未设置字段务必用 result.get("field", default)（返回 default）或 result["field"]
    （抛 KeyError）——**不要直接 .field 访问未设置字段**, 会拿到 _UNSET 哨兵对象。
    .field 仅用于已知设置的字段（如 run() 结果的 result.metrics）。
    """

    equity_curve: Any = _UNSET
    trades: Any = _UNSET
    metrics: Any = _UNSET
    stop_config_summary: Any = _UNSET
    selections: Any = _UNSET
    stock_count: Any = _UNSET
    cumulative_return: Any = _UNSET
    raw_equity: Any = _UNSET
    raw_trades: Any = _UNSET
    # 2026-07-18: 5m 降级报告 (degrade_5m=True 且 run() 路径才设置; 计划书 §4.7)
    degradation: Any = _UNSET

    # ── dict-like 兼容（精确复刻 dict 语义: 只有 set 的字段才算 in）──
    def _all_field_names(self) -> List[str]:
        return [f.name for f in fields(self)]

    def _is_set(self, key: str) -> bool:
        return key in self._all_field_names() and getattr(self, key) is not _UNSET

    def __getitem__(self, key: str) -> Any:
        if self._is_set(key):
            return getattr(self, key)
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        if self._is_set(key):
            return getattr(self, key)
        return default

    def __contains__(self, key: object) -> bool:
        return self._is_set(str(key)) if isinstance(key, str) else False

    def keys(self) -> List[str]:
        return [n for n in self._all_field_names() if getattr(self, n) is not _UNSET]

    def items(self) -> Iterator[Tuple[str, Any]]:
        for name in self._all_field_names():
            v = getattr(self, name)
            if v is not _UNSET:
                yield name, v

    def to_dict(self) -> dict:
        """显式转 dict（需要真 dict 的场景, 如 JSON 序列化）。"""
        return {name: getattr(self, name) for name in self.keys()}
