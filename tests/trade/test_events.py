"""EventEngine 单元测试 (P1 地基, 2026-07-26).

锁住三件事: 静态接线 (构造即全部订阅者)、handler 异常不杀死
消费者线程、stop 干净退出 (线程 join)。
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.events import (
    EVENT_ORDER_UPDATE,
    EVENT_TICK,
    Event,
    EventEngine,
)


def _wait_until(pred, timeout=2.0):
    """轮询等待条件成立, 超时返回 False (测试里不 sleep 死等)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_static_wiring_dispatches_to_registered_handler():
    """构造函数传入的 handler 收到对应类型事件。"""
    received = []
    engine = EventEngine({EVENT_TICK: lambda e: received.append(e)})
    engine.start()
    try:
        engine.put(Event(type=EVENT_TICK, data={"last": 10.5}))
        assert _wait_until(lambda: len(received) == 1)
        assert received[0].data == {"last": 10.5}
    finally:
        engine.stop()


def test_unsubscribed_event_dropped_not_crash():
    """无订阅者的事件被丢弃, 引擎与已订阅事件都不受影响。"""
    received = []
    engine = EventEngine({EVENT_TICK: lambda e: received.append(e)})
    engine.start()
    try:
        engine.put(Event(type=EVENT_ORDER_UPDATE, data={}))  # 无订阅者
        engine.put(Event(type=EVENT_TICK, data=1))
        assert _wait_until(lambda: len(received) == 1)
    finally:
        engine.stop()


def test_handler_exception_does_not_kill_consumer():
    """handler 抛异常后, 后续事件仍被正常消费 (唯一写者不许死)。"""
    received = []

    def bad_handler(e):
        if e.data == "boom":
            raise RuntimeError("模拟 handler 崩溃")
        received.append(e.data)

    engine = EventEngine({EVENT_TICK: bad_handler})
    engine.start()
    try:
        engine.put(Event(type=EVENT_TICK, data="boom"))
        engine.put(Event(type=EVENT_TICK, data="after"))
        assert _wait_until(lambda: received == ["after"])
    finally:
        engine.stop()


def test_stop_joins_thread_cleanly():
    """stop 后消费者线程退出, 非僵尸。"""
    engine = EventEngine({EVENT_TICK: lambda e: None})
    engine.start()
    thread = engine._thread
    assert thread is not None and thread.is_alive()
    engine.stop()
    assert not thread.is_alive()


def test_stop_without_start_is_safe():
    """未 start 直接 stop 不抛异常。"""
    engine = EventEngine({})
    engine.stop()  # 无异常即 PASS


def test_double_start_keeps_single_consumer():
    """重复 start 是 no-op —— 绝不许起出第二个写者线程。"""
    engine = EventEngine({EVENT_TICK: lambda e: None})
    engine.start()
    first = engine._thread
    engine.start()
    assert engine._thread is first
    engine.stop()


def test_serial_order_preserved():
    """单消费者串行: 事件按入队顺序处理。"""
    received = []
    engine = EventEngine({EVENT_TICK: lambda e: received.append(e.data)})
    engine.start()
    try:
        for i in range(50):
            engine.put(Event(type=EVENT_TICK, data=i))
        assert _wait_until(lambda: len(received) == 50)
        assert received == list(range(50))
    finally:
        engine.stop()


# ═══════════════════════════════════════════════════════════════
# 审计M4/L1/L2 修复
# ═══════════════════════════════════════════════════════════════

def test_bounded_queue_full_drops_with_audit():
    """审计M4修复: 队列满 → 告警 + audit 留痕 + 丢弃 (不阻塞回调线程)。"""
    audits = []
    engine = EventEngine(
        {EVENT_TICK: lambda e: None}, maxsize=3, put_timeout_sec=0.01,
        audit_sink=lambda k, m, d: audits.append((k, m)))
    for i in range(3):
        engine.put(Event(type=EVENT_TICK, data=i))   # 塞满 (未 start)
    engine.put(Event(type=EVENT_TICK, data="overflow"))
    assert audits and audits[0][0] == "event_dropped"
    assert engine._queue.qsize() == 3                # 未挤入


def test_stop_drains_queue_before_stopping():
    """审计L1修复: stop 先把队列消费完 (限时) 再停, 不丢 in-flight 事件。"""
    received = []
    engine = EventEngine({EVENT_TICK: lambda e: (
        time.sleep(0.01), received.append(e.data))})
    engine.start()
    for i in range(20):
        engine.put(Event(type=EVENT_TICK, data=i))
    engine.stop()   # drain 应保证 20 个全消费
    assert len(received) == 20


def test_stop_join_timeout_keeps_thread_reference():
    """审计L2修复: join 超时线程仍 alive → 不置 None (防起第二个写者)。"""
    gate = threading.Event()
    engine = EventEngine({EVENT_TICK: lambda e: gate.wait(2.0)})
    engine.start()
    engine.put(Event(type=EVENT_TICK, data="block"))  # handler 阻塞 2s
    assert _wait_until(lambda: engine._queue.empty())  # 已被取出在跑
    engine.stop(join_timeout_sec=0.1)
    assert engine._thread is not None                  # 引用保留
    assert engine._thread.is_alive()
    engine.start()                                     # 被 is_alive 挡住
    gate.set()                                         # 放行让线程自然死
    assert _wait_until(lambda: not engine._thread.is_alive())
