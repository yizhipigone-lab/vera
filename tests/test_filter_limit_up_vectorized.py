"""Phase 1 (2026-07-17): _filter_limit_up 全矩阵向量化的等价性与缓存测试。

锁定: 向量化实现与旧逐列实现结果 DataFrame.equals 完全一致。
旧实现内联于本测试作甲骨文(oracle), 不依赖生产代码旧版本。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import backtest.engine as engine_mod
from backtest.engine import BacktestEngine


def _old_filter_limit_up(entries, close, get_info):
    """旧逐列实现 (甲骨文, 复制自 25cdee6 之前 engine.py)。"""
    if not isinstance(entries, pd.DataFrame):
        return entries
    result = entries.copy()
    prev_close = close.shift(1)
    for col in entries.columns:
        limit_ratio = 0.10
        col_str = str(col)
        if col_str.startswith('688'):
            limit_ratio = 0.20
        elif col_str.startswith('300') or col_str.startswith('301'):
            limit_ratio = 0.20
        else:
            info = get_info(col_str)
            if str(info.get('IsSTGP', '0')) == '1':
                limit_ratio = 0.05
        limit_price = prev_close[col] * (1.0 + limit_ratio)
        is_limit_up = close[col] >= limit_price * 0.997
        result.loc[is_limit_up, col] = False
    return result


def _make(n_dates=120, seed=7):
    rng = np.random.default_rng(seed)
    dates = pd.date_range('2024-01-01', periods=n_dates, freq='B')
    codes = ['600001', '600002', '300001', '301001', '688001', '00005T']
    price = 10.0 * np.cumprod(1.0 + rng.normal(0, 0.03, (n_dates, len(codes))), axis=0)
    close = pd.DataFrame(price, index=dates, columns=codes)
    # 人为造一些"接近涨停"的行, 让过滤真的触发
    for j, c in enumerate(codes):
        rows = rng.choice(n_dates - 1, size=8, replace=False) + 1
        close.iloc[rows, j] = close.iloc[rows - 1, j].values * 1.099
    entries = pd.DataFrame(rng.random((n_dates, len(codes))) < 0.2,
                           index=dates, columns=codes)
    return close, entries


@pytest.fixture
def st_map(monkeypatch):
    """00005T 判为 ST, 其余非 ST。"""
    calls = []

    def fake_info(code):
        calls.append(code)
        return {'IsSTGP': '1' if code == '00005T' else '0'}

    monkeypatch.setattr(engine_mod, 'get_cached_info', fake_info)
    return calls


def test_vectorized_equals_old(st_map):
    close, entries = _make()
    eng = BacktestEngine({})
    new = eng._filter_limit_up(entries, close)
    old = _old_filter_limit_up(entries, close,
                               lambda c: {'IsSTGP': '1' if c == '00005T' else '0'})
    assert new.equals(old), "向量化结果与旧逐列实现不一致"
    assert new.sum().sum() < entries.sum().sum(), "过滤未生效(测试数据构造失败)"


def test_first_row_never_filtered(st_map):
    """首行 prev=NaN → 比较恒 False → 首行信号必须保留。"""
    close, entries = _make()
    entries.iloc[0, :] = True
    eng = BacktestEngine({})
    new = eng._filter_limit_up(entries, close)
    assert new.iloc[0].all()


def test_ratio_vector_board_rules(st_map):
    """四种板块幅度: 600→0.10, 300/301/688→0.20, ST→0.05。"""
    eng = BacktestEngine({})
    vec = eng._limit_ratio_vector(['600001', '300001', '301001', '688001', '00005T'])
    assert list(vec) == [0.10, 0.20, 0.20, 0.20, 0.05]


def test_ratio_cache_reuses_across_calls(st_map):
    """同列集合第二次调用不再查 ST 信息 (批量跑 N 公式只查一次)。"""
    eng = BacktestEngine({})
    cols = ['600001', '00005T']
    eng._limit_ratio_vector(cols)
    n_after_first = len(st_map)
    assert n_after_first == 2  # 688/300/301 不查; 600001 与 00005T 各查一次 (与旧逻辑一致)
    eng._limit_ratio_vector(cols)
    assert len(st_map) == n_after_first, "缓存未生效, 重复查询 ST 信息"


def test_missing_column_raises_keyerror(st_map):
    """close 缺列时与旧版行为一致: KeyError。"""
    close, entries = _make()
    eng = BacktestEngine({})
    with pytest.raises(KeyError):
        eng._filter_limit_up(entries, close.drop(columns=['600001']))


def test_non_dataframe_passthrough(st_map):
    eng = BacktestEngine({})
    arr = np.ones((3, 3), dtype=bool)
    assert eng._filter_limit_up(arr, None) is arr
