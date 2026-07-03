"""
阶梯止盈（ladder take-profit）纯函数。

从 backtest/engine.py::_simulate_core_v3 抽离的纯逻辑，便于单元测试。

两个核心函数：
  - compute_ladder_trigger: 判断同 bar 内有多少新触发的档位，返回新 mask
  - compute_ladder_sell_ratio: 计算本次卖出比例（仅算"新触发"档位，钳位到 [0,1]）

设计说明（分批止盈语义）：
  - 每档触发后产生一次独立的卖出动作
  - sell_ratio 只算"本次新触发"的档位比例，不重复算过去已触发的档位
  - 同 bar 多档满足时（如跳空高开），合并为一次大额卖出（累加新触发档位比例）
  - 钳位到 1.0 防止多档累加导致超卖
"""
from __future__ import annotations

from typing import Sequence


def compute_ladder_trigger(
    done_mask: int,
    hi_profit: float,
    profits: Sequence[float],
) -> int:
    """
    计算阶梯止盈的新触发 mask。

    同 bar 内所有未触发且 hi_profit >= profit[i] 的档位都置位。
    若没有任何新触发，返回值等于 done_mask（调用方据此判断是否触发）。

    修正历史（BUG-5）：
      - 旧实现命中即 break，导致同 bar 多档只置位一档
      - 新实现循环置位所有满足的档位

    Args:
        done_mask: 已触发的档位 bitmask，第 i 位 = 1 表示第 i 档已触发
        hi_profit: 当前 bar 的最高价相对入场价的涨幅（如 0.08 = 8%）
        profits: 阶梯档位的盈利阈值（如 [0.06, 0.15]）

    Returns:
        新的 done_mask
    """
    new_mask = done_mask
    for li, profit in enumerate(profits):
        if (new_mask >> li) & 1:
            continue
        if hi_profit >= profit:
            new_mask |= (1 << li)
    return new_mask


def compute_ladder_sell_ratio(
    prev_mask: int,
    new_mask: int,
    profits: Sequence[float],
    ratios: Sequence[float],
) -> float:
    """
    计算本次卖出的比例（累加"本 bar 新触发"档位的 sell_ratio）。

    关键：sell_ratio 只算"本次新触发"的档位比例（new_mask 中新置位的位），
    不包括"过去已触发"的档位。这样：
      - 同 bar 多档触发 → 累加（合并卖出）
      - 不同 bar 各档触发 → 各自独立卖（分批）
      - 累计不会因为"老档位还在 done_mask 里"而重复计算

    修正历史（BUG-5）：
      - 旧实现 sell_ratio = ladder_ratios[li] 每次覆盖，最终只取最后一档
      - 进一步 bug：旧实现把所有"已触发且 hi_pp 满足"都算上，导致老档位重复计入

    Args:
        prev_mask: 本 bar 处理前的 done_mask
        new_mask: 本 bar 处理后的 done_mask（含本 bar 新触发的位）
        profits: 阶梯档位的盈利阈值
        ratios: 阶梯档位的卖出比例

    Returns:
        sell_ratio ∈ [0.0, 1.0]
    """
    sell_ratio = 0.0
    for li in range(len(profits)):
        # 只算"本 bar 新触发"的位
        was_set = (prev_mask >> li) & 1
        is_set = (new_mask >> li) & 1
        if is_set and not was_set:
            sell_ratio += ratios[li]
    # 防止多档 sell_ratio 合计 > 1.0 导致超卖
    if sell_ratio > 1.0:
        sell_ratio = 1.0
    return sell_ratio
