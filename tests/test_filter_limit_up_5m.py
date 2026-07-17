"""5m 涨停过滤口径修复 (2026-07-18, MEMORY 老账) + 北交所 30% + 缓存上限。

修复前: prev 取前一根 5m bar, 5m 单 bar 涨 10% 几乎不发生 → 过滤形同虚设。
修复后: 分钟级相对前一**交易日**收盘判定, 与实盘涨停规则一致。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import backtest.engine as engine_mod
from backtest.engine import BacktestEngine


@pytest.fixture(autouse=True)
def no_st(monkeypatch):
    monkeypatch.setattr(engine_mod, 'get_cached_info', lambda c: {'IsSTGP': '0'})


def _make_5m_entries(close):
    return pd.DataFrame(True, index=close.index, columns=close.columns)


def test_5m_limit_up_uses_prev_day_close():
    """5m: 次日开盘即较前日收盘 +10% → 信号被滤(旧口径不滤)。"""
    idx_d1 = pd.date_range('2024-01-02 09:35', periods=48, freq='5min')
    idx_d2 = pd.date_range('2024-01-03 09:35', periods=48, freq='5min')
    idx = idx_d1.append(idx_d2)
    close = pd.DataFrame(10.0, index=idx, columns=['600001'])
    close.iloc[48:, 0] = 11.0  # day2 全部较前日收盘 10.0 涨 10%
    eng = BacktestEngine({'period': '5m'})
    out = eng._filter_limit_up(_make_5m_entries(close), close)
    assert out.iloc[:48, 0].all(), "day1(首日, prev=NaN) 不应过滤"
    assert not out.iloc[48:, 0].any(), "day2 较前日收盘涨停, 应全部过滤"


def test_5m_not_limit_up_when_below_threshold():
    """5m: 次日仅 +5% → 主板 10% 限制下不过滤。"""
    idx_d1 = pd.date_range('2024-01-02 09:35', periods=48, freq='5min')
    idx_d2 = pd.date_range('2024-01-03 09:35', periods=48, freq='5min')
    idx = idx_d1.append(idx_d2)
    close = pd.DataFrame(10.0, index=idx, columns=['600001'])
    close.iloc[48:, 0] = 10.5
    eng = BacktestEngine({'period': '5m'})
    out = eng._filter_limit_up(_make_5m_entries(close), close)
    assert out.all().all()


def test_5m_intrabar_jump_not_misread():
    """5m: 日内单 bar 从 10.0 跳到 10.5 (+5% 单bar), 但较前日收盘也 +5% → 不滤。
    旧口径会把"单 bar 大涨"误判; 新口径只看前日收盘。"""
    idx_d1 = pd.date_range('2024-01-02 09:35', periods=48, freq='5min')
    idx_d2 = pd.date_range('2024-01-03 09:35', periods=48, freq='5min')
    idx = idx_d1.append(idx_d2)
    close = pd.DataFrame(10.0, index=idx, columns=['600001'])
    close.iloc[60:, 0] = 10.5  # day2 中午跳涨, 但全天相对前日 +5%
    eng = BacktestEngine({'period': '5m'})
    out = eng._filter_limit_up(_make_5m_entries(close), close)
    assert out.all().all()


def test_1d_path_unchanged():
    """1d: 口径不变, 仍按前一行(即前一交易日)判定。"""
    idx = pd.date_range('2024-01-02', periods=3, freq='B')
    close = pd.DataFrame({'600001': [10.0, 11.0, 11.0]}, index=idx)
    eng = BacktestEngine({'period': '1d'})
    out = eng._filter_limit_up(_make_5m_entries(close), close)
    assert out.iloc[0, 0]                      # 首行 prev=NaN 不滤
    assert not out.iloc[1, 0]                  # 11.0 >= 10.0*1.1*0.997=10.97 → 滤
    assert out.iloc[2, 0]                      # 11.0 < 11.0*1.1*0.997=12.06 → 保留


def test_bse_30_percent_limit():
    """北交所 4xx/8xx/920: 30% 幅度, +25% 不滤。"""
    idx = pd.date_range('2024-01-02', periods=3, freq='B')
    close = pd.DataFrame({'830001': [10.0, 12.5, 13.0]}, index=idx)
    eng = BacktestEngine({'period': '1d'})
    out = eng._filter_limit_up(_make_5m_entries(close), close)
    assert out.iloc[1, 0]                      # +25% < 30% → 保留
    assert out.iloc[2, 0]                      # 13.0 < 12.5*1.3*0.997=16.2 → 保留


def test_bse_ratio_vector():
    eng = BacktestEngine({})
    vec = eng._limit_ratio_vector(['430001', '830001', '920001', '600001'])
    assert list(vec) == [0.30, 0.30, 0.30, 0.10]


def test_ratio_cache_bounded():
    """缓存上限: 超过 32 个不同列集合后最旧键被淘汰, 不无限增长。"""
    eng = BacktestEngine({})
    for k in range(40):
        eng._limit_ratio_vector([f'6{k:05d}'])
    assert len(eng._limit_ratio_cache) <= 32


def test_empty_entries_frame():
    """空帧守卫: 0 行 DataFrame 不炸 (旧向量化版 prev[0] 会 IndexError)。"""
    close = pd.DataFrame({'600001': []}, index=pd.DatetimeIndex([]))
    entries = pd.DataFrame({'600001': []}, index=pd.DatetimeIndex([]))
    eng = BacktestEngine({})
    out = eng._filter_limit_up(entries, close)
    assert len(out) == 0
