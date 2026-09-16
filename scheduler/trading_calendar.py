"""scheduler/trading_calendar.py — A 股交易日判断 (上交所历)。

优先使用 exchange_calendars ("XSHG") 精确历; 该库缺失时降级为
"周一~周五 + 内置 2026 年法定节假日表" 的近似历, 并记 warning。

降级精度有限, 且**内置表只覆盖 2026 年**: 2027 年及以后的放假安排由国务院
当年公告 (通常当年 11~12 月发布), 公告前写不出准表, 表外日期只能按
"周一~周五"粗判 —— 落在工作日里的法定假日会被误判成交易日。

⚠ 2026-09-16 实测: **装上 exchange_calendars 也只到 2026-12-31** —— 该库
(4.13.2) 的 XSHG 历自己就报 "The XSHG holidays are only recorded to the
year 2026" (上游同样在等公告)。所以"装库"不等于"覆盖未来"; 问"某天的判断
可不可信"一律用 `calendar_covers(d)` (它比较精确历的**实际覆盖区间**与内置表
的覆盖区间), 不要用 `precise_calendar_available()` 代替。
需要严格判据的调用方 (例如"盘后要求当日日线") 在不可信时应放宽判据或降级,
而不是照严格判据一直等一个永远不会来的数据。
2026 年 12 月公告出来后二选一即可恢复覆盖: 升级库 (上游更新 XSHG 历), 或把
内置表补到 2027 年。

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


# 内置假日表的覆盖区间 (闭区间)。表外日期只能按"周一~周五"粗判 ——
# 法定假日会误判成交易日, 故需要严格判据的调用方先问 calendar_covers()。
_HOLIDAY_TABLE_FIRST = dt.date(2026, 1, 1)
_HOLIDAY_TABLE_LAST = dt.date(2026, 12, 31)


def _xcal_covers(cal, d: dt.date) -> bool:
    """精确历是否覆盖这一天 (闭区间比较, 取 .date() 免时区/类型纠缠)。"""
    try:
        return bool(cal.first_session.date() <= d <= cal.last_session.date())
    except Exception:            # noqa: BLE001  历对象异常 → 当不覆盖处理
        return False


def precise_calendar_available() -> bool:
    """exchange_calendars 精确历 (XSHG) 是否**加载成功** (不代表覆盖目标日期)。

    进程内**首次尝试即定死** (见 `_load_xshg` 的 `_XCAL_TRIED` 闩: 失败也置闩,
    之后不再重试) —— 所以运行中把库装上不会自动生效, 必须重启进程。
    要问"某一天的判断可不可信", 用 `calendar_covers(d)`, 不要用本函数。
    """
    return _load_xshg() is not None


def calendar_covers(d: dt.date) -> bool:
    """这一天的交易日判断是否**可信**: 精确历覆盖该日, 或内置假日表覆盖该日。

    注意 (2026-09-16 实测): 装上 exchange_calendars **并不等于**覆盖未来 ——
    4.13.2 的 XSHG 历只到 2026-12-31 (2027 年放假安排国务院尚未公告, 上游也
    没有数据), 所以必须按"实际覆盖区间"判断, 不能只看库在不在。
    返回 False 只表示"只能按周一~周五粗判" (认不出表外年份的法定假日),
    不代表结果一定错; 调用方据此放宽严格判据或降级, 而不是拒绝工作。
    """
    cal = _load_xshg()
    if cal is not None and _xcal_covers(cal, d):
        return True
    return _HOLIDAY_TABLE_FIRST <= d <= _HOLIDAY_TABLE_LAST


def is_trading_day(d: dt.date) -> bool:
    """判断是否 A 股交易日。任何异常都降级到近似历, 不抛异常。

    日期超出精确历覆盖区间 (2026-09-16 实测 exchange_calendars 4.13.2 的 XSHG
    历只到 2026-12-31, 库自己会抛 "holidays are only recorded to the year
    2026") 时**静默走内置表** —— 先前是先调用再捕获异常, 表外日期会每次调用
    刷一条 WARNING。是否可信由 `calendar_covers` 回答, 不信时由调用方放宽判据。
    """
    cal = _load_xshg()
    if cal is not None and _xcal_covers(cal, d):
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


def month_grid(year: int, month: int) -> dict:
    """月历网格 (server /api/calendar 数据源) — 2026-09-15 自路由下沉纯函数。

    超出精确历覆盖 (2026-12-31 后) 降级为周末规则, 法定节假日不再可辨。
    输出 {"year","month","trading_calendar": {YYYY-MM-DD: {is_trading, weekday}},
    "data_source"}。
    """
    import calendar as _cal
    now = dt.date.today()
    y = year if year > 0 else now.year
    m = month if 1 <= month <= 12 else now.month
    result = {}
    for d in range(1, _cal.monthrange(y, m)[1] + 1):
        date_str = f"{y}-{m:02d}-{d:02d}"
        day = dt.date(y, m, d)
        result[date_str] = {
            "is_trading": bool(is_trading_day(day)),
            "weekday": day.weekday(),  # 0=Mon
        }
    return {"year": y, "month": m, "trading_calendar": result,
            "data_source": "xshg_precise"}
