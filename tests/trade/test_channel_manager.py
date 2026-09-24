"""ChannelManager 状态机接缝测试 (2026-09-07, 方案设计书 §5.3)。

定位: 断言"意图 vs 生效"双层态、探针通过/失败/悬空/断线哨兵的转换
与网关武装下推 —— 不连真客户端、不 import easytrader。替身网关注入。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.channel_manager import ChannelManager  # noqa: E402


class FakeGw:
    """ChannelManager 的替身网关: 记录 set_armed, 可注入查询失败/行情检查。"""

    def __init__(self):
        self.armed = False
        self.connect_calls = 0
        self.query_asset_ok = True
        self.positions_ok = True
        self.orders_ok = True

    def set_armed(self, armed):
        self.armed = bool(armed)

    def connect(self):
        self.connect_calls += 1

    def query_asset(self):
        if not self.query_asset_ok:
            raise RuntimeError("资金查询失败 (替身)")
        return {"cash": 1000.0, "frozen_cash": 0.0, "market_value": 0.0,
                "total_asset": 1000.0}

    def query_positions(self):
        if not self.positions_ok:
            raise RuntimeError("持仓查询失败 (替身)")
        return []

    def query_orders(self):
        if not self.orders_ok:
            raise RuntimeError("委托查询失败 (替身)")
        return []


def _make(clock, intent=True, retry=60, rounds=3, quote_check=None,
          audits=None):
    gw = FakeGw()
    def rec(kind, msg, detail):
        if audits is not None:
            audits.append((kind, msg, detail))
    mgr = ChannelManager(
        gw, armed_intent=intent, retry_sec=retry, rounds=rounds,
        quote_check=quote_check, write_audit=rec, clock=clock)
    return gw, mgr


def test_construct_disarmed():
    """构造即未武装 (armed 锁 False), 意图如实保留。"""
    _, mgr = _make(lambda: 0.0, intent=True)
    assert mgr.armed_intent is True
    assert mgr.armed_effective is False
    assert mgr.last_probe["ok"] is False


def test_probe_ok_with_intent_arms():
    """探针通过 + 意图真 → 武装生效, 网关锁放开。"""
    t = [1000.0]
    gw, mgr = _make(lambda: t[0], intent=True)
    r = mgr.full_probe()
    assert r["ok"] is True
    assert mgr.armed_effective is True
    assert gw.armed is True


def test_probe_ok_without_intent_stays_disarmed():
    """探针通过但意图关 → 不武装 (意图是必要不充分条件)。"""
    gw, mgr = _make(lambda: 0.0, intent=False)
    mgr.full_probe()
    assert mgr.last_probe["ok"] is True
    assert mgr.armed_effective is False
    assert gw.armed is False


def test_probe_fail_goes_suspend():
    """探针失败 → 悬空 (未武装), 记录失败原因, 网关锁保持。"""
    t = [1000.0]
    gw, mgr = _make(lambda: t[0], intent=True)
    gw.query_asset_ok = False
    r = mgr.full_probe()
    assert r["ok"] is False
    assert "资金查询失败" in r["error"]
    assert mgr.armed_effective is False
    assert gw.armed is False


def test_arm_refused_without_fresh_probe():
    """未探针过直接 arm → 拒绝给理由, 不抛。"""
    _, mgr = _make(lambda: 0.0, intent=True)
    r = mgr.arm()
    assert r["ok"] is False and "探针未通过" in r["reason"]


def test_arm_refused_without_intent():
    """意图关时即使探针过也不准 arm (arm 是意图的显式化)。"""
    gw, mgr = _make(lambda: 0.0, intent=False)
    mgr.full_probe()
    r = mgr.arm()
    assert r["ok"] is False and "意图未开启" in r["reason"]


def test_disarm_and_rearm_cycle():
    """解除零摩擦 → 再武装需探针仍有效 (本周期内 ok 即放行)。"""
    t = [1000.0]
    gw, mgr = _make(lambda: t[0], intent=True)
    mgr.full_probe()
    assert gw.armed is True
    r = mgr.disarm()
    assert r["ok"] is True and gw.armed is False
    r = mgr.arm()
    assert r["ok"] is True and gw.armed is True


def test_poll_failure_sentinel():
    """轮询连续失败达判断线 → channel_down; 一次成功清零。"""
    t = [1000.0]
    _, mgr = _make(lambda: t[0], rounds=3)
    assert mgr.channel_down is False
    for _ in range(2):
        mgr.on_poll_result(False)
    assert mgr.channel_down is False
    mgr.on_poll_result(False)
    assert mgr.channel_down is True
    mgr.on_poll_result(True)
    assert mgr.channel_down is False
    assert mgr.consecutive_failures == 0


def test_probe_retry_due_gating():
    """悬空态重探到点判定: 有意图+未生效+距上次≥retry。"""
    t = [0.0]
    gw, mgr = _make(lambda: t[0], intent=True, retry=60)
    # 初始: 无生效且从未探针 → 距上次是巨值, 应到点
    assert mgr.probe_retry_due() is True
    # 刚失败探针 → 距上次 0, 未到点
    gw.query_asset_ok = False
    mgr.full_probe()   # ts=0
    t[0] = 30.0
    assert mgr.probe_retry_due() is False
    t[0] = 61.0
    assert mgr.probe_retry_due() is True
    # 生效后不需要重探
    t[0] = 1000.0
    gw.query_asset_ok = True
    mgr.full_probe()   # ok → armed
    assert mgr.armed_effective is True
    t[0] = 2000.0
    assert mgr.probe_retry_due() is False


def test_quote_check_blocks_arm():
    """行情源检查不过 → 探针失败并给出人话原因 (方案设计书 §5.8)。"""
    audits = []
    _, mgr = _make(lambda: 0.0, intent=True,
                   quote_check=lambda: False, audits=audits)
    r = mgr.full_probe()
    assert r["ok"] is False and "行情源" in r["error"]
    assert audits and audits[0][0] == "ths_probe"


def test_audit_written_on_probe():
    """每次探针留痕 (ths_probe) 含 ok/intent/effective。"""
    audits = []
    _, mgr = _make(lambda: 0.0, intent=True, audits=audits)
    mgr.full_probe()
    kind, msg, detail = audits[0]
    assert kind == "ths_probe" and detail["ok"] is True
    assert detail["armed_effective"] is True


def test_apply_reconfig_keeps_effective_until_probe():
    """apply 改意图不清生效态; 生效仍以探针为准。"""
    t = [1000.0]
    gw, mgr = _make(lambda: t[0], intent=True)
    mgr.full_probe()
    assert gw.armed is True
    # 运行中把意图关掉: 立即失去武装依据 (保守: 生效随意图关解除)
    mgr.apply(armed_intent=False)
    assert mgr.armed_intent is False
    # 意图是生效的必要条件 —— 关意图即解武装
    assert mgr.armed_effective is False
