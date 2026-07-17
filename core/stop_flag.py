"""协作式停止标志 (2026-07-17)。

背景: 前端「停止回测」按钮需要中断正在运行的回测。Python 线程不能强杀,
唯一安全做法是各耗时循环主动检查 threading.Event 并抛出 BacktestStoppedError。

使用方式:
  - server.py /api/stop  → request_stop()
  - server.py /api/run 开头 → clear_stop() (清掉上一次残留的停止标志)
  - 耗时循环 (数据拉取 / 回测逐 bar) → raise_if_stopped()

注意: server 是常驻进程且单回测 (running 时 /api/run 返回 409), 全局单例够用。
批量脚本从不调用 request_stop, 标志恒为未置位, 行为完全不变 (向后兼容)。
"""

import threading

_stop_event = threading.Event()


class BacktestStoppedError(Exception):
    """用户点击「停止回测」, 从耗时循环中抛出以中断管线。"""


def request_stop() -> None:
    """请求停止当前回测 (幂等)。"""
    _stop_event.set()


def clear_stop() -> None:
    """清除停止标志。每次新回测开始前必须调用。"""
    _stop_event.clear()


def stop_requested() -> bool:
    """是否已请求停止。is_set() 开销极小, 可在逐 bar 热循环里每轮调用。"""
    return _stop_event.is_set()


def raise_if_stopped() -> None:
    """若已请求停止则抛 BacktestStoppedError。"""
    if _stop_event.is_set():
        raise BacktestStoppedError("用户手动停止回测")
