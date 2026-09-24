"""移动止损/止盈策略 (reason=4 loss / reason=8 profit)。

对齐 engine.py:206-209/231-234/251-254 的触发 + engine.py:304-311 的执行价。

语义（2026-07-05 v3 改造）: Low 触及回撤线即触发, 按回撤线价成交（盘中锁利）。
  trail_line = peak_hi * (1 - drawdown)
  reason = 8（移动止盈）if (trail_line-ep)/ep > 0 else 4（移动止损）
不做跳空保护: 始终按回撤线价（与 cost_stop 不同）。
"""

from __future__ import annotations

from typing import List

from ..state import Bar, Context, Position, PrefilterInputs
from .base import TriggerResult


class TrailingStrategy:
    """移动止损/止盈: peak_hi 涨过激活线后, Low 触及回撤线即触发。

    confirm 模式 (2026-08-04):
      - "intraday" (默认, 旧行为): 每根 bar 判定, Low 触线按线价成交。
        分钟级下 1% 回撤线落在日内噪音带内, 会被正常波动扫出 (2026-08-04
        1D/5M 对比实验: 同参数 1D +6.7% vs 5M -10.6%, 差距全部来自此)。
      - "low" (日频最低价确认): 只在当日最后一根 bar 判定, 用当日累计
        最低价对回撤线; 触发按当日收盘价成交。无未来信息, 可实盘落地。
      - "close" (日频收盘价确认): 同上, 但要求当日收盘价本身跌破线
        (下影线洗盘不触发, 动能还在就继续持有)。
      日频模式下当天阶梯止盈触发过新档位 → 当日 trailing 休息
      (复刻 1D stop_first "先到先得" 天然产生的阶梯挡枪效应)。
      - "simple" (2026-08-05 用户拍板, 实盘简化语义): 无阶梯休息规则。
        1D (bpday==1): 收盘价跌破线才触发, 按收盘价成交 (不看盘中下影线,
        天然免疫同bar高低顺序不可知的乐观偏差)。
        分钟级: 每根 bar Low 触线即触发, 但按该 bar 收盘价成交
        (不用偏乐观的线价)。
      - "real" (2026-08-05 用户拍板, 条件单语义, 1D/5M 同一套): 无阶梯休息。
        线只认"提前挂好的": 本 bar 自己创峰值 → 本 bar 不判定
        (同bar高低顺序不可知, 不拿新线审判本 bar 的旧低点);
        峰值来自更早 bar 时: 开盘已在线下(跳空) → 按开盘价成交;
        盘中 Low 触线 → 按线价成交 (隔夜/前 bar 已挂好的条件单,
        实盘可达)。1D 下代价 = 峰值日多观察一天。
        已知代价 (2026-08-06 审计):
        - 平峰: 本 bar 最高价恰好等于历史峰值 (最小变动单位下不罕见)
          也按"创峰值"跳过 —— 方向保守 (晚卖一根 bar), 可接受。
        - open 缺失 (open_np=None → bar.open=NaN): 跳空分支不命中,
          跳空 bar 退化为按线价成交 (偏乐观), 与既有 gap_protection 口径一致。

    gap_protection (2026-07-21 用户拍板, opt-in 默认关): 跳空低开直接跌穿回撤线时
    按 min(回撤线, 开盘价) 成交——实盘条件单开盘触发只能拿到开盘价, 原语义(始终按
    回撤线价)在 gap bar 上系统性高估成交价。对齐 cost_stop 的跳空保护思路。
    (仅 intraday 模式有意义; 日频模式按收盘价成交, 无此问题)
    """

    name = "trailing"

    def __init__(self, activation: float, drawdown: float,
                 gap_protection: bool = False, confirm: str = "intraday"):
        self.activation = float(activation)
        self.drawdown = float(drawdown)
        self.gap_protection = bool(gap_protection)
        if confirm not in ("intraday", "low", "close", "simple", "real"):
            raise ValueError(f"trailing confirm 非法: {confirm!r} (合法: intraday/low/close/simple/real)")
        self.confirm = confirm

    def prefilter(self, x: PrefilterInputs) -> bool:
        """预筛: 涨过激活线后, 盘中/简单/条件单模式看 Low 触线, 日频模式看当日末根 bar。"""
        if x.peak_hi_profit < self.activation:
            return False
        if self.confirm in ("intraday", "simple", "real"):
            return x.lo <= x.peak_hi * (1.0 - self.drawdown)
        # low/close 日频确认: 只在当日末根 bar 可能触发
        return (x.i % x.bpday) == x.bpday - 1

    def check(self, pos: Position, bar: Bar, ctx: Context) -> List[TriggerResult]:
        if ctx.peak_hi_profit < self.activation:
            return []
        if self.confirm == "simple":
            return self._check_simple(pos, bar, ctx)
        if self.confirm == "real":
            return self._check_real(pos, bar, ctx)
        if self.confirm != "intraday":
            return self._check_daily(pos, bar, ctx)
        trail_line = ctx.peak_hi * (1.0 - self.drawdown)
        if bar.low <= trail_line:
            ep = pos.entry_px
            # M3: 防御 ep==0 除零（上游已挡, 此处独立守卫）
            reason = 8 if (ep > 0.0 and (trail_line - ep) / ep > 0.0) else 4
            exec_px = trail_line
            if self.gap_protection and ctx.peak_hi > bar.high and bar.open < trail_line:
                # 隔夜跳空跌穿"已被更早 bar 激活"的回撤线: 实盘只能按开盘价成交。
                # 判别依据: peak_hi 来自更早 bar(> 本 bar high), 且开盘价已低于线。
                # 若本 bar 自己创出峰值(peak_hi == bar.high), 属同 bar 顺序不可知,
                # 保持原线价语义(那是另一个已知乐观源, 不属跳空保护范围)。
                exec_px = bar.open
            return [TriggerResult(
                reason=reason, strategy_name=self.name, execution_price=exec_px,
            )]
        return []

    def _check_simple(self, pos: Position, bar: Bar, ctx: Context) -> List[TriggerResult]:
        """实盘简化语义: 无阶梯休息, 按收盘价成交。

        1D: 收盘价跌破线才触发 (不看盘中下影线)。
        分钟级: 每根 bar Low 触线即触发, 按该 bar 收盘价成交。
        """
        trail_line = ctx.peak_hi * (1.0 - self.drawdown)
        if ctx.bpday == 1:
            touched = bar.close <= trail_line
        else:
            touched = bar.low <= trail_line
        if not touched:
            return []
        ep = pos.entry_px
        exec_px = bar.close
        reason = 8 if (ep > 0.0 and (exec_px - ep) / ep > 0.0) else 4
        return [TriggerResult(
            reason=reason, strategy_name=self.name, execution_price=exec_px,
        )]

    def _check_real(self, pos: Position, bar: Bar, ctx: Context) -> List[TriggerResult]:
        """条件单语义: 线必须"提前挂好", 1D/5M 同一套, 无阶梯休息。

        - 本 bar 自己创峰值 (peak_hi == bar.high): 跳过。同 bar 内高低顺序
          不可知, 新线在这根 bar 走完前不存在, 不能拿它审判本 bar 的低点。
        - 峰值来自更早 bar:
          · 跳空 (open < 线): 按开盘价成交 (集合竞价/开盘出, 拿不到线价)
          · Low 触线: 按线价成交 (提前挂好的条件单, 实盘可达)
        1D 下代价: 峰值日多观察一天, 卖出延后一天。
        """
        trail_line = ctx.peak_hi * (1.0 - self.drawdown)
        if ctx.peak_hi == bar.high:
            return []  # 本 bar 创峰值, 线刚生成, 下一根再判
        if bar.open < trail_line:
            exec_px = bar.open
        elif bar.low <= trail_line:
            exec_px = trail_line
        else:
            return []
        ep = pos.entry_px
        reason = 8 if (ep > 0.0 and (exec_px - ep) / ep > 0.0) else 4
        return [TriggerResult(
            reason=reason, strategy_name=self.name, execution_price=exec_px,
        )]

    def _check_daily(self, pos: Position, bar: Bar, ctx: Context) -> List[TriggerResult]:
        """日频确认: 只在当日末根 bar 判定, 触发按收盘价成交。"""
        if not ctx.is_last_bar:
            return []
        if ctx.ladder_fired_today:
            return []  # 阶梯值班日, trailing 休息
        trail_line = ctx.peak_hi * (1.0 - self.drawdown)
        if self.confirm == "low":
            touched = ctx.day_lo <= trail_line
        else:  # "close"
            touched = bar.close <= trail_line
        if not touched:
            return []
        ep = pos.entry_px
        exec_px = bar.close
        reason = 8 if (ep > 0.0 and (exec_px - ep) / ep > 0.0) else 4
        return [TriggerResult(
            reason=reason, strategy_name=self.name, execution_price=exec_px,
        )]
