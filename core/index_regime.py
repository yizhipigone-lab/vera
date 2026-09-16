"""core/index_regime.py — 市场牛熊口径单一真相源 (治理III W1-b, 2026-09-05 收口)。

判定 (只用 T 日及以前数据, 不看未来):
    bull  : C > MA250 且 MA250 的 20 日斜率 > 0
    bear  : C < MA250 且 MA250 的 20 日斜率 < 0
    range : 其他

背景: 2026-09-04 市场体检表 (brain/data_tools.market_health) 与
research/index_regime.py 各自实现本口径且参数分叉 (min_periods 250 vs 60,
注释宣称"复用"实为复制), 2026-09-05 治理III 收口至此。

门槛 MIN_PERIODS=200 为 2026-09-05 用户拍板: 不满 200 根不算, 短样本
不冒充。MA 自第 200 根起有值; 斜率还需 MA 再往前 20 根, 故出判定实际
最少 220 根。MA 线仍为 MA250, 与 MA200 交易闸门 (trade/regime.py) 保持区分。
"""
from __future__ import annotations

import pandas as pd

__all__ = [
    "MA_WINDOW", "MIN_PERIODS", "SLOPE_SPAN",
    "BULL", "BEAR", "RANGE",
    "classify", "regime_series", "ma_and_slope",
]

MA_WINDOW = 250
MIN_PERIODS = 200   # 2026-09-05 用户拍板 (原分叉: data_tools 250 / research 60)
SLOPE_SPAN = 20

BULL = "bull"
BEAR = "bear"
RANGE = "range"


def _ma_and_slope(closes: pd.Series) -> tuple[pd.Series, pd.Series]:
    c = closes.astype(float)
    ma = c.rolling(MA_WINDOW, min_periods=MIN_PERIODS).mean()
    slope = ma / ma.shift(SLOPE_SPAN) - 1
    return ma, slope


def ma_and_slope(closes) -> tuple[pd.Series, pd.Series]:
    """全序列的 MA250 与 MA250 的 20 日斜率 (2026-09-17 加入)。

    存在的理由: 需要"偏离年线多少"这类 MA **数值**的消费方 (如大盘位置指标)
    若各自重算一遍 MA250, 就是把同一个窗口写第二份 —— 本项目已有多次
    "同一规则 N 份实现必然漂移"的教训 (买卖口径/行情陈旧判定/AI 设置三档)。
    故把内部实现开一个只读出口, 口径仍只此一处。
    """
    return _ma_and_slope(pd.Series(closes))


def classify(closes) -> tuple[str | None, float, float]:
    """序列末点判定 → (state, ma_now, slope)。

    state=None 表示样本不足门槛 (短样本不冒充), 此时 ma/slope 为 nan。
    调用方 (如市场体检表) 自行把 None 渲染成【缺】。
    """
    c = pd.Series(closes)
    ma, slope = _ma_and_slope(c)
    ma_now, sl = float(ma.iloc[-1]), float(slope.iloc[-1])
    price = float(c.iloc[-1])
    if ma_now != ma_now or sl != sl:   # NaN → 数据不够长
        return None, ma_now, sl
    if price > ma_now and sl > 0:
        return BULL, ma_now, sl
    if price < ma_now and sl < 0:
        return BEAR, ma_now, sl
    return RANGE, ma_now, sl


def regime_series(closes) -> pd.Series:
    """全序列标签 (历史回填用, research/index_regime.py 消费)。

    样本不足处标 range (中性默认, NaN 比较为 False, 与 research 版
    旧行为逐字节一致); 末点与 classify() 结论一致。
    """
    c = pd.Series(closes).astype(float)
    ma, slope = _ma_and_slope(c)
    regime = pd.Series(RANGE, index=c.index)
    regime[(c > ma) & (slope > 0)] = BULL
    regime[(c < ma) & (slope < 0)] = BEAR
    return regime
