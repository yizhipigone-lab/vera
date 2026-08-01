# -*- coding: utf-8 -*-
"""1m 常量层测试 (2026-07-26, 计划书 §5.1)。

钉死:
1. BARS_PER_DAY["1m"]==240, PERIODS_PER_YEAR["1m"]==60480
2. STD_1M_BAR_TIMES 恰好 240 个时刻, 首尾 09:31/15:00, 无杂时刻
3. STD_BAR_TIMES 映射完整 (5m/1m), STD_5M_BAR_TIMES 旧名不变
4. _CALENDAR_BARS_PER_DAY 不含 "1m" (防未来 1m 选股被 3000 封顶静默浅扫)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest._constants import (  # noqa: E402
    BARS_PER_DAY,
    PERIODS_PER_YEAR,
    STD_1M_BAR_TIMES,
    STD_5M_BAR_TIMES,
    STD_BAR_TIMES,
)


def test_bars_per_day_1m():
    assert BARS_PER_DAY["1m"] == 240
    assert BARS_PER_DAY["5m"] == 48  # 回归


def test_periods_per_year_1m():
    assert PERIODS_PER_YEAR["1m"] == 60480
    assert PERIODS_PER_YEAR["5m"] == 48 * 252


def test_std_1m_bar_times_content():
    assert len(STD_1M_BAR_TIMES) == 240
    assert "09:31" in STD_1M_BAR_TIMES and "15:00" in STD_1M_BAR_TIMES
    assert "11:30" in STD_1M_BAR_TIMES and "13:01" in STD_1M_BAR_TIMES
    # 无杂时刻: 竞价/午休/收盘后
    for bad in ("09:30", "11:31", "12:00", "12:59", "15:01"):
        assert bad not in STD_1M_BAR_TIMES
    # 分钟连续性抽查
    assert "09:32" in STD_1M_BAR_TIMES and "10:00" in STD_1M_BAR_TIMES
    assert "13:02" in STD_1M_BAR_TIMES and "14:59" in STD_1M_BAR_TIMES


def test_std_bar_times_mapping():
    assert STD_BAR_TIMES["5m"] == STD_5M_BAR_TIMES
    assert STD_BAR_TIMES["1m"] == STD_1M_BAR_TIMES
    assert len(STD_BAR_TIMES["5m"]) == 48  # 回归


def test_calendar_bars_per_day_no_1m():
    """计划书 §3.1 v2 修订: 不加 1m 映射, 防 _MAX_SCAN_COUNT=3000 静默浅扫。

    2026-07-30 (8b0a1ba): _CALENDAR_BARS_PER_DAY 随自适应扫描深度一并移除,
    扫描深度回到全周期恒定 3000。守注意图不变 —— 钉死"1m 无独立浅扫映射":
    任何 period (含 1m) 都返回同一深度。注意 3000 根 1m bar ≈ 12.5 个交易日,
    即当前设计不支持 1m 选股; 若未来支持, 必须重设计扫描深度并改本测试。
    """
    from core.formula_runner import _MAX_SCAN_COUNT, _adaptive_scan_count
    for period in ("1d", "5m", "1m"):
        assert _adaptive_scan_count("2024-01-01", "2024-12-31", period) \
            == _MAX_SCAN_COUNT
