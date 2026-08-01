"""scheduler/ — 政策研究平台的定时调度层 (独立进程, 不碰 VERA 主程序)。

设计意图:
    交易日常识 (trading_calendar) + 轻量定时器 (vera_scheduler) +
    优雅停机 (graceful_shutdown) 三件套, 全部 stdlib 实现, 零新增硬依赖。
    exchange_calendars 只做可选 import, 缺失时降级为内置假日表。

    本 __init__ 刻意不做任何 eager import —— 各模块由入口
    (scheduler/__main__.py 或调用方) 显式组装。
"""
