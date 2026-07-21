# -*- coding: utf-8 -*-
"""移动止盈跳空保护(opt-in) — 触发价语义测试

2026-07-21 用户拍板: 跳空低开跌穿回撤线时, gap_protection=True 按 min(回撤线,开盘价) 成交;
默认 False 保持原语义(始终按回撤线价, 与 legacy/parity 一致)。
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.loop.state import Bar, Context, Position  # noqa: E402
from backtest.loop.strategies.trailing import TrailingStrategy  # noqa: E402


def _ctx(entry=10.0, peak=10.30):
    """entry 10.00, 持仓期最高 10.30(+3%, 已激活)。"""
    return Context(
        bar_index=5, ci=0, bpday=1, hold_days=2,
        entry_px=entry, pp=0.02, hp_profit=0.03,
        peak_hi=peak, peak_hi_profit=(peak - entry) / entry,
        pos_high_px=peak, pos_high_hi=peak, hi_pp=0.03, lo_pp=0.01,
        ladder_profits=np.array([]), ladder_ratios=np.array([]), n_ladder=0,
    )


def _pos(entry=10.0):
    return Position(code=0, shares=1000.0, entry_px=entry, entry_idx=1,
                    high_px=10.30, high_hi=10.30, ladder_done=0)


TRAIL_LINE = 10.30 * (1 - 0.005)   # 10.2485


def test_gap_bar_fills_at_open_when_protection_on():
    """已激活持仓, 次日跳空低开 10.20(<10.2485): 保护开 → 按开盘价成交。"""
    s = TrailingStrategy(activation=0.02, drawdown=0.005, gap_protection=True)
    bar = Bar(close=10.21, high=10.22, low=10.19, open=10.20)
    hits = s.check(_pos(), bar, _ctx())
    assert len(hits) == 1
    assert hits[0].execution_price == 10.20      # 按开盘价, 不是回撤线


def test_gap_bar_fills_at_trail_line_when_protection_off():
    """同一 gap bar: 保护关(默认) → 按回撤线 10.2485 成交(原语义, parity 不破)。"""
    s = TrailingStrategy(activation=0.02, drawdown=0.005)   # 默认 False
    bar = Bar(close=10.21, high=10.22, low=10.19, open=10.20)
    hits = s.check(_pos(), bar, _ctx())
    assert len(hits) == 1
    assert hits[0].execution_price == TRAIL_LINE


def test_normal_bar_still_fills_at_trail_line():
    """非跳空 bar(开盘 10.26 高于回撤线): 保护开也按回撤线成交。"""
    s = TrailingStrategy(activation=0.02, drawdown=0.005, gap_protection=True)
    bar = Bar(close=10.25, high=10.27, low=10.24, open=10.26)
    hits = s.check(_pos(), bar, _ctx())
    assert len(hits) == 1
    assert hits[0].execution_price == TRAIL_LINE


def test_not_armed_no_trigger():
    """未激活(峰值未达 2%)不触发。"""
    s = TrailingStrategy(activation=0.02, drawdown=0.005, gap_protection=True)
    bar = Bar(close=10.05, high=10.08, low=10.04, open=10.06)
    ctx = _ctx(peak=10.10)   # +1% 未达激活线
    assert s.check(_pos(), bar, ctx) == []


def test_same_bar_new_peak_keeps_trail_line():
    """本 bar 自己创出峰值(peak_hi == bar.high): 同 bar 顺序不可知,
    不属跳空保护范围, 保持线价语义(保护开也不按开盘价)。"""
    s = TrailingStrategy(activation=0.02, drawdown=0.005, gap_protection=True)
    # 本 bar 冲高到 10.35(= peak_hi), 开盘 10.05 < 线 10.333
    ctx = Context(
        bar_index=5, ci=0, bpday=1, hold_days=2,
        entry_px=10.0, pp=0.03, hp_profit=0.035,
        peak_hi=10.35, peak_hi_profit=0.035,
        pos_high_px=10.35, pos_high_hi=10.35, hi_pp=0.035, lo_pp=0.002,
        ladder_profits=np.array([]), ladder_ratios=np.array([]), n_ladder=0,
    )
    bar = Bar(close=10.30, high=10.35, low=10.02, open=10.05)
    hits = s.check(_pos(), bar, ctx)
    assert len(hits) == 1
    assert hits[0].execution_price == 10.35 * (1 - 0.005)   # 线价, 不是开盘 10.05


def test_gap_protection_requires_peak_from_earlier_bar():
    """保护开 + 峰值来自本 bar(peak==bar.high): 不按开盘价, 保持线价。
    对照 test_gap_bar_fills_at_open_when_protection_on(峰值来自更早 bar): 按开盘价。"""
    s = TrailingStrategy(activation=0.02, drawdown=0.005, gap_protection=True)
    ctx = _ctx(peak=10.22)   # peak 恰好= 下面的 bar.high
    bar = Bar(close=10.15, high=10.22, low=10.10, open=10.14)   # open < 线 10.1489
    hits = s.check(_pos(), bar, ctx)
    assert hits[0].execution_price == 10.22 * (1 - 0.005)
