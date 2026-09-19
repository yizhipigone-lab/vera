# -*- coding: utf-8 -*-
"""tests/trade/test_events_pump.py — 等待期就地消费回报类事件
(2026-09-19 架构修订批次 4.2)。

背景(架构审查 P0-4): rotation 等成交 / executor 等撤单 ack 原在消费者线程里
sleep 轮询 (最长 3 秒/笔), 阻塞期间**回报全排队** —— 而"这笔单成没成"恰恰就是
靠回报知道的。现在 `EventEngine.pump(PUMPABLE_WAIT_TYPES, …)` 把等待窗口内的
回报类事件就地分发, 其余事件 (命令/信号/扫描/对账) 原样留队列 (它们会重入
特性层, 等待中间插进来会打断状态机)。

本测试锁三件事:
  ① 等待期间到达的回报事件**在等待结束前**就被处理 (不是排队等);
  ② 非回报事件不被就地消费, 且一条不丢 (放回队列, 之后正常处理);
  ③ 安全阀: 非消费者线程 can_pump()=False 且 pump() 抛错 (防"两个写者")。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.events import (EVENT_ORDER_UPDATE, EVENT_TICK, EVENT_TIMER_SCAN,
                          EventEngine, Event, PUMPABLE_WAIT_TYPES)


def _make_engine(seen: list):
    """handlers 全部记录到 seen (顺序即处理顺序)。"""

    def _rec(name):
        return lambda e: seen.append(name)

    eng = EventEngine(handlers={
        "trigger": lambda e: None,          # 占位, 下面替换
        EVENT_ORDER_UPDATE: _rec("order_update"),
        EVENT_TICK: _rec("tick"),
        EVENT_TIMER_SCAN: _rec("timer_scan"),
    })

    def _trigger(e):
        seen.append("wait_start")
        eng.pump(PUMPABLE_WAIT_TYPES, 0.4)
        seen.append("wait_end")
    eng._handlers["trigger"] = _trigger
    return eng


def test_pump_consumes_fill_events_during_wait():
    seen: list = []
    eng = _make_engine(seen)
    eng.start()
    try:
        # 触发"等待", 稍后在等待窗口内投入一笔委托回报
        eng.put(Event(type="trigger"))
        time.sleep(0.05)
        eng.put(Event(type=EVENT_ORDER_UPDATE, data={"order_id": "O1"}))
        time.sleep(0.6)      # 等 trigger handler 跑完 + 后续队列清空
    finally:
        eng.stop()

    assert "order_update" in seen and "wait_end" in seen
    # 关键断言: 回报在"等待结束"之前就被处理了 (就地消费, 不是干等)
    assert seen.index("order_update") < seen.index("wait_end"), (
        f"回报事件必须在等待窗口内被就地处理, 实际顺序: {seen}")
    assert seen.index("wait_start") < seen.index("order_update")


def test_pump_consumes_tick_during_wait():
    """行情类也在等待窗口内就地消费 (monitor.on_quote 是叶子 handler, 不下单)
    —— 即"等待 3 秒期间报价不排队" (架构审查 P0-4 的验收点)。"""
    seen: list = []
    eng = _make_engine(seen)
    eng.start()
    try:
        eng.put(Event(type="trigger"))
        time.sleep(0.05)
        eng.put(Event(type=EVENT_TICK, data={"code": "510300.SH", "last": 4.0}))
        time.sleep(0.6)
    finally:
        eng.stop()
    assert "tick" in seen
    assert seen.index("tick") < seen.index("wait_end"), (
        f"tick 必须在等待窗口内被处理, 实际顺序: {seen}")


def test_pump_leaves_non_fill_events_in_queue_and_loses_none():
    seen: list = []
    eng = _make_engine(seen)
    eng.start()
    try:
        eng.put(Event(type="trigger"))
        time.sleep(0.05)
        eng.put(Event(type=EVENT_TIMER_SCAN, data={"hhmm": "09:35"}))
        time.sleep(0.7)
    finally:
        eng.stop()

    # 非回报事件: 不在等待窗口内处理 (留队列), 但一条不丢 (之后正常处理)
    assert "timer_scan" in seen, "scan 事件被放回了队列, 必须最终被处理"
    assert seen.index("timer_scan") > seen.index("wait_end"), (
        f"非回报事件不该在等待窗口内被消费, 实际顺序: {seen}")


def test_pump_rejects_non_consumer_thread():
    seen: list = []
    eng = _make_engine(seen)
    # 引擎未启动 (主线程不是消费者)
    assert eng.can_pump() is False
    with pytest.raises(RuntimeError, match="只许消费者线程"):
        eng.pump(PUMPABLE_WAIT_TYPES, 0.01)


def test_pump_drains_within_budget_not_longer():
    """等待窗口有上限: pump(0.2) 不应显著超时 (它是"等 0.2 秒", 不是阻塞循环)。"""
    seen: list = []
    eng = _make_engine(seen)
    box: dict = {}

    def _trigger(e):
        t0 = time.monotonic()
        eng.pump(PUMPABLE_WAIT_TYPES, 0.2)
        box["elapsed"] = time.monotonic() - t0
    eng._handlers["trigger"] = _trigger
    eng.start()
    try:
        eng.put(Event(type="trigger"))
        time.sleep(0.4)
    finally:
        eng.stop()
    assert 0.15 <= box["elapsed"] < 0.55, f"窗口应约 0.2s, 实际 {box['elapsed']:.2f}s"
