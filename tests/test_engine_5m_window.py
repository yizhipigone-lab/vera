"""F4 (2026-07-18 审计): engine.run() 5m 稀疏窗口块测试 — 此前零覆盖。

被测块: backtest/engine.py:758-824 (get_kline_windowed 调用 / win_td 自动加长 /
tradable∩window_mask / last_tradable_idx 窗口内重算)。
mock DataFetcher.get_kline_windowed + _simulate_core_v3(捕获透传矩阵), 不触 TDX。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

import backtest.engine as engine_module
from backtest.engine import BacktestEngine


def _make_5m(n_bars=96, n_stocks=2):
    """2 天 × 48 根 5m bar。"""
    idx = pd.date_range('2024-01-02 09:35', periods=n_bars, freq='5min')
    codes = ['600001', '688001'][:n_stocks]
    close = pd.DataFrame(
        10.0 + np.arange(n_bars)[:, None] * 0.01 + np.arange(n_stocks)[None, :],
        index=idx, columns=codes)
    return close, codes, idx


def _run_5m(monkeypatch, close, mask, stop_config=None, capture=None, config=None):
    """公共驱动: mock 窗口拉取 + 捕获 _simulate_core_v3 入参。"""
    eng = BacktestEngine(config or {'period': '5m'})
    kline = {'Close': close, 'High': close * 1.01, 'Low': close * 0.99,
             'Open': close.copy()}
    called = {}

    def fake_windowed(selections, period, window_trading_days, dividend_type,
                      fill_data, *, use_cache=False):
        called['window_trading_days'] = window_trading_days
        called['use_cache'] = use_cache
        return kline, mask

    monkeypatch.setattr(engine_module.DataFetcher, 'get_kline_windowed', fake_windowed)
    monkeypatch.setattr(BacktestEngine, '_filter_limit_up',
                        lambda self, entries, prices: entries)

    def fake_core(price_np, entry_np, *args, **kwargs):
        if capture is not None:
            capture['tradable_np'] = kwargs.get('tradable_np')
            capture['last_tradable_idx'] = kwargs.get('last_tradable_idx')
        return np.full(price_np.shape[0], 100000.0), np.empty((0, 9))

    monkeypatch.setattr(engine_module, '_simulate_core_v3', fake_core)

    selections = pd.DataFrame([
        {'select_date': close.index[0], 'stock_code': c} for c in close.columns])
    eng.run(selections=selections, start_time='20240102', end_time='20240103',
            stop_config=stop_config or {'time_stop': {'enabled': True, 'max_hold_days': 20}})
    return called


def test_window_auto_lengthens_when_max_hold_days_close(monkeypatch, caplog):
    """max_hold_days=40, 默认窗口 45 ≤ 45 → 自动加长到 55 并告警。"""
    close, codes, idx = _make_5m()
    mask = pd.DataFrame(True, index=idx, columns=codes)
    with caplog.at_level(logging.WARNING):
        called = _run_5m(monkeypatch, close, mask,
                         stop_config={'time_stop': {'enabled': True, 'max_hold_days': 40}})
    assert called['window_trading_days'] == 55
    assert any('自动加长窗口' in r.message for r in caplog.records)


def test_window_no_lengthen_when_max_hold_days_small(monkeypatch, caplog):
    """max_hold_days=20, 默认窗口 45 > 25 → 不加长不告警。"""
    close, codes, idx = _make_5m()
    mask = pd.DataFrame(True, index=idx, columns=codes)
    with caplog.at_level(logging.WARNING):
        called = _run_5m(monkeypatch, close, mask)
    assert called['window_trading_days'] == 45
    assert not any('自动加长窗口' in r.message for r in caplog.records)


def test_window_warns_when_time_stop_disabled(monkeypatch, caplog):
    """time_stop 关闭 → 告警窗口尾部持仓会被当退市强平。"""
    close, codes, idx = _make_5m()
    mask = pd.DataFrame(True, index=idx, columns=codes)
    with caplog.at_level(logging.WARNING):
        _run_5m(monkeypatch, close, mask,
                stop_config={'time_stop': {'enabled': False}})
    assert any('建议开启时间止损' in r.message for r in caplog.records)


def test_window_mask_reindexes_shuffled_columns(monkeypatch):
    """window_mask 列顺序与 close 乱序 → reindex 后按股票名对齐, 不张冠李戴。"""
    close, codes, idx = _make_5m()
    # 600001 全程可交易; 688001 只有前半窗口可交易, mask 列故意乱序
    mask = pd.DataFrame(True, index=idx, columns=['688001', '600001'])
    mask.iloc[48:, mask.columns.get_loc('688001')] = False
    capture = {}
    _run_5m(monkeypatch, close, mask, capture=capture)
    tradable = capture['tradable_np']
    cols = sorted(close.columns)  # run() 内 cols 排序: ['600001', '688001']
    ci_600, ci_688 = cols.index('600001'), cols.index('688001')
    assert tradable[:, ci_600].all(), "600001 应全程可交易"
    assert tradable[:48, ci_688].all() and not tradable[48:, ci_688].any(), \
        "688001 窗口外应不可交易(乱序 mask 未对齐则此项错)"


def test_last_tradable_idx_recomputed_inside_window(monkeypatch):
    """last_tradable_idx = 窗口内最后一个可交易 bar (每列独立)。"""
    close, codes, idx = _make_5m()
    mask = pd.DataFrame(True, index=idx, columns=codes)
    mask.iloc[60:, mask.columns.get_loc('688001')] = False
    capture = {}
    _run_5m(monkeypatch, close, mask, capture=capture)
    lti = capture['last_tradable_idx']
    cols = sorted(close.columns)
    assert lti[cols.index('600001')] == len(idx) - 1
    assert lti[cols.index('688001')] == 59


def test_engine_5m_passes_use_kline_cache(monkeypatch):
    """engine.use_kline_cache (默认 True) 应透传到 get_kline_windowed 的 use_cache。"""
    close, codes, idx = _make_5m()
    mask = pd.DataFrame(True, index=idx, columns=codes)
    called = _run_5m(monkeypatch, close, mask, capture={})
    assert called.get('use_cache') is True


def test_engine_5m_passes_use_kline_cache_false(monkeypatch):
    """反向 (审计 LOW-1): use_kline_cache=False 必须透传 False — 防误改硬编码 True。"""
    close, codes, idx = _make_5m()
    mask = pd.DataFrame(True, index=idx, columns=codes)
    called = _run_5m(monkeypatch, close, mask, capture={},
                     config={'period': '5m', 'use_kline_cache': False})
    assert called.get('use_cache') is False
