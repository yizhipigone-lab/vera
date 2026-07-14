"""阶梯止盈策略 (reason=5)。

薄壳, 转调 backtest.ladder_tp 的两个纯函数。
对齐 engine.py:192-204/218-227/240-249 的触发 + engine.py:291-303 的执行价。

注意（HA4 例外）: 本策略 mutate pos.ladder_done bitmask, 与 engine.py:196 原地
mutation 一致。dispatcher 据返回值的 is_partial/sell_ratio 决定是否阻塞后续策略
（trailing_first 下部分卖不阻塞, 见 exit_engine.py）。
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
        profits = ctx.ladder_profits[: ctx.n_ladder]
        new_mask = compute_ladder_trigger(prev_mask, ctx.hi_pp, profits)
        if new_mask == prev_mask:
            return []
        # mutate bitmask（跨 bar 累计, 与 engine.py:196 一致）
        pos.ladder_done = new_mask
        ratios = ctx.ladder_ratios[: ctx.n_ladder]
        sell_ratio = float(compute_ladder_sell_ratio(prev_mask, new_mask, profits, ratios))
        # 执行价: 取已置位且 hi_pp 满足的最大档位 profit（engine.py:296-302）
        tp_profit = 0.0
        for li in range(ctx.n_ladder):
            if (new_mask >> li) & 1 and ctx.hi_pp >= profits[li]:
                if profits[li] > tp_profit:
                    tp_profit = profits[li]
        exec_price = float(pos.entry_px * (1.0 + tp_profit))
        return [TriggerResult(
            reason=5, strategy_name=self.name, execution_price=exec_price,
            sell_ratio=sell_ratio, is_partial=bool(sell_ratio < 1.0),
        )]
