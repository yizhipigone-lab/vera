# -*- coding: utf-8 -*-
"""scheduler/trading_calendar.py — 兼容 shim (2026-09-19 架构修订批次 1.2)。

真身已搬到 `utils/trading_calendar.py` —— 它是零 scheduler 依赖的纯日历工具,
却曾被 core/(3 处) 和 trade/(6 处) 向上 import, 层级倒挂。本文件只做 re-export
兼容旧引用, 一个版本后删除; **新代码一律 import utils.trading_calendar**。
"""
from utils.trading_calendar import (  # noqa: F401
    calendar_covers,
    is_trading_day,
    month_grid,
    next_trading_day,
    precise_calendar_available,
)
