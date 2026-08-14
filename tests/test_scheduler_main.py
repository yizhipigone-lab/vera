"""tests/test_scheduler_main.py — scheduler/__main__ 注册逻辑。

重点: _register_sentiment 必须 (a) 注册 tick+daily 双 job, (b) 启动飞书推送 worker
(集成 bug 修复的关键: 不 start 则队列只入不出, 告警全丢), (c) brain 不可用时 fail-soft
不拖垮月度笔记/周度进化。
"""
from __future__ import annotations

import scheduler.vera_scheduler as vs


def test_register_sentiment_starts_and_configures(monkeypatch):
    """注册双 job + 启动飞书 worker + 间隔/时刻读自 config。"""
    import research.sentiment_notifier as sn

    class FakeNotifier:
        def __init__(self):
            self.started = False

        def start(self):
            self.started = True

        def stop(self, drain_timeout_sec: float = 2.0):
            self.started = False

    fake = FakeNotifier()
    monkeypatch.setattr(sn, "get_default_notifier", lambda: fake)

    from scheduler.__main__ import _register_sentiment
    sched = vs.VeraScheduler()
    _register_sentiment(sched)

    names = {j.name: j for j in sched._jobs}
    assert names["sentiment_tick"].kind == "interval"
    assert names["sentiment_tick"].interval_sec == 600.0      # 10min (config)
    assert names["sentiment_tick"].trading_hours == ("09:30-11:30", "13:00-15:00")
    assert names["sentiment_daily"].hhmm == "15:05"           # config daily_report_hhmm
    assert fake.started is True, "飞书推送 worker 未启动 (告警会全丢)"


def test_register_sentiment_fail_soft_when_brain_unavailable(monkeypatch):
    """brain 导入失败 → _register_sentiment 不抛, job 列表空 (不影响其他 job)。"""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name.startswith("brain."):
            raise ImportError("模拟 brain 不可用")
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    from scheduler.__main__ import _register_sentiment
    sched = vs.VeraScheduler()
    _register_sentiment(sched)          # 不抛 (fail-soft)
    assert sched._jobs == []            # 没注册上, 但也不崩
