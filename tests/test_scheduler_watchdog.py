# -*- coding: utf-8 -*-
"""tests/test_scheduler_watchdog.py — 交易进程看门狗 (2026-09-20 item 4)。

为什么要有这个文件:
调度器至少两个静默停机窗口无人知 (09-11~09-14, 09-19~09-20,
scheduler/health.py:17-20 自记)。看门狗 = scheduler 里一个 1 分钟 interval
job, 探 8081(交易)+8080(web), 连续 2 次失败推飞书。

判定逻辑抽成纯函数 evaluate_watchdog 放 scheduler/health.py (铁律 1:
不 import trade)。本文件锁: 判定规则 (首失败不告警/二次告警/冷却/恢复/
None 三态防御/字段缺失回退) + job 行为 (人工停机整轮跳过/错两次只发一次/
探测异常不冒泡)。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scheduler import health as H  # noqa: E402


def _snap(trade_ok=True, web_ok=True, monitor_healthy=True, tick_age=5.0):
    return {"trade_ok": trade_ok, "web_ok": web_ok,
            "monitor_healthy": monitor_healthy, "last_tick_age_s": tick_age}


def _state():
    return {"fails": 0, "last_alert_key": "", "last_alert_ts": 0.0,
            "was_down": False}


class TestEvaluateWatchdog:
    def test_全好_无动作(self):
        st = _state()
        d = H.evaluate_watchdog(_snap(), st, now=1000.0)
        assert d["action"] == "none" and st["fails"] == 0

    def test_首次失败只计数不告警(self):
        st = _state()
        d = H.evaluate_watchdog(_snap(trade_ok=False), st, now=1000.0)
        assert d["action"] == "none" and st["fails"] == 1

    def test_连续两次失败告警(self):
        st = _state()
        H.evaluate_watchdog(_snap(trade_ok=False), st, now=1000.0)
        d = H.evaluate_watchdog(_snap(trade_ok=False), st, now=1060.0)
        assert d["action"] == "alert" and "8081" in d["reason"]

    def test_同原因冷却期内不重复告警(self):
        st = _state()
        H.evaluate_watchdog(_snap(trade_ok=False), st, now=1000.0)
        H.evaluate_watchdog(_snap(trade_ok=False), st, now=1060.0)
        d = H.evaluate_watchdog(_snap(trade_ok=False), st, now=1120.0)
        assert d["action"] == "none", "冷却期内同原因不得重复告警"

    def test_恢复发recover(self):
        st = _state()
        H.evaluate_watchdog(_snap(trade_ok=False), st, now=1000.0)
        H.evaluate_watchdog(_snap(trade_ok=False), st, now=1060.0)
        d = H.evaluate_watchdog(_snap(), st, now=1120.0)
        assert d["action"] == "recover" and st["was_down"] is False

    def test_monitor_healthy为None不误报(self):
        """None = 非连续竞价(不适用), 不许当 False 告警。"""
        st = _state()
        d = H.evaluate_watchdog(_snap(monitor_healthy=None), st, now=1000.0)
        assert d["action"] == "none"

    def test_tick年龄超阈值告警(self):
        st = _state()
        H.evaluate_watchdog(_snap(tick_age=300.0), st, now=1000.0)
        d = H.evaluate_watchdog(_snap(tick_age=300.0), st, now=1060.0)
        assert d["action"] == "alert" and "行情" in d["reason"]

    def test_tick年龄缺失时回退到monitor_healthy(self):
        """字段缺失 (滚动期 trade_main 未重启) → 回退用 monitor_healthy。"""
        st = _state()
        snap = _snap(tick_age=None, monitor_healthy=False)
        H.evaluate_watchdog(snap, st, now=1000.0)
        d = H.evaluate_watchdog(snap, st, now=1060.0)
        assert d["action"] == "alert"

    def test_tick年龄缺失且monitor_healthy为True不告警(self):
        st = _state()
        d = H.evaluate_watchdog(_snap(tick_age=None), st, now=1000.0)
        assert d["action"] == "none"

    def test_web挂了也告警(self):
        st = _state()
        H.evaluate_watchdog(_snap(web_ok=False), st, now=1000.0)
        d = H.evaluate_watchdog(_snap(web_ok=False), st, now=1060.0)
        assert d["action"] == "alert" and "8080" in d["reason"]


class TestWatchdogJob:
    def test_人工停机整轮跳过(self, monkeypatch, tmp_path):
        """停止标记存在 → 探测都不该发生 (人工停机不算故障, 防误报训练)。"""
        import scheduler.__main__ as M
        marker = tmp_path / ".vera_stopped"
        marker.write_text("x", encoding="utf-8")
        monkeypatch.setattr(H, "STOP_MARKER_PATH", marker)
        calls = []
        monkeypatch.setattr(M, "_probe_watchdog",
                            lambda: calls.append(1) or _snap())
        M._WATCHDOG_STATE.update(_state())
        M._job_trade_watchdog()
        assert calls == [], "人工停机时连探测都不该发生"

    def test_连续两次失败只发一次飞书(self, monkeypatch):
        import scheduler.__main__ as M
        monkeypatch.setattr(H, "stopped_on_purpose", lambda: False)
        monkeypatch.setattr(M, "_probe_watchdog",
                            lambda: _snap(trade_ok=False))
        sent = []
        monkeypatch.setattr(M, "_send_watchdog_alert",
                            lambda title, text, level="red":
                            sent.append((title, text, level)))
        M._WATCHDOG_STATE.update(_state())
        M._job_trade_watchdog()
        M._job_trade_watchdog()
        assert len(sent) == 1, f"错两次应只发一次, 实际 {len(sent)}"
        assert sent[0][2] == "red"

    def test_恢复后发一次恢复通知(self, monkeypatch):
        import scheduler.__main__ as M
        monkeypatch.setattr(H, "stopped_on_purpose", lambda: False)
        probe = {"snap": _snap(trade_ok=False)}
        monkeypatch.setattr(M, "_probe_watchdog", lambda: probe["snap"])
        sent = []
        monkeypatch.setattr(M, "_send_watchdog_alert",
                            lambda title, text, level="red":
                            sent.append((title, text, level)))
        M._WATCHDOG_STATE.update(_state())
        M._job_trade_watchdog()
        M._job_trade_watchdog()
        assert len(sent) == 1 and sent[0][2] == "red"
        probe["snap"] = _snap()
        M._job_trade_watchdog()
        assert len(sent) == 2 and sent[1][2] == "green"

    def test_探测异常不冒泡(self, monkeypatch):
        """看门狗绝不能把异常冒进调度器主循环 (它自己不能成为新单点)。"""
        import scheduler.__main__ as M
        monkeypatch.setattr(H, "stopped_on_purpose", lambda: False)

        def _boom():
            raise RuntimeError("boom")

        monkeypatch.setattr(M, "_probe_watchdog", _boom)
        M._WATCHDOG_STATE.update(_state())
        M._job_trade_watchdog()  # 不抛即通过
