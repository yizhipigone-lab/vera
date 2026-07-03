"""
阶梯止盈（ladder take-profit）纯函数测试

按 TDD 流程：先写测试，验证 RED（fail），再实现。

被测函数（位于 backtest/ladder_tp.py）：
  - compute_ladder_trigger(done_mask, hi_profit, profits) -> new_mask
       返回新置位后的 done_mask；若没有新触发则等于 done_mask。
  - compute_ladder_sell_ratio(prev_mask, new_mask, profits, ratios) -> float
       累加"本 bar 新触发"档位的 sell_ratio（不含老档位），钳位到 [0.0, 1.0]。
"""
from __future__ import annotations

import pytest

from backtest.ladder_tp import (
    compute_ladder_trigger,
    compute_ladder_sell_ratio,
)


# 阶梯: 6% 卖 30%, 15% 卖 30%  (两档)
TWO_LVL_PROFITS = (0.06, 0.15)
TWO_LVL_RATIOS = (0.30, 0.30)

# 阶梯: 6%/15%/25% 卖 30%/30%/40%  (三档)
THREE_LVL_PROFITS = (0.06, 0.15, 0.25)
THREE_LVL_RATIOS = (0.30, 0.30, 0.40)


# ---------------------------------------------------------------------------
# Test 1: 单档触发
# ---------------------------------------------------------------------------
def test_single_level_triggered():
    """hi=8%, 阶梯 6%/15%：只触发第 0 档"""
    done_mask = 0
    new_mask = compute_ladder_trigger(done_mask, hi_profit=0.08, profits=TWO_LVL_PROFITS)
    # 第 0 档置位
    assert new_mask == 0b01
    assert new_mask != done_mask, "应至少有新触发"
    # sell_ratio 应为 0.30（只算新触发的第 0 档）
    sell_ratio = compute_ladder_sell_ratio(done_mask, new_mask, TWO_LVL_PROFITS, TWO_LVL_RATIOS)
    assert sell_ratio == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# Test 2: 同 bar 双档触发 → 累加 sell_ratio
# ---------------------------------------------------------------------------
def test_same_bar_two_levels_sell_ratio_accumulates():
    """hi=16%, 阶梯 6%/15%：同 bar 触发两档，sell_ratio 应累加为 0.60"""
    prev_mask = 0
    new_mask = compute_ladder_trigger(prev_mask, hi_profit=0.16, profits=TWO_LVL_PROFITS)
    # 两档都置位
    assert new_mask == 0b11, f"两档都应置位，实际 {bin(new_mask)}"
    # sell_ratio 累加（新触发两档：0.30 + 0.30 = 0.60）
    sell_ratio = compute_ladder_sell_ratio(
        prev_mask, new_mask, TWO_LVL_PROFITS, TWO_LVL_RATIOS
    )
    assert sell_ratio == pytest.approx(0.60), f"累加应得 0.60，实际 {sell_ratio}"


# ---------------------------------------------------------------------------
# Test 3: 同 bar 三档触发且累计 = 1.0
# ---------------------------------------------------------------------------
def test_same_bar_three_levels_sell_ratio_one():
    """hi=30%, 阶梯 6%/15%/25% 比例 30%/30%/40%：三档全触发，sell_ratio = 1.0"""
    prev_mask = 0
    new_mask = compute_ladder_trigger(prev_mask, hi_profit=0.30, profits=THREE_LVL_PROFITS)
    assert new_mask == 0b111
    sell_ratio = compute_ladder_sell_ratio(
        prev_mask, new_mask, THREE_LVL_PROFITS, THREE_LVL_RATIOS
    )
    assert sell_ratio == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Test 4: 同 bar 多档触发但累计 > 1.0 → 钳位到 1.0
# ---------------------------------------------------------------------------
def test_sell_ratio_clamped_to_one():
    """故意构造比例合计 1.5，验证钳位"""
    profits = (0.05, 0.10)
    ratios = (0.80, 0.70)  # 合计 1.50
    prev_mask = 0
    new_mask = compute_ladder_trigger(prev_mask, hi_profit=0.15, profits=profits)
    assert new_mask == 0b11
    sell_ratio = compute_ladder_sell_ratio(prev_mask, new_mask, profits, ratios)
    assert sell_ratio == pytest.approx(1.0), f"应钳位到 1.0，实际 {sell_ratio}"


# ---------------------------------------------------------------------------
# Test 5: 第二天只触发第二档 → sell_ratio 只算新触发的那档（分批语义）
# ---------------------------------------------------------------------------
def test_next_bar_only_new_level_contributes():
    """昨天已触发第 0 档 (mask=0b01)，今天 hi=16% 触发第 1 档。
    sell_ratio 应为 0.30（只算新触发的第 1 档），不是 0.60。
    这是 BUG-5 的核心修正：分批止盈语义，老档位不重复计入。
    """
    prev_mask = 0b01  # 第 0 档昨天已触发
    # 今天 hi=16%，第 1 档新触发 → mask 应变成 0b11
    new_mask = compute_ladder_trigger(prev_mask, hi_profit=0.16, profits=TWO_LVL_PROFITS)
    assert new_mask == 0b11
    # sell_ratio 应只算新触发的第 1 档 = 0.30
    sell_ratio = compute_ladder_sell_ratio(
        prev_mask, new_mask, TWO_LVL_PROFITS, TWO_LVL_RATIOS
    )
    assert sell_ratio == pytest.approx(0.30), (
        f"应只算新触发的第 1 档 (0.30)，实际 {sell_ratio}。"
        "如果累加得 0.60，说明 sell_ratio 把老档位也算进去了"
    )


# ---------------------------------------------------------------------------
# Test 6: hi 未达任何档 → 无新触发
# ---------------------------------------------------------------------------
def test_no_level_triggered():
    """hi=5%, 阶梯 6%/15%：无任何档位触发"""
    prev_mask = 0
    new_mask = compute_ladder_trigger(prev_mask, hi_profit=0.05, profits=TWO_LVL_PROFITS)
    assert new_mask == 0
    assert new_mask == prev_mask, "无新触发时 new_mask 应等于 prev_mask"
    # sell_ratio 也应为 0
    sell_ratio = compute_ladder_sell_ratio(
        prev_mask, new_mask, TWO_LVL_PROFITS, TWO_LVL_RATIOS
    )
    assert sell_ratio == 0.0


# ---------------------------------------------------------------------------
# 额外: 所有档位都已触发，hi 继续涨 → 不应重复触发、sell_ratio=0
# ---------------------------------------------------------------------------
def test_all_levels_already_done_no_new_trigger():
    """所有档位都已触发，hi 继续涨 → mask 不变，无新触发 → sell_ratio=0"""
    prev_mask = 0b11  # 两档都触发过
    new_mask = compute_ladder_trigger(prev_mask, hi_profit=0.20, profits=TWO_LVL_PROFITS)
    assert new_mask == 0b11
    # 没有"新触发"的档位 → sell_ratio = 0（不会重复卖）
    sell_ratio = compute_ladder_sell_ratio(
        prev_mask, new_mask, TWO_LVL_PROFITS, TWO_LVL_RATIOS
    )
    assert sell_ratio == 0.0, (
        f"所有档位都已触发过，无新触发，sell_ratio 应为 0，实际 {sell_ratio}"
    )
