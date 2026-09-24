"""tests/test_scheduler_cache_refresh.py — 盘后缓存补拉 job 注册与委托护栏。

补拉实现已移到 core/kline_cache_maintenance（server 启动自检共用），
本文件只锁 scheduler 侧：注册参数 + job 委托到共用入口。
"""
from __future__ import annotations

import datetime as dt

import scheduler.vera_scheduler as vs


def test_cache_refresh_registered():
    """kline_cache_refresh: daily + 15:45 + 只在交易日触发。"""
    import scheduler.__main__ as m
    sched = vs.VeraScheduler()
    sched.add_daily("kline_cache_refresh", m._job_kline_cache_refresh, hhmm="15:45")
    job = {j.name: j for j in sched._jobs}["kline_cache_refresh"]
    assert job.kind == "daily" and job.hhmm == "15:45"
    assert vs.is_due(job, dt.datetime(2026, 8, 14, 15, 46)) is True   # 周五
    assert vs.is_due(job, dt.datetime(2026, 8, 15, 15, 46)) is False  # 周六


def test_job_delegates_to_maintenance(monkeypatch):
    """job 委托 core.kline_cache_maintenance.ensure_cache_fresh。"""
    import scheduler.__main__ as m
    cap = {}

    def fake_ensure(trigger):
        cap["trigger"] = trigger
        return {"fresh": True, "stale": [], "refreshing": False}
    monkeypatch.setattr("core.kline_cache_maintenance.ensure_cache_fresh",
                        fake_ensure)
    m._job_kline_cache_refresh()
    assert cap["trigger"] == "scheduler_daily"
