"""ExitDispatcher — 止损止盈调度器。

候选 A 阶段 2 — stage 2。把 _simulate_core_v3 的 3 套优先级 if 链
（engine.py:188-273）抽成 dispatcher.evaluate() -> List[TriggerResult]。

核心设计（v3 计划书 §2.1 CA1）:
- 多结果模型: trailing_first 下同 bar 可返回 2 个触发
  （ladder 部分卖 + trailing/cost_stop 全卖剩余）, 精确复刻 engine.py:346-384。
- capability gating（HA1）: 禁用的策略根本不进 strategies dict。
- 排序由 Priority 枚举 + 内部顺序表编码, 策略自身无 priority 字段。
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, List

from .strategies.base import ExitStrategy, TriggerResult
from .state import Bar, Context, Position


class Priority(Enum):
    """三档优先级（engine.py:183-187）。"""

    STOP_FIRST = "stop_first"              # cost_stop > ladder_tp > trailing
    LADDER_TP_FIRST = "ladder_tp_first"    # ladder_tp > cost_stop > trailing
    TRAILING_FIRST = "trailing_first"      # ladder_tp(部分卖不阻塞) > trailing > cost_stop


# 先于 ExitDispatcher 评估的策略名（engine.py:24, formula_sell 始终最高优先级）
PRE_DISPATCH_STRATEGIES = {"formula_sell"}


# priority-block 求值顺序（first-trigger-wins）
# atr_stop 为新 API 策略, 默认不在 dict（builder 未启用则不进 strategies, 无影响）
#
# 设计说明 — ATR 在不同 priority 下的位置语义:
#   STOP_FIRST:       cost_stop > atr_stop > ladder_tp > trailing
#     (ATR 是波动率止损，排在硬止损之后、止盈之前)
#   LADDER_TP_FIRST:  ladder_tp > cost_stop > atr_stop > trailing
#     (止盈优先，ATR 退到成本止损之后)
#   TRAILING_FIRST:   单独双触发语义（不在本表），ATR 排在 trailing > cost_stop 之后
#     (ladder 部分卖不阻塞 → trailing 全卖剩余 或 cost_stop 兜底 → ATR 最末)
#   三种 priority 下 ATR 求值顺序不同是刻意设计：波动率止损的"止损"属性使其
#   始终排在成本止损附近（同属止损族），具体前/后取决于该 priority 下止损 vs 止盈的优先级。
_PRIORITY_ORDER: Dict[Priority, List[str]] = {
    Priority.STOP_FIRST: ["cost_stop", "atr_stop", "ladder_tp", "trailing"],
    Priority.LADDER_TP_FIRST: ["ladder_tp", "cost_stop", "atr_stop", "trailing"],
    # trailing_first 单独处理（双触发语义）
}

# 公共尾部求值顺序（engine.py:258-273, 各 priority block 之后）
_TAIL_ORDER = ["time_stop", "cond_time", "first_day"]


class ExitDispatcher:
    """持有已启用的 ExitStrategy（按名索引）, 按 Priority 求触发。

    Args:
        strategies: {name: ExitStrategy} 只含已启用策略（capability 过滤后）。
            合法 name: cost_stop / ladder_tp / trailing / time_stop / cond_time / first_day。
        priority: 三档优先级之一。

    2026-07-18 审计 F3: **构造后不可变**。求值序列 (_order/_tail/_ladder 等)
    在 __init__ 一次性快照, 构造后增删 self.strategies 不会生效且不会报错
    (新策略永不求值, 删掉的继续触发)。要改策略集合请重建 ExitDispatcher。
    """

    def __init__(self, strategies: Dict[str, ExitStrategy], priority: Priority):
        self.strategies = strategies
        self.priority = priority
        # 2026-07-17 Phase 1 项3c: 预建求值序列, evaluate 热路径不再 dict.get / enum 比较。
        # 求值顺序与 _PRIORITY_ORDER/_TAIL_ORDER/_eval_trailing_first 完全一致,
        # 双触发语义在 ladder_partial 传递链里, 与查找方式无关。self.strategies 保留公开。
        self._order = [strategies[n] for n in _PRIORITY_ORDER.get(priority, []) if n in strategies]
        self._tail = [strategies[n] for n in _TAIL_ORDER if n in strategies]
        self._ladder = strategies.get("ladder_tp")
        self._trailing = strategies.get("trailing")
        self._cost = strategies.get("cost_stop")
        self._atr = strategies.get("atr_stop")
        self._is_trailing_first = (priority == Priority.TRAILING_FIRST)

    def evaluate(self, pos: Position, bar: Bar,
                 ctx: Context) -> List[TriggerResult]:
        """按 priority 求触发。

        - stop_first / ladder_tp_first: first-trigger-wins, 至多 1 个结果
        - trailing_first: ladder 部分卖不阻塞, trailing/cost_stop 可追加 → 至多 2 个
        - 任一 priority block 无触发则走公共尾部（time_stop/cond_time/first_day）
        """
        if self._is_trailing_first:
            return self._eval_trailing_first(pos, bar, ctx)
        for s in self._order:
            r = s.check(pos, bar, ctx)
            if r:
                return [r[0]]
        return self._eval_tail(pos, bar, ctx)

    def _eval_trailing_first(self, pos: Position, bar: Bar,
                             ctx: Context) -> List[TriggerResult]:
        # 1. ladder（部分卖不阻塞, 全卖阻塞）
        ladder_partial = None
        ladder = self._ladder
        if ladder is not None:
            r = ladder.check(pos, bar, ctx)
            if r:
                if r[0].is_partial:
                    ladder_partial = r[0]      # 部分卖, 继续检查 trailing
                else:
                    return [r[0]]              # 全卖, 阻塞
        # 2. trailing（ladder 部分卖或未触发时检查; ladder 全卖已早退）
        trailing = self._trailing
        if trailing is not None:
            r = trailing.check(pos, bar, ctx)
            if r:
                if ladder_partial is not None:
                    return [ladder_partial, r[0]]   # 双触发: ladder 部分卖 + trailing 全卖剩余
                return [r[0]]
        # 3. cost_stop 兜底（trailing 没触发才检查）
        cost = self._cost
        if cost is not None:
            r = cost.check(pos, bar, ctx)
            if r:
                if ladder_partial is not None:
                    return [ladder_partial, r[0]]   # 双触发: ladder 部分卖 + cost_stop 全卖剩余
                return [r[0]]
        # 3b. atr_stop 兜底（cost_stop 没触发才检查, 同款兜底语义）
        atr = self._atr
        if atr is not None:
            r = atr.check(pos, bar, ctx)
            if r:
                if ladder_partial is not None:
                    return [ladder_partial, r[0]]   # 双触发: ladder 部分卖 + atr_stop 全卖剩余
                return [r[0]]
        # 4. trailing/cost/atr 都没触发, 但 ladder 部分卖了 → ladder 是唯一触发
        if ladder_partial is not None:
            return [ladder_partial]
        # 5. 公共尾部
        return self._eval_tail(pos, bar, ctx)

    def _eval_tail(self, pos: Position, bar: Bar,
                   ctx: Context) -> List[TriggerResult]:
        for s in self._tail:
            r = s.check(pos, bar, ctx)
            if r:
                return [r[0]]
        return []
