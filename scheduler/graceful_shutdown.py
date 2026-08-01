"""scheduler/graceful_shutdown.py — SIGTERM/SIGINT → threading.Event 置位。

用法: ev = install() 之后, 主循环等 ev.wait() 或在循环里查 ev.is_set(),
收到退出信号即优雅停机 (而不是被 KeyboardInterrupt 撕碎)。

松耦合: 信号注册只能在主线程; 非主线程调用时记 warning 降级
(返回的 Event 仍可用, 只是不会收到信号)。
"""
from __future__ import annotations

import signal
import threading

from utils.logger import get_logger

_logger = get_logger("scheduler.graceful_shutdown")


def install() -> threading.Event:
    """注册 SIGTERM/SIGINT 处理器, 返回停机 Event。收到信号时 Event 置位。"""
    stop_event = threading.Event()

    def _handler(signum, _frame):
        _logger.info("收到退出信号 (%s), 置停机标志", signum)
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError) as e:
        # 非主线程/平台不支持: 降级为"只返回 Event", 不抛异常
        _logger.warning("信号处理器注册失败 (非主线程?), 降级为手动停机: %s", e)
    return stop_event
