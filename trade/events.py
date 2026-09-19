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
EVENT_ROTATION = "rotation"                # ETF 轮动信号结果 (工作线程→消费者, 2026-08-14)
EVENT_CONNECTION_LOST = "connection_lost"  # 断线 (回调或心跳双检测)
EVENT_SYNC_REPORTS = "sync_reports"        # 增量同步 (成交补记+委托回写)
EVENT_ORDER_ERROR = "order_error"          # 下单失败回报 (拒单原因, 2026-08-07)
EVENT_CANCEL_ERROR = "cancel_error"        # 撤单失败回报 (2026-08-07)
# 2026-09-19 批次 4.1: HTTP 线程的只读查询入队 (资产/持仓类) —— 消费者线程执行,
# 结果经 concurrent.futures.Future 回给 HTTP 线程。修"HTTP 线程直调 gateway
# 同步查询, 与消费者线程并发打 xtquant" (架构审查 P0-2)。
EVENT_READ_QUERY = "read_query"

# 2026-08-01 M1: 关键事件类型 —— 队列满时优先保留, tick/快照可驱逐
_CRITICAL_TYPES = frozenset({
    EVENT_RECONCILE, EVENT_SYNC_REPORTS, EVENT_TIMER_SCAN,
    EVENT_COMMAND, EVENT_SIGNALS, EVENT_ROTATION, EVENT_CONNECTION_LOST,
    EVENT_EOD, EVENT_ORDER_ERROR, EVENT_CANCEL_ERROR,
    # 只读查询也按关键处理: 被丢弃会让 HTTP 线程白等到超时 (有 caller 在等)。
    # 2026-09-20 审计 P2-1: read_via_consumer 现在自己带预算 (put 传显式
    # timeout), 这里只影响"丢弃时按关键留痕", 不再决定等待时长。
    EVENT_READ_QUERY,
})

#: 2026-09-19 批次 4.2: "等待回报期间可就地消费"的事件类型。
#: rotation 等成交 / executor 等撤单 ack 原先在消费者线程里 sleep 轮询 (最长
#: 3 秒/笔), 阻塞期间队列里的回报与行情全排队。现在等待期间就地分发这两类
#: **叶子 handler**:
#:   - 回报类 (委托回报/成交/报错): 等待的目标本身, 就地应用才不用干等;
#:   - 行情类 (tick/快照): monitor.on_quote 只更新报价缓存与心跳, 不下单、
#:     不重入任何特性层 —— 就地处理让"等 3 秒"期间报价保持新鲜。
#: **明确排除** 命令/信号/轮动/扫描/对账/EOD: 它们会重入特性层 (可能下单),
#: 在等待中间插进来会把状态机打断 —— 那些留待正常轮次。
PUMPABLE_WAIT_TYPES = frozenset({
    EVENT_ORDER_UPDATE, EVENT_TRADE_FILL,
    EVENT_ORDER_ERROR, EVENT_CANCEL_ERROR,
    EVENT_TICK, EVENT_QUOTE_SNAPSHOT,
})


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
        #: 消费者线程 ident (2026-09-19 批次 4.2): pump() 只许消费者线程调,
        #: 错线程调用会让"唯一写者"变成两个线程同时跑 handler (铁律 3 破口)
        self._consumer_ident: int | None = None

    def put(self, event: Event, timeout: float | None = None) -> bool:
        """生产侧唯一入口。回调线程里只许调这个 (铁律 2)。

        2026-08-01 M1: 关键事件 (对账/同步/扫描/命令/信号/断线/EOD)
        用更长超时 (5s), 宁可回调线程多等, 不可丢对账/同步/扫描事件
        (07-31 实测: reconcile 1 + sync_reports 31 + timer_scan 548 被丢);
        tick/快照/委托/成交仍用短超时 —— 量大可丢弃, 对账兜底补。

        2026-09-20 审计 P2-1: 加 `timeout` 显式覆盖 + 返回是否入队成功。
        只读查询 (TradeApp.read_via_consumer) 的等待方有自己的一整个超时
        预算, 不能被 put 的关键事件 5s 超时吃掉 (5s + Future 2s = HTTP 线程
        实测卡 ~7s, 远超它对调用方承诺的 timeout)。返回 False = 已丢弃
        (照旧告警 + audit 留痕), 调用方据此立刻失败, 而不是白等到超时。
        """
        is_critical = event.type in _CRITICAL_TYPES
        if timeout is None:
            timeout = 5.0 if is_critical else self._put_timeout
        try:
            self._queue.put(event, timeout=timeout)
            return True
        except queue.Full:
            _logger.error("事件队列满 (%d), 丢弃%s事件: %s",
                          self._queue.maxsize,
                          "关键" if is_critical else "",
                          event.type)
            if self._audit_sink:
                try:
                    self._audit_sink("event_dropped",
                                     f"事件队列满, 丢弃 {'关键' if is_critical else ''}{event.type}",
                                     {"type": event.type, "critical": is_critical})
                except Exception:
                    pass  # audit 也失败不能再炸回调线程
            return False

    def start(self) -> None:
        """启动消费者线程。重复调用是 no-op, 防止起出第二个写者。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="trade-event-consumer", daemon=True,
        )
        self._thread.start()

    def can_pump(self) -> bool:
        """当前线程能不能调 pump() (2026-09-19 批次 4.2)。

        调用方 (rotation 等成交 / executor 等 ack) 用它决定"就地消费回报"
        还是"退回纯 sleep": 生产走消费者线程 → True; 单测直接构造特性、在
        测试线程里跑 → False, 行为与改造前一致 (不抛错, 不假装能 pump)。
        """
        return (self._consumer_ident is not None
                and threading.get_ident() == self._consumer_ident)

    def pump(self, types: frozenset[str], duration: float,
             max_events: int = 200) -> int:
        """在 handler 内部"顺手消费"指定类型的事件 (2026-09-19 批次 4.2)。

        用途: rotation 等成交 / executor 等撤单 ack 期间, 不再是"死等" ——
        等待窗口内到达的**回报类**事件就地分发 (回调线程 put 进来的正是它们),
        其余事件原样放回队列尾, 留待正常轮次。

        约束 (都是安全阀, 不是装饰):
          - **只许消费者线程调用**: 错线程调用 = 两个线程同时跑 handler,
            破"唯一写者"铁律 → 直接抛 RuntimeError;
          - **types 白名单由调用方给**: 只该放叶子 handler 类 (见
            PUMPABLE_WAIT_TYPES: 回报 + 行情),
            放进命令/信号类会让特性层在等待中间被重入;
          - 被放回的事件会排到队尾 (相对顺序在它们彼此之间保持): 这不改变
            语义 —— tick 后到的覆盖先到的, 定时扫描/对账与回报无先后依赖。

        **窗口语义 (2026-09-20 审计 P2-2): duration 是"至少"不是"至多" ——
        实际耗时 ≤ duration + 单个 handler 的执行时间。** 截止时间只在循环入口
        判 (handler 一旦开始就不能被打断), 所以别拿它当硬实时期限用; 拿它当
        "等待期间顺手消费"即可。此前只在 dispatch 之后判截止, 一个 1s 的慢
        handler 会把 0.2s 的窗口拖成 1.06s 并再多消费一个事件。

        返回就地消费的事件条数。
        """
        if not self.can_pump():
            raise RuntimeError(
                "pump() 只许消费者线程调用 (错线程会破'唯一写者'铁律); "
                "调用前先问 can_pump()")
        import time as _t
        deadline = _t.monotonic() + max(0.0, duration)
        consumed = 0
        held: list[Event] = []
        try:
            while consumed < max_events:
                # 2026-09-20 审计 P2-2: 截止时间在**循环入口**判 (这是唯一的
                # 截止判定点) —— 唯一判定+入口判定, 保证"过了截止不再起新的
                # dispatch"且空队列时不会无限自旋。
                if _t.monotonic() >= deadline:
                    break
                try:
                    ev = self._queue.get_nowait()
                except queue.Empty:
                    _t.sleep(0.005)     # 短睡让出 CPU, 不是原来那种整段死等
                    continue
                if ev.type in types:
                    self._dispatch(ev)
                    consumed += 1
                else:
                    held.append(ev)
        finally:
            # 放回: 队列在等待期间可能被生产者填满 → 与 put() 同纪律 (告警留痕)
            for ev in held:
                try:
                    self._queue.put_nowait(ev)
                except queue.Full:
                    _logger.error("pump 放回事件失败 (队列满), 丢弃: %s", ev.type)
                    if self._audit_sink:
                        try:
                            self._audit_sink("event_dropped",
                                             f"pump 放回失败, 丢弃 {ev.type}",
                                             {"type": ev.type})
                        except Exception:
                            pass
        return consumed

    def _dispatch(self, event: Event) -> None:
        """分发一个事件 (pump 与消费循环共用, 保证异常语义一致)。"""
        handler = self._handlers.get(event.type)
        if handler is None:
            # 无订阅者的事件 = 接线错误, 必须留痕而不是静默吞掉
            _logger.warning("事件无订阅者, 已丢弃: %s", event.type)
            return
        try:
            handler(event)
        except Exception:
            _logger.exception("事件处理异常 (引擎继续运行): %s", event.type)

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
        self._consumer_ident = threading.get_ident()
        try:
            while not self._stop.is_set():
                try:
                    event = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                self._dispatch(event)
        finally:
            self._consumer_ident = None
