"""trade/raw_log.py — JSONL 原始回报异步写盘 (治理III W4-f 自 trade/store.py 端出)。

2026-08-06 审计 HIGH#3 异步化落盘: 回调线程只 put_nowait (微秒级, 无锁无磁盘
IO) —— 修掉"回调线程持 store._lock 同步 write+flush"的铁律 2 违反 (tick 暴雨
或磁盘抖动不再堵住网关回调与消费者记账)。本模块是 TradeStore 内的独立小深模块
(3 方法藏一整条写盘流水线), 治理时整体迁出为独立文件; store.py 顶部 re-export
`_RawLogWriter` 保持既有 `from trade.store import _RawLogWriter` 引用零改动。
"""
from __future__ import annotations

import queue
import threading
import time

from utils.logger import get_logger

_logger = get_logger("trade.raw_log")


class _RawLogWriter:
    """JSONL 异步写盘 (2026-08-06 审计 HIGH#3)。

    回调线程只 put_nowait (微秒级, 无锁无磁盘 IO) —— 修掉"回调线程持
    store._lock 同步 write+flush"的铁律 2 违反: tick 暴雨或磁盘抖动
    (Windows 杀软扫盘) 不再堵住网关回调与消费者记账。

    语义边界 (与 M10 定位一致, JSONL 仅审计留痕):
    - 崩溃丢失窗口 ≤ flush_interval 的尾部未落盘批次, 可接受
    - 正常退出经 shutdown() drain: 队列清空 + 终 flush, 一条不丢
    - 队列满 (磁盘卡死) → 丢弃 + dropped 计数 + 节流告警, 绝不阻塞回调
    - writer 线程死亡 → put 时探活, error 告警一次 (不降级回同步写,
      避免把磁盘 IO 引回回调线程)
    """

    _SENTINEL = object()

    def __init__(self, fp, flush_interval: float = 0.5, maxsize: int = 100_000):
        self._fp = fp
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._flush_interval = flush_interval
        self.dropped = 0   # 队列满丢弃计数
        self.errors = 0    # 写盘/flush 异常计数
        self._dead_warned = False
        self._t = threading.Thread(
            target=self._loop, name="raw-log-writer", daemon=True)
        self._t.start()

    def put(self, line: str) -> None:
        if not self._t.is_alive() and not self._dead_warned:
            self._dead_warned = True
            _logger.error("raw writer 线程未存活, 审计日志中断 (不影响交易)")
        try:
            self._q.put_nowait(line)
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 10000 == 0:
                _logger.warning(
                    "raw 日志队列满 (磁盘卡住?), 累计丢弃 %d 条", self.dropped)

    def flush(self, timeout: float = 5.0) -> bool:
        """阻塞至队列清空且已落盘 (测试接缝 / close 前 drain)。True=完成。"""
        ev = threading.Event()
        try:
            self._q.put(ev, timeout=timeout)
        except queue.Full:
            return False
        return ev.wait(timeout)

    def shutdown(self, timeout: float = 5.0) -> None:
        """sentinel 停线程 + drain + 终 flush; 队列满则重试至超时。"""
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._q.put(self._SENTINEL, timeout=0.1)
                break
            except queue.Full:
                if time.monotonic() >= deadline:
                    break
        self._t.join(max(0.0, deadline - time.monotonic()) + 1.0)

    def _write_safe(self, line: str) -> None:
        try:
            self._fp.write(line)
        except Exception as e:
            self.errors += 1
            if self.errors == 1 or self.errors % 100 == 0:
                _logger.error("raw 日志写盘异常 (累计 %d): %s", self.errors, e)

    def _flush_safe(self) -> None:
        try:
            self._fp.flush()
        except Exception as e:
            self.errors += 1
            _logger.error("raw 日志 flush 异常: %s", e)

    def _handle(self, item) -> bool:
        """处理一个队列项, 返回 True = 收到关闭信号。"""
        if item is self._SENTINEL:
            return True
        if isinstance(item, threading.Event):
            self._flush_safe()   # flush 请求: 落盘已完成批次后放行
            item.set()
        else:
            self._write_safe(item)
        return False

    def _loop(self) -> None:
        stopping = False
        while True:
            try:
                item = self._q.get(timeout=self._flush_interval)
            except queue.Empty:
                self._flush_safe()   # 空闲兜底: 崩溃丢失窗口 ≤ flush_interval
                continue
            stopping = self._handle(item)
            # 批量 drain: 积攒的一波一次写完, 只做一次 flush
            while True:
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    break
                if self._handle(item):
                    stopping = True
            self._flush_safe()
            if stopping:
                return
