"""回测引擎纯数据常量 (P2-6, 2026-07-15; H-2 MappingProxyType, 2026-07-15).

之前散在 BacktestEngine 类上的 BARS_PER_DAY / PERIODS_PER_YEAR 是纯数据字典,
没有依赖任何运行时状态, 但被 benchmark.py 引用时被迫 import 整个 engine 模块
(→ 拖入 connector / data_fetcher / numpy/pandas 等一长串依赖)。

提取到独立模块后:
- engine.py 仍可通过 BacktestEngine.PERIODS_PER_YEAR 访问 (向后兼容)
- benchmark.py 等"只想要数据"的调用方无需引入整条引擎依赖链

H-2 (2026-07-15): 用 MappingProxyType 包裹, 只读暴露。防止未来某个模块
意外修改 dict 影响所有持有同一引用的模块。
"""
from types import MappingProxyType


# BARS_PER_DAY: 一个交易日内的 bar 数 (用于 max_hold_days / days 的 bar 缩放)
_BARS_PER_DAY = {
    "1d": 1,
    "1w": 1,
    "5m": 48,   # A股 5m = 4 小时 / 5分钟 = 48 根/日
}

# PERIODS_PER_YEAR: 不同周期的年化基数 (用于 annualized_return / 几何年化)
#   - 1d: 252 个交易日/年 (A股惯例)
#   - 1w: 52 周/年 (避免被 *252 高估 4.8 倍 — P1-4 历史教训)
#   - 5m: 48 根/日 × 252 日 = 12096 根/年
_PERIODS_PER_YEAR = {
    "1d": 252,
    "1w": 52,
    "5m": 48 * 252,
}

BARS_PER_DAY = MappingProxyType(_BARS_PER_DAY)
PERIODS_PER_YEAR = MappingProxyType(_PERIODS_PER_YEAR)