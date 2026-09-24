"""core/index_regime.py 单测 — 牛熊口径单一真相源 (治理III W1-b, 2026-09-05).

门槛常量是用户拍板的决策锚点 (MA250 / 门槛200 / 斜率20), 谁要改动
必须显式改这里的断言, 不许悄悄漂移。
"""
import pandas as pd

from core.index_regime import (
    BEAR,
    BULL,
    RANGE,
    MA_WINDOW,
    MIN_PERIODS,
    SLOPE_SPAN,
    classify,
    regime_series,
)


def test_constants_anchored():
    """决策锚点: 2026-09-05 用户拍板门槛 200 天 (原分叉: data_tools 250 / research 60)。"""
    assert MA_WINDOW == 250      # 牛熊线 MA250, 与 MA200 交易闸门保持区分
    assert MIN_PERIODS == 200
    assert SLOPE_SPAN == 20
    assert (BULL, BEAR, RANGE) == ("bull", "bear", "range")


def _rising(n, start=10.0, step=0.05):
    return pd.Series([start + i * step for i in range(n)])


def _falling(n, start=100.0, step=0.05):
    return pd.Series([start - i * step for i in range(n)])


def test_bull_rising_above_ma():
    state, ma_now, slope = classify(_rising(300))
    assert state == BULL
    assert ma_now > 0 and slope > 0


def test_bear_falling_below_ma():
    state, ma_now, slope = classify(_falling(300))
    assert state == BEAR
    assert slope < 0


def test_range_flat_series():
    # 常数序列: 价 == MA 且斜率 == 0, 两个严格条件都不满足 → 震荡
    state, _, slope = classify(pd.Series([50.0] * 300))
    assert state == RANGE
    assert slope == 0


def test_insufficient_returns_none():
    # 门槛: MA 自第 200 根起有值, 斜率还要再往前 20 根 → 最少 220 根才有判定
    assert classify(_rising(200))[0] is None   # MA 有值但斜率缺
    assert classify(_rising(219))[0] is None
    assert classify(_rising(220))[0] == BULL


def test_regime_series_matches_classify_at_tail():
    closes = _falling(300)
    series = regime_series(closes)
    assert series.iloc[-1] == classify(closes)[0] == BEAR


def test_regime_series_insufficient_is_range():
    # 历史回填口径: 样本不足段标 range (中性默认, 与 research 旧行为一致)
    series = regime_series(_rising(100))
    assert set(series.unique()) == {RANGE}
