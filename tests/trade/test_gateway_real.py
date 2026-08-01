"""RealGateway 行情订阅腿接缝测试 (审计H5修复, 2026-07-26)。

注意定位: 这是给不可信外部库做接缝测试, 不是自欺 —— 断言的是
"我们的转换与接线逻辑" (callback 转换输出、run 线程幂等、退订
逐票调 unsubscribe_quote), 不断言 xtdata 本身的行为。
FakeGateway 路径不受本文件影响 (xtdata 不可 import 的环境照跑)。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.gateway import RealGateway


class FakeXtdata:
    """xtdata 的测试替身: 记录订阅/退订/运行调用, 回放 tick。"""

    def __init__(self):
        self.subscriptions = {}   # seq -> (code, period, count, callback)
        self.unsubscribed: list[int] = []
        self.run_calls = 0
        self._seq = 0

    def subscribe_quote(self, stock_code, period="tick", count=0, callback=None):
        self._seq += 1
        self.subscriptions[self._seq] = (stock_code, period, count, callback)
        return self._seq

    def unsubscribe_quote(self, seq):
        self.unsubscribed.append(seq)

    def run(self):
        self.run_calls += 1     # 真身阻塞; 替身立即返回, 只证"被启动"

    def get_full_tick(self, codes):
        return {c: _tick(c) for c in codes}


def _tick(code="000001.SZ", last=10.5, ts_ms=1_700_000_000_000):
    """与真机 introspect 同形的 tick dict。"""
    return {"lastPrice": last, "bidPrice": [last - 0.01], "askPrice": [last + 0.01],
            "high": 11.0, "low": 10.0, "lastClose": 10.2, "time": ts_ms}


@pytest.fixture()
def xt():
    return FakeXtdata()


@pytest.fixture()
def gw(xt):
    received = []
    g = RealGateway(account_id="TEST",
                    on_quote=lambda code, q: received.append((code, q)))
    g._xtdata = lambda: xt          # 接缝: 注入替身 (实例属性遮蔽方法)
    g._received = received
    return g


def test_subscribe_registers_callback_per_code(gw, xt):
    """每票 subscribe_quote(period='tick', callback=...), seq 保存。"""
    assert gw.subscribe_quotes(["000001.SZ", "600519.SH"])
    assert len(xt.subscriptions) == 2
    code, period, count, cb = xt.subscriptions[1]
    assert (code, period, count) == ("000001.SZ", "tick", 0)
    assert callable(cb)
    assert gw._quote_seqs == {"000001.SZ": 1, "600519.SH": 2}


def test_callback_converts_latest_tick_to_quote(gw, xt):
    """callback 取最后一条 tick, 转换输出与 query_quotes 同构同映射。"""
    gw.subscribe_quotes(["000001.SZ"])
    cb = xt.subscriptions[1][3]
    older = _tick(last=10.4, ts_ms=1_700_000_000_000)
    newer = _tick(last=10.6, ts_ms=1_700_000_001_000)
    cb({"000001.SZ": [older, newer]})   # 可能多条, 只取最新
    assert len(gw._received) == 1
    code, quote = gw._received[0]
    assert code == "000001.SZ"
    assert quote["last"] == 10.6 and quote["bid1"] == 10.59
    assert quote["ask1"] == 10.61 and quote["high"] == 11.0
    assert quote["prev_close"] == 10.2
    assert quote["ts"] == 1_700_000_001.0     # 毫秒 → 秒
    # 同构: 订阅推送与轮询兜底同一份字段映射 (两条腿一个口径)
    polled = gw.query_quotes(["000001.SZ"])["000001.SZ"]
    assert set(quote.keys()) == set(polled.keys())


def test_tick_without_timestamp_gives_none(gw, xt):
    """tick 无时间戳字段 → ts=None (2026-07-27 裁决③: 不再兜底
    time.time 冒充新鲜, None 由 monitor 视为陈旧 fail-closed)。"""
    gw.subscribe_quotes(["000001.SZ"])
    cb = xt.subscriptions[1][3]
    tick = _tick()
    del tick["time"]
    cb({"000001.SZ": [tick]})
    _, quote = gw._received[0]
    assert quote["ts"] is None


def test_run_thread_started_once(gw, xt):
    """xtdata.run 幂等: 多次 subscribe 只启一次推送循环。"""
    gw.subscribe_quotes(["000001.SZ"])
    gw.subscribe_quotes(["600519.SH"])
    deadline = time.time() + 2.0
    while xt.run_calls < 1 and time.time() < deadline:
        time.sleep(0.01)
    assert xt.run_calls == 1


def test_unsubscribe_all_per_seq(gw, xt):
    """unsubscribe_all 用保存的 seq 逐票退订并清空登记。"""
    gw.subscribe_quotes(["000001.SZ", "600519.SH"])
    gw.unsubscribe_all()
    assert sorted(xt.unsubscribed) == [1, 2]
    assert gw._quote_seqs == {}


def test_resubscribe_overwrites_seq(gw, xt):
    """断线重连后的重订阅: 新 seq 覆盖旧 seq (trade_main 已有重订阅逻辑)。"""
    gw.subscribe_quotes(["000001.SZ"])
    gw.subscribe_quotes(["000001.SZ"])
    assert gw._quote_seqs["000001.SZ"] == 2


# ── 2026-07-31: 断线重连死循环修复 (连续失败释放旧会话+换 session_id) ──

class FakeTrader:
    """XtQuantTrader 替身: 记录 start/stop, connect 返回可编排的 rc。"""

    instances: list = []
    rc_queue: list = []         # 每次 connect 弹一个返回码

    def __init__(self, path, session_id, callback):
        self.path, self.session_id = path, session_id
        self.started = False
        self.stopped = False
        self.subscribed = []
        FakeTrader.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def connect(self):
        return FakeTrader.rc_queue.pop(0)

    def subscribe(self, account):
        self.subscribed.append(account)


class _FakeCb:    # XtQuantTraderCallback 替身 (connect 只拿来当基类)
    pass


def _wire_fake_xt(gw):
    FakeTrader.instances = []
    FakeTrader.rc_queue = []
    gw._xt = lambda: (FakeTrader, _FakeCb, lambda acc: acc)   # 实例属性遮蔽静态方法


def test_connect_success_replaces_old_trader():
    """基线: 成功才关旧 (审计M9/计划书 §5.5 语义不破坏)。"""
    g = RealGateway(account_id="TEST")
    _wire_fake_xt(g)
    FakeTrader.rc_queue = [0, 0]
    assert g.connect() and g.connect()
    a, b = FakeTrader.instances
    assert a.stopped and not b.stopped and g._trader is b
    assert g._connect_failures == 0


def test_connect_failure_keeps_old_until_threshold():
    """前 2 次失败: 新实例收尸、旧实例保留 (断线期最后的信息源);
    第 3 次失败: 释放旧实例并更换 session_id (0731 死循环修复);
    之后成功: 正常替换, 失败计数清零。"""
    g = RealGateway(account_id="TEST")
    _wire_fake_xt(g)
    FakeTrader.rc_queue = [0]                 # 先连上一次, 造出"旧会话"
    g.connect()
    old_sid = g._session_id
    old = g._trader

    FakeTrader.rc_queue = [-1, -1]
    for _ in range(2):
        with pytest.raises(RuntimeError):
            g.connect()
    assert g._trader is old and not old.stopped   # 成功才关旧
    assert g._session_id == old_sid

    FakeTrader.rc_queue = [-1]
    with pytest.raises(RuntimeError):
        g.connect()                           # 第 3 次失败 → 释放
    assert old.stopped and g._trader is None
    assert g._session_id != old_sid           # 换 session, 残留会话不再挡路

    FakeTrader.rc_queue = [0]
    assert g.connect()                        # 释放后能连上
    assert g._connect_failures == 0
