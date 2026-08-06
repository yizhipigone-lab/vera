"""HIGH#6 (2026-08-06 审计): monitor _peak_px 注入路径测试.

审计前 grep "peak_px|query_daily_highs" tests/ = 0 命中 —— 实盘新路径
(2026-08-06 移动止盈持仓期历史峰值, monitor.py:344 hist_peak) 零保护,
错了会误卖/漏卖。本文件锁住注入点的 4 种行为:

1. peak_px 返回历史峰值 → peak=max(cost,high,hist) 含 hist, 触发移动止盈
2. 不注入 (默认 None→0.0) 同场景不触发 —— 与 1 对照, 证明 hist_peak 是主因
3. peak_px 抛异常 → monitor.py:345 try/except 回退 0.0, 不崩
4. peak_px 返回 None/0/负 (falsy) → `or 0.0` 回退当日口径

场景设计 (activation 默认 3.5%, drawdown 测试档 5%):
- cost=10, 当日 high=10.2 (仅涨 2% < 3.5% 未激活), hist_peak=11 (涨 10%)
- 注入: peak=11 激活 + last=10.4 ≤ 11×0.95=10.45 触发
- 不注入: peak=10.2 涨 2% 未激活, 不触发
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.book import DIRECTION_BUY, Book
from trade.config import (
    CostStopConfig,
    StopConfig,
    TradeConfig,
    TrailingStopConfig,
)
from trade.gateway import FakeGateway
from trade.monitor import Monitor
from trade.store import TradeStore

# 时段感知: 自动规则只在连续竞价评估, 时钟落工作日盘中 (2024-01-02 周二 10:00)
_T0 = time.mktime(time.strptime("2024-01-02 10:00", "%Y-%m-%d %H:%M"))
CODE = "600519.SH"


class _StubExecutor:
    """记录 execute_exit 调用的替身 (与 test_monitor.py 同款, 不跨文件共享)。"""

    def __init__(self):
        self.exits: list[tuple[str, str]] = []

    def execute_exit(self, code, reason, qty=None):
        self.exits.append((code, reason))
        return True

    def pending_check(self, now_hhmm=None):
        pass


@pytest.fixture()
def store(tmp_path):
    s = TradeStore(tmp_path / "trade.db", tmp_path / "raw.jsonl")
    yield s
    s.close()


def _book(code=CODE, volume=1000, cost=10.0):
    b = Book()
    b.apply_trade(f"T-{code}", f"O-{code}", code, DIRECTION_BUY, cost, volume)
    b.set_can_use(code, volume)
    return b


def _cfg():
    # drawdown 5% (比默认好构造序列), activation 走默认 3.5%
    return TradeConfig(
        account_id="TEST", tick_heartbeat_sec=15,
        stop=StopConfig(
            trailing_stop=TrailingStopConfig(drawdown=0.05),
            cost_stop=CostStopConfig(threshold=-0.12),
        ),
    )


def _make(store, peak_px, clock=_T0):
    """造 Monitor 并注入 peak_px (默认 None → Monitor 内置 lambda:None)。"""
    gw = FakeGateway()
    gw.connect()
    stub = _StubExecutor()
    mon = Monitor(gw, _book(), stub, store, _cfg(),
                  hold_days=lambda c: 0, peak_px=peak_px,
                  clock=(lambda: clock))
    return mon, stub


# 触发场景的统一 tick: last=10.4 / 当日 high=10.2 (仅涨 2%)
_TICK = {"last": 10.4, "bid1": 10.4, "high": 10.2}


def test_peak_px_drives_trailing_trigger(store):
    """注入 hist_peak=11 → peak=11 (涨 10% > 激活 3.5%) + last=10.4 ≤ 11×0.95
    → 触发移动止盈。证明 _peak_px 注入生效。"""
    mon, stub = _make(store, peak_px=lambda c: 11.0)
    mon.on_quote(CODE, _TICK)
    trigs = mon.scan_once()
    assert len(stub.exits) == 1
    assert trigs and trigs[0][0] == CODE


def test_peak_px_absent_no_trigger(store):
    """同场景不注入 peak_px (默认 None→0.0): peak=max(10,10.2,0)=10.2 仅涨 2%
    < 激活 3.5% → 不触发。与上一用例对照, 证明 hist_peak 是触发主因。"""
    mon, stub = _make(store, peak_px=None)
    mon.on_quote(CODE, _TICK)
    mon.scan_once()
    assert stub.exits == []


def test_peak_px_exception_fallback_no_crash(store):
    """peak_px 抛异常 (xtdata 挂) → monitor.py:345 try/except 回退 hist_peak=0.0,
    不崩, 等价默认不触发 (无盲区吞错)。"""
    def boom(code):
        raise RuntimeError("xtdata down")
    mon, stub = _make(store, peak_px=boom)
    mon.on_quote(CODE, _TICK)
    mon.scan_once()  # 不抛即过
    assert stub.exits == []


def test_peak_px_falsy_fallback(store):
    """peak_px 返回 None / 0.0 / 负数 (falsy) → `self._peak_px(code) or 0.0`
    回退当日口径, 不触发 (不会把 None/0 当峰值涨跌幅误算)。"""
    for v in (None, 0.0, -1.0):
        mon, stub = _make(store, peak_px=(lambda c, _v=v: _v))
        mon.on_quote(CODE, _TICK)
        mon.scan_once()
        assert stub.exits == [], f"peak_px={v} 应回退不触发"
