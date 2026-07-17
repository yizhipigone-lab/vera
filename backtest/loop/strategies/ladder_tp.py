"""阶梯止盈策略 (reason=5)。

薄壳, 转调 backtest.ladder_tp 的两个纯函数。
对齐 engine.py:192-204/218-227/240-249 的触发 + engine.py:291-303 的执行价。

设计选择 — pos.ladder_done 就地修改:
  check() 在当前 bar 内 mutate pos.ladder_done bitmask, 然后 loop 在 evaluate 之后
  通过 book.set_ladder_done() 写回 PositionBook。这样做的原因是 ladder_done 是跨 bar
  累计状态（一旦某档触发就永久置位），check() 返回的新 mask 需要被本次 bar 的后续策略
  （如同 bar 的 trailing/cost_stop）看到更新后的状态。
  风险: 如果在 check() 和 set_ladder_done() 之间有人读 pos.ladder_done，会看到已修改值。
  更干净的设计是 check() 返回 new_ladder_done 不 mutate pos（见审计 F-H3），
  但当前与 engine.py:196 原地 mutation 范式保持一致。
"""

from __future__ import annotations

from typing import List

from backtest.ladder_tp import compute_ladder_trigger, compute_ladder_sell_ratio

from .base import ExitStrategy, TriggerResult
from ..state import Bar, Context, Position


class LadderTpStrategy:
    """阶梯止盈: 当根 High 涨破新档位即部分/全卖。

    触发: compute_ladder_trigger 得到 new_mask != pos.ladder_done
    执行价: ep*(1+tp_profit), tp_profit = max(已置位且 hi_pp>=profit 的档位 profit)
    卖出比例: compute_ladder_sell_ratio（<1.0 为部分卖）
    """

    name = "ladder_tp"

    def check(self, pos: Position, bar: Bar, ctx: Context) -> List[TriggerResult]:
        prev_mask = pos.ladder_done
        # H5/R9: .copy() 取只读副本, 防误写污染跨 bar 的 ladder_profits 视图
        # H5/R9 原注释: 防误写。已核验 compute_ladder_trigger/sell_ratio 对输入纯只读
        # (backtest/ladder_tp.py:44-50/:81-91), 2026-07-17 Phase 1 去 copy (每 check 省一次数组拷贝)
        profits = ctx.ladder_profits[: ctx.n_ladder]
        new_mask = compute_ladder_trigger(prev_mask, ctx.hi_pp, profits)
        if new_mask == prev_mask:
            return []
        # mutate bitmask（跨 bar 累计, 与 engine.py:196 一致）
        pos.ladder_done = new_mask
        ratios = ctx.ladder_ratios[: ctx.n_ladder]  # 只读视图, 同上
        sell_ratio = compute_ladder_sell_ratio(prev_mask, new_mask, profits, ratios)
        # 执行价: 取已置位且 hi_pp 满足的最大档位 profit（engine.py:296-302）
        tp_profit = 0.0
        for li in range(ctx.n_ladder):
            if (new_mask >> li) & 1 and ctx.hi_pp >= profits[li]:
                if profits[li] > tp_profit:
                    tp_profit = profits[li]
        # 保留 np.float64 不 cast, 保证与 legacy 裸数组算术字节级一致
        exec_price = pos.entry_px * (1.0 + tp_profit)
        return [TriggerResult(
            reason=5, strategy_name=self.name, execution_price=exec_price,
            sell_ratio=sell_ratio, is_partial=bool(sell_ratio < 1.0),
            actual_return=tp_profit,
        )]
