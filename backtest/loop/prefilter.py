"""TriggerPreFilter — 卖出侧触发预筛 (Phase 3, 2026-07-18)。

跳过"本 bar 数学上不可能有任何触发"的持仓评估 (实测空跑率 84.7%)。
推导与零假阴性论证: docs/audit/2026-07-18_Phase3预筛条件推导.md。

设计 (2026-08-16 P0-1 派生重构):
- 预筛条件从策略对象派生: 每个策略暴露 prefilter(x) 谓词 (标量束 PrefilterInputs),
  prefilter 遍历 dispatcher.strategies 统一调用, 不再手抄每策略 if 分支。
- 条件即触发本身或更宽(保守充分条件), 任一策略拿不准 → 返回 True 走全路径。
- fail-open: 策略缺 prefilter 方法 (未来新增策略忘了补) → 恒 True, 防静默漏卖
  (旧版手抄 if 分支, 新策略漏补 = could_trigger 返 False = 该策略永不触发)。
- 只引用策略的只读参数(threshold/activation/profits 等), 不碰任何可变状态。
- 禁用策略(capability gating 未进 dispatcher)不参与判定。
"""

from __future__ import annotations

from typing import Sequence

from .state import PrefilterInputs


class TriggerPreFilter:
    """从 dispatcher 策略 + absolutes 构造, could_trigger() 纯标量短路。"""

    def __init__(self, dispatcher, absolutes: Sequence):
        # 保留 dispatcher 的构造顺序 (builder 按 cost/ladder/trailing/time/cond/first/atr
        # 插入), 只存策略对象列表; 各策略的 prefilter 谓词内聚在策略自身。
        self._strategies = list(dispatcher.strategies.values())
        self._formula = next(
            (a for a in absolutes if getattr(a, "name", None) == "formula_sell"), None)

    def could_trigger(self, *, ci: int, i: int, ep: float,
                      hi: float, lo: float, hi_pp: float, lo_pp: float,
                      peak_hi: float, peak_hi_profit: float,
                      hold_days: int, entry_idx: int, bpday: int,
                      ladder_done: int,
                      ladder_profits, n_ladder: int) -> bool:
        """True=可能触发(走全路径); False=数学上不可能触发(可安全跳过)。"""
        x = PrefilterInputs(
            ci=ci, i=i, ep=ep, hi=hi, lo=lo, hi_pp=hi_pp, lo_pp=lo_pp,
            peak_hi=peak_hi, peak_hi_profit=peak_hi_profit,
            hold_days=hold_days, entry_idx=entry_idx, bpday=bpday,
            ladder_done=ladder_done, ladder_profits=ladder_profits,
            n_ladder=n_ladder,
        )
        # ── formula_sell (绝对优先) ──
        f = self._formula
        if f is not None:
            if self._eval(f, x):
                return True
        # ── dispatcher 退出策略 ──
        for s in self._strategies:
            if self._eval(s, x):
                return True
        return False

    @staticmethod
    def _eval(strategy, x: PrefilterInputs) -> bool:
        """调用策略的 prefilter 谓词; 无 prefilter → fail-open 恒 True。"""
        pf = getattr(strategy, "prefilter", None)
        if pf is None:
            # 未来新增策略忘了补 prefilter: 宁可全路径慢, 不可静默漏卖。
            return True
        return bool(pf(x))
