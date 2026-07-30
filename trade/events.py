"""trade/events.py — 单消费者事件引擎 (vnpy EventEngine 模式)。

设计意图:
    线程模型的物理载体: 所有交易状态只有一个写者 —— 本引擎的消费者线程。
    xtquant 回调线程 / 行情线程 / 定时器 / Web 命令一律 put 进队列,
    串行消费, 业务代码因此零锁。

    静态接线 (计划书 §三): handlers 在构造函数一次性传入,
    无 register/unregister —— 订阅者集合翻一个文件即知,
    杜绝"忘订阅静默失败"。订阅侧不得回发事件 (防事件链)。
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from utils.logger import get_logger

_logger = get_logger("trade.events")

# ═══════════════════════════════════════════════════════════════
# 事件类型常量 — 全项目只此一份 (计划书 §三反模式红线)
# ═══════════════════════════════════════════════════════════════

EVENT_ORDER_UPDATE = "order_update"        # 委托状态回报
EVENT_TRADE_FILL = "trade_fill"            # 成交回报
EVENT_TICK = "tick"                        # 行情 tick 推送
EVENT_QUOTE_SNAPSHOT = "quote_snapshot"    # 轮询兜底的全景快照
EVENT_TIMER_SCAN = "timer_scan"            # 监控腿定时扫描
EVENT_RECONCILE = "reconcile"              # 定时/收盘对账触发
EVENT_EOD = "eod"                          # 收盘归档
EVENT_COMMAND = "command"                  # Web/CLI 人工命令
EVENT_SIGNALS = "signals"                  # 尾盘选股结果 (工作线程→消费者)
EVENT_CONNECTION_LOST = "connection_lost"  # 断线 (回调或心跳双检测)


@dataclass(frozen=True)
class Event:
    """队列里流动的最小单元。ts 由生产侧打点, 便于事后复盘时序。"""

    type: str
    data: Any = None
    ts: float = field(default_factory=time.time)


class EventEngine:
    """queue.Queue + 单消费者线程。公开接口: put / start / stop。

    审计M4修复: 队列有界 (默认 10000)。断线重连占住消费者线程期间
    定时器照常 put, 无界队列会让积压事件在重连后全部补跑 (陈旧
    tick 盖心跳/驱动评估)。满时策略 (任务书裁决的简单策略):
    put 阻塞超时后告警 + 记 audit + 丢弃 —— 宁可丢事件留痕,
    不可让回调线程 (xtquant 侧) 阻塞出连锁故障。
    """

    def __init__(self, handlers: dict[str, Callable[[Event], None]],
                 maxsize: int = 10000, put_timeout_sec: float = 1.0,
                 audit_sink: Callable[[str, str, dict], None] | None = None):
        # 复制一份, 构造后外部再改 dict 不影响引擎 —— 静态接线的字面含义
        self._handlers: dict[str, Callable[[Event], None]] = dict(handlers)
        self._queue: queue.Queue[Event] = queue.Queue(maxsize=maxsize)
        self._put_timeout = put_timeout_sec
        self._audit_sink = audit_sink
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def put(self, event: Event) -> None:
        """生产侧唯一入口。回调线程里只许调这个 (铁律 2)。
        队列满: 阻塞至超时后丢弃 + 告警 + audit (事件可丢, 痕迹不可丢)。"""
        try:
            self._queue.put(event, timeout=self._put_timeout)
        except queue.Full:
            _logger.error("事件队列满 (%d), 丢弃事件: %s",
                          self._queue.maxsize, event.type)
            if self._audit_sink:
                try:
                    self._audit_sink("event_dropped",
                                     f"事件队列满, 丢弃 {event.type}",
                                     {"type": event.type})
                except Exception:
                    pass  # audit 也失败不能再炸回调线程

    def start(self) -> None:
        """启动消费者线程。重复调用是 no-op, 防止起出第二个写者。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="trade-event-consumer", daemon=True,
        )
        self._thread.start()

    def stop(self, drain_timeout_sec: float = 2.0,
             join_timeout_sec: float = 5.0) -> None:
        """审计L1/L2修复:
        - 先 drain: 限时等队列消费完再置停止标志 (docstring 承诺
          "不丢 in-flight 状态写", 旧实现直接丢弃残留事件);
        - join 超时线程仍 alive → 不置 None + ERROR 日志: 此时再 start
          会被 is_alive 挡住, 防止 handler 卡死时起出第二个写者 (铁律 3)。
        """
        deadline = time.time() + drain_timeout_sec
        while not self._queue.empty() and time.time() < deadline:
            time.sleep(0.01)
        if not self._queue.empty():
            _logger.error("stop drain 超时, 队列残留 %d 个事件被丢弃",
                          self._queue.qsize())
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout_sec)
            if self._thread.is_alive():
                _logger.error("消费者线程 join 超时仍存活 (handler 卡死?), "
                              "保留引用防止起第二个写者")
            else:
                self._thread = None

    def _run(self) -> None:
        """消费循环: handler 异常必须捕获, 绝不让唯一写者死掉。"""
        while not self._stop.is_set():
            try:
                event = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            handler = self._handlers.get(event.type)
            if handler is None:
                # 无订阅者的事件 = 接线错误, 必须留痕而不是静默吞掉
                _logger.warning("事件无订阅者, 已丢弃: %s", event.type)
                continue
            try:
                handler(event)
            except Exception:
                _logger.exception("事件处理异常 (引擎继续运行): %s", event.type)
