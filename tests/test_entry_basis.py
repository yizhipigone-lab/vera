"""入场价口径守卫测试 (迭代 1, 2026-07-15).

锁住:
- 业务铁律 2: 回测 = 信号日 T 收盘价 (不容改)
- 业务铁律 3: 两套口径绝不混用
- EntryEngine 必须显式声明走 BACKTEST_T_CLOSE 路径
- LiveBiasEstimate 偏差估算正确性
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest._entry_basis import (
    ENTRY_BASIS_BACKTEST,
    ENTRY_BASIS_LIVE,
    EntryPath,
    EntryBasisConflictError,
    LiveBiasEstimate,
    assert_single_path,
)
from backtest.loop.entry import ENTRY_PATH


# ═══════════════════════════════════════════════════════════════
# 铁律 2: 回测 = 信号日 T 收盘价
# ═══════════════════════════════════════════════════════════════


def test_backtest_basis_is_signal_day_close():
    """业务铁律 2: 回测入场价口径字面量必须 = close_on_signal_day."""
    assert ENTRY_BASIS_BACKTEST == "close_on_signal_day"


def test_live_basis_is_next_day_open():
    """业务铁律 3: 实盘入场价口径字面量必须 = open_on_next_day."""
    assert ENTRY_BASIS_LIVE == "open_on_next_day"


def test_backtest_and_live_basis_must_differ():
    """两套口径字面量必须不同 — 防止有人误把实盘标成回测."""
    assert ENTRY_BASIS_BACKTEST != ENTRY_BASIS_LIVE


# ═══════════════════════════════════════════════════════════════
# EntryEngine 路径守卫
# ═══════════════════════════════════════════════════════════════


def test_entry_engine_uses_backtest_path():
    """EntryEngine 必须显式声明 BACKTEST_T_CLOSE — 业务铁律 2 代码层落地."""
    assert ENTRY_PATH == EntryPath.BACKTEST_T_CLOSE
    assert ENTRY_PATH.is_backtest is True
    assert ENTRY_PATH.is_live is False


def test_entry_engine_path_value_matches_basis_constant():
    """ENTRY_PATH 字面量必须与 ENTRY_BASIS_BACKTEST 一致 (单一真相源)."""
    assert ENTRY_PATH.value == ENTRY_BASIS_BACKTEST


# ═══════════════════════════════════════════════════════════════
# 铁律 3: 路径冲突守卫
# ═══════════════════════════════════════════════════════════════


def test_assert_single_path_allows_only_backtest():
    """全回测路径应通过."""
    assert_single_path(
        EntryPath.BACKTEST_T_CLOSE,
        EntryPath.BACKTEST_T_CLOSE,
    )


def test_assert_single_path_allows_only_live():
    """全实盘路径应通过."""
    assert_single_path(
        EntryPath.LIVE_T_PLUS_1_OPEN,
        EntryPath.LIVE_T_PLUS_1_OPEN,
    )


def test_assert_single_path_rejects_mixed():
    """混用回测+实盘必须抛 EntryBasisConflictError — 业务铁律 3 核心守卫."""
    with pytest.raises(EntryBasisConflictError) as exc_info:
        assert_single_path(
            EntryPath.BACKTEST_T_CLOSE,
            EntryPath.LIVE_T_PLUS_1_OPEN,
        )
    assert "业务铁律 3" in str(exc_info.value)


def test_assert_single_path_empty_is_ok():
    """空路径应通过 (无路径 = 无冲突, 边界合理)."""
    assert_single_path()


# ═══════════════════════════════════════════════════════════════
# LiveBiasEstimate 偏差估算
# ═══════════════════════════════════════════════════════════════


def test_bias_estimate_zero_is_identity():
    """零偏差应等于回测原值."""
    bias = LiveBiasEstimate()
    assert bias.estimate_adjusted_return(0.25) == pytest.approx(0.25)


def test_bias_estimate_subtracts_compound_bias():
    """综合偏差应从回测收益中扣除 (保守估算)."""
    bias = LiveBiasEstimate(compound_bias_pct=0.05)  # 5pp 偏差
    adjusted = bias.estimate_adjusted_return(0.25)
    assert adjusted == pytest.approx(0.20)


def test_bias_estimate_from_empirical_default_slippage():
    """from_empirical 启发式公式 — 默认 liquidity_slippage 5bps."""
    # 5bps = 0.05% 流动性损耗
    bias = LiveBiasEstimate.from_empirical(
        t_close_to_t1_open_gap=0.001,        # +0.1% 平均跳空
        limit_up_down_miss_rate=0.05,         # 5% 涨跌停 miss
        liquidity_slippage_bps=5.0,
    )
    # compound = 0.05 * 0.02 + 0.001 + 5/10000 = 0.001 + 0.001 + 0.0005 = 0.0025
    assert bias.compound_bias_pct == pytest.approx(0.0025)


def test_bias_estimate_from_empirical_zero_miss_rate():
    """涨跌停 miss=0 时, 偏差只剩流动性 + 跳空."""
    bias = LiveBiasEstimate.from_empirical(
        t_close_to_t1_open_gap=0.002,
        limit_up_down_miss_rate=0.0,
        liquidity_slippage_bps=10.0,
    )
    # compound = 0 + 0.002 + 10/10000 = 0.003
    assert bias.compound_bias_pct == pytest.approx(0.003)


def test_bias_estimate_adjusted_return_with_realistic_bias():
    """实盘估算: 25% 回测 - 2.5pp 偏差 = 22.5% 实盘估算."""
    bias = LiveBiasEstimate(compound_bias_pct=0.025)
    assert bias.estimate_adjusted_return(0.25) == pytest.approx(0.225)


def test_bias_estimate_frozen_dataclass():
    """LiveBiasEstimate 必须 frozen — 防运行时状态漂移."""
    bias = LiveBiasEstimate(compound_bias_pct=0.05)
    with pytest.raises(Exception):
        bias.compound_bias_pct = 0.10