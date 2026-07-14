"""ExitStrategy Protocol + TriggerResult dataclass.

候选 A 阶段 2 — 核心循环拆解的 Strategy 模式基类。

设计要点（v3 计划书 §2.1）:
- TriggerResult 折叠 execution_price（CA4）: 执行价在 check() 内算好填入,
  避免"检测"与"执行价计算"分离导致数据耦合。
- ExitStrategy.check() 返回 List[TriggerResult]（CA1）: 支持 trailing_first
  同 bar 双触发（ladder 部分卖 + trailing/cost_stop 全卖剩余）。
  单个 strategy 通常返回 0 或 1 个; dispatcher 在 trailing_first 下追加第二个。
- 无 enabled 字段（HA1）: capability gating 在 dispatcher 构造时过滤,
  禁用的 strategy 根本不进 dispatcher。
- 无 priority 字段: 排序由 dispatcher 按构造时 list 顺序 + Priority 枚举编码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class TriggerResult:
    """单次触发结果（frozen，不可变）。

    Attributes:
        reason: 退出原因码 3/4/5/6/7/8/9/10/12（见 state.TradeColumns / engine.py:89）。
        strategy_name: 策略名（用于日志，不用于排序），如 "cost_stop" / "ladder_tp"。
        execution_price: 已算好的成交价（含跳空保护等逻辑，check() 内算完填入）。
        sell_ratio: 卖出比例。1.0=全卖; <1.0=部分卖（ladder 阶梯止盈）。
        is_partial: 是否部分卖（trailing_first 双触发用，ladder 部分卖后保留仓位）。
        actual_return: 精确收益率（可选）。ladder 用 tp_profit 原值（engine.py:303）,
            其余策略不填（None → loop 用 (exec_price-ep)/ep 重算, 对齐 engine.py:290/311/315）。
            仅 _execute_single 用; _execute_dual 的 ladder 部分卖走重算（engine.py:377）。
    """

    reason: int
    strategy_name: str
    execution_price: float
    sell_ratio: float = 1.0
    is_partial: bool = False
    actual_return: Optional[float] = None


@runtime_checkable
class ExitStrategy(Protocol):
    """Strategy 模式: 单一止损类型的检测 + 执行价计算。

    命名上沿袭 VERA 团队习惯叫 adapter, 实际为 Strategy pattern。

    策略只读 pos / bar / ctx, 返回 TriggerResult 列表。
    例外: ladder_tp 策略可 mutate pos.ladder_done bitmask（跨 bar 累计状态,
    与 engine.py:196 原地 mutation 一致）。其余 Position 字段（high_px/high_hi）
    由 BacktestLoop 统一 mutate。
    """

    name: str

    def check(self, pos: "Position", bar: "Bar", ctx: "Context") -> List[TriggerResult]:
        """返回 0/1 个触发。trailing_first 下的第二个触发由 dispatcher 追加。"""
        ...


@runtime_checkable
class AbsoluteStrategy(Protocol):
    """最高优先级短路 — 先于 ExitDispatcher 评估。

    VERA 当前唯一实例: formula_sell (reason=12, engine.py:24 明确):
    "formula_sell (reason=12) 始终最高优先级, 不受 priority 开关影响"
    """

    name: str

    def check(self, pos: "Position", bar: "Bar", ctx: "Context") -> List[TriggerResult]:
        ...
