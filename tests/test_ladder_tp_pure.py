"""compute_ladder_trigger / compute_ladder_sell_ratio 纯函数测试 (迭代 3, 2026-07-15).

这些是 LadderTpStrategy.check() 内部调用的纯函数, 直接覆盖可避开 strategy
状态耦合. 锁住 ladder 触发的所有边界条件:
- 0 档触发
- 单档触发
- 多档同时触发
- 跨 bar 累计 (bitmask)
- 全档触发 (阻塞场景)
- 极端: hi_pp == profit (边界, F-H6 不容差)
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.ladder_tp import compute_ladder_trigger, compute_ladder_sell_ratio


# ═══════════════════════════════════════════════════════════════
# compute_ladder_trigger — 位掩码生成
# ═══════════════════════════════════════════════════════════════


def test_trigger_no_levels_above_hi():
    """hi_pp < 所有档位 → 不触发任何 (new_mask == prev_mask)."""
    profits = np.array([0.06, 0.15], dtype=np.float64)
    new_mask = compute_ladder_trigger(done_mask=0, hi_profit=0.04, profits=profits)
    assert new_mask == 0


def test_trigger_first_level():
    """hi_pp >= 第 1 档 profit (0.06) → mask=0b01."""
    profits = np.array([0.06, 0.15], dtype=np.float64)
    new_mask = compute_ladder_trigger(done_mask=0, hi_profit=0.07, profits=profits)
    assert new_mask == 0b01


def test_trigger_second_level():
    """hi_pp >= 第 2 档 (0.15) → mask=0b11."""
    profits = np.array([0.06, 0.15], dtype=np.float64)
    new_mask = compute_ladder_trigger(done_mask=0, hi_profit=0.16, profits=profits)
    assert new_mask == 0b11


def test_trigger_boundary_equal_to_profit():
    """hi_pp == profit → 触发 (<=, F-H6 不容差)."""
    profits = np.array([0.06, 0.15], dtype=np.float64)
    new_mask = compute_ladder_trigger(done_mask=0, hi_profit=0.06, profits=profits)
    assert new_mask == 0b01


def test_trigger_accumulates_across_bars():
    """prev_mask 已含第 1 档 → 跨 bar 累计, hi_pp >= 第 2 档 → new_mask=0b11."""
    profits = np.array([0.06, 0.15], dtype=np.float64)
    new_mask = compute_ladder_trigger(done_mask=0b01, hi_profit=0.16, profits=profits)
    assert new_mask == 0b11


def test_trigger_idempotent_when_already_triggered():
    """prev_mask 已含所有档位 → new_mask == prev_mask (无新触发)."""
    profits = np.array([0.06, 0.15], dtype=np.float64)
    new_mask = compute_ladder_trigger(done_mask=0b11, hi_profit=0.20, profits=profits)
    assert new_mask == 0b11


def test_trigger_does_not_downgrade():
    """hi_pp 下降时, prev_mask 已置位的位不应被清除."""
    profits = np.array([0.06, 0.15], dtype=np.float64)
    # 第 1 档已触发, 现在 hi_pp 下降到 0.04
    new_mask = compute_ladder_trigger(done_mask=0b01, hi_profit=0.04, profits=profits)
    assert new_mask == 0b01  # 第 1 档位保留


def test_trigger_three_levels():
    """3 档: hi_pp >= 0.20 → mask=0b111."""
    profits = np.array([0.06, 0.15, 0.25], dtype=np.float64)
    new_mask = compute_ladder_trigger(done_mask=0, hi_profit=0.30, profits=profits)
    assert new_mask == 0b111


# ═══════════════════════════════════════════════════════════════
# compute_ladder_sell_ratio — 卖出比例
# ═══════════════════════════════════════════════════════════════


def test_sell_ratio_no_new_trigger_returns_zero():
    """无新触发 → sell_ratio=0 (不卖)."""
    profits = np.array([0.06, 0.15], dtype=np.float64)
    ratios = np.array([0.30, 0.30], dtype=np.float64)
    ratio = compute_ladder_sell_ratio(prev_mask=0b11, new_mask=0b11, profits=profits, ratios=ratios)
    assert ratio == 0.0


def test_sell_ratio_single_level_trigger():
    """从 0 → 0b01 触发第 1 档 (ratio=0.30) → 卖 30%."""
    profits = np.array([0.06, 0.15], dtype=np.float64)
    ratios = np.array([0.30, 0.30], dtype=np.float64)
    ratio = compute_ladder_sell_ratio(prev_mask=0, new_mask=0b01, profits=profits, ratios=ratios)
    assert ratio == pytest.approx(0.30)


def test_sell_ratio_two_levels_trigger_at_once():
    """从 0 → 0b11 同时触发两档 → 卖 30% + 30% = 60%."""
    profits = np.array([0.06, 0.15], dtype=np.float64)
    ratios = np.array([0.30, 0.30], dtype=np.float64)
    ratio = compute_ladder_sell_ratio(prev_mask=0, new_mask=0b11, profits=profits, ratios=ratios)
    assert ratio == pytest.approx(0.60)


def test_sell_ratio_partial_cumulative():
    """第 1 档已触发, 现在触发第 2 档 → 只算新增 (第 2 档) 的 30%."""
    profits = np.array([0.06, 0.15], dtype=np.float64)
    ratios = np.array([0.30, 0.30], dtype=np.float64)
    ratio = compute_ladder_sell_ratio(prev_mask=0b01, new_mask=0b11, profits=profits, ratios=ratios)
    assert ratio == pytest.approx(0.30)


def test_sell_ratio_full_close_when_total_ratio_geq_one():
    """ratios 之和 >= 1.0 → 视为全卖 (阻塞 dispatcher 后续策略)."""
    profits = np.array([0.06, 0.15], dtype=np.float64)
    ratios = np.array([0.60, 0.60], dtype=np.float64)  # 总和 1.2
    ratio = compute_ladder_sell_ratio(prev_mask=0, new_mask=0b11, profits=profits, ratios=ratios)
    # 实现细节: 超过 1.0 时 clip 到 1.0
    assert ratio == pytest.approx(1.0)


def test_sell_ratio_uneven_ratios():
    """不等比例 (0.20 + 0.50 = 0.70)."""
    profits = np.array([0.06, 0.15], dtype=np.float64)
    ratios = np.array([0.20, 0.50], dtype=np.float64)
    ratio = compute_ladder_sell_ratio(prev_mask=0, new_mask=0b11, profits=profits, ratios=ratios)
    assert ratio == pytest.approx(0.70)


# ═══════════════════════════════════════════════════════════════
# 兼容性 + 边界
# ═══════════════════════════════════════════════════════════════


def test_trigger_with_single_level():
    """单档 ladder."""
    profits = np.array([0.10], dtype=np.float64)
    new_mask = compute_ladder_trigger(done_mask=0, hi_profit=0.12, profits=profits)
    assert new_mask == 0b1


def test_trigger_with_zero_levels():
    """空 profits → 不触发."""
    profits = np.array([], dtype=np.float64)
    new_mask = compute_ladder_trigger(done_mask=0, hi_profit=0.10, profits=profits)
    assert new_mask == 0


def test_trigger_returns_int_type():
    """返回值必须是 int (bitmask 操作期望)."""
    profits = np.array([0.06], dtype=np.float64)
    new_mask = compute_ladder_trigger(done_mask=0, hi_profit=0.07, profits=profits)
    assert isinstance(new_mask, (int, np.integer))


def test_sell_ratio_returns_float():
    """sell_ratio 必须是 float."""
    profits = np.array([0.06], dtype=np.float64)
    ratios = np.array([0.30], dtype=np.float64)
    ratio = compute_ladder_sell_ratio(prev_mask=0, new_mask=0b1, profits=profits, ratios=ratios)
    assert isinstance(ratio, (float, np.floating))