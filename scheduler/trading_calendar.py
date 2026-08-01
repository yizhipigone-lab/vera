"""scheduler/trading_calendar.py — A 股交易日判断 (上交所历)。

优先使用 exchange_calendars ("XSHG") 精确历; 该库缺失时降级为
"周一~周五 + 内置 2026 年法定节假日表" 的近似历, 并记 warning
(降级精度有限: 不含调休上班的周六日以外的临时休市, 且只覆盖 2026 年;
`pip install exchange-calendars` 可得精确版)。

松耦合铁律: 任何外部失败 (库缺失/历查询异常) 都回落到近似历,
绝不向调用方抛异常。
"""
from __future__ import annotations

import datetime as dt

from utils.logger import get_logger

_logger = get_logger("scheduler.trading_calendar")

# ── 可选精确历 (exchange_calendars) ──────────────────────────

_XCAL = None          # 已加载的 XSHG 历; None = 不可用/未加载
_XCAL_TRIED = False   # 只尝试加载一次, 避免每个判断都走 import


def _load_xshg():
    """惰性加载 exchange_calendars 的 XSHG 历。失败返 None 并记 warning。"""
    global _XCAL, _XCAL_TRIED
    if _XCAL_TRIED:
        return _XCAL
    _XCAL_TRIED = True
    try:
        import exchange_calendars as xcals
        _XCAL = xcals.get_calendar("XSHG")
    except Exception as e:  # 松耦合: 库缺失/版本不兼容都降级
        _logger.warning(
            "exchange_calendars 不可用, 交易日判断降级为内置 2026 假日表 "
            "(精度有限, pip install exchange-calendars 可得精确版): %s", e)
        _XCAL = None
    return _XCAL


# ── 降级用内置假日表 (2026 年, 以国务院公告为准) ─────────────
# 精度说明: 只覆盖 2026 年法定节假日。A 股周末一律休市 (即使国务院
# 调休把某个周末调成工作日, 沪深交易所也不开市), 故降级历只需
# "周一~周五 + 假日表", 无需调休上班日补丁。

_HOLIDAYS_2026: frozenset[dt.date] = frozenset(
    dt.date.fromisoformat(s)
    for lo, hi in [
        ("2026-01-01", "2026-01-03"),  # 元旦
        ("2026-02-15", "2026-02-23"),  # 春节
        ("2026-04-04", "2026-04-06"),  # 清明
        ("2026-05-01", "2026-05-05"),  # 劳动节
        ("2026-06-19", "2026-06-21"),  # 端午
        ("2026-09-25", "2026-09-27"),  # 中秋
        ("2026-10-01", "2026-10-08"),  # 国庆
    ]
    for n in range((dt.date.fromisoformat(hi) - dt.date.fromisoformat(lo)).days + 1)
    for s in [str(dt.date.fromisoformat(lo) + dt.timedelta(days=n))]
)

def _is_trading_day_fallback(d: dt.date) -> bool:
    """降级近似历: 周一~周五且非法定假日 (A 股周末一律休市)。"""
    if d.weekday() >= 5:
        return False
    return d not in _HOLIDAYS_2026


def is_trading_day(d: dt.date) -> bool:
    """判断是否 A 股交易日。任何异常都降级到近似历, 不抛异常。"""
    cal = _load_xshg()
    if cal is not None:
        try:
            import pandas as pd
            return bool(cal.is_session(pd.Timestamp(d)))
        except Exception as e:  # 松耦合: 历查询异常同样降级
            _logger.warning("exchange_calendars 查询异常, 降级为内置假日表: %s", e)
    return _is_trading_day_fallback(d)


def next_trading_day(d: dt.date) -> dt.date:
    """d (不含) 之后的第一个交易日。最多向后找 370 天, 防假日表外的死循环。"""
    cur = d + dt.timedelta(days=1)
    for _ in range(370):
        if is_trading_day(cur):
            return cur
        cur += dt.timedelta(days=1)
    # 理论上不可达 (降级历下每周必有工作日); 兜底返回并记 warning
    _logger.warning("next_trading_day 向后 370 天未找到交易日, 返回 %s (异常兜底)", cur)
    return cur
