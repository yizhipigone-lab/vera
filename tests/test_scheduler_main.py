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


# ── 大盘位置三个 job 与「推送时刻」回归锁 (2026-09-17 用户纠错) ──────────
#
# 背景: 原设计"次日 09:05 推体温表"被用户指出是白推 —— 实测
# `expected_last_trading_day()` 在"当日 15:50"与"次日 09:05"返回同一天,
# 两次 collect 的记录逐字段相同, 早上那张卡零新信息。体温表是收盘价算出来的,
# 天生属于"盘后"。计划书: docs/plan/2026-09-17_大盘位置与趋势研判_计划书.md §17


def _market_jobs():
    from scheduler.__main__ import _register_market_position
    sched = vs.VeraScheduler()
    _register_market_position(sched)
    return {j.name: j for j in sched._jobs}


def test_market_position_jobs_registered():
    """三个 job 都在, 且都是 daily。"""
    jobs = _market_jobs()
    for name in ("market_position_collect", "market_position_push",
                 "market_position_morning"):
        assert name in jobs, f"缺 job: {name}"
        assert jobs[name].kind == "daily"


def test_push_time_not_before_cache_refresh():
    """**回归锁**: 体温表推送时刻必须 ≥15:45 (K 线缓存补尾段之后)。

    防的是"又把它挪回早上/挪到缓存刷新之前"——那会拿旧数据出报告。
    """
    jobs = _market_jobs()
    push_hhmm = jobs["market_position_push"].hhmm
    # 字符串比较对 "HH:MM" 零填充格式成立
    assert push_hhmm >= "15:45", \
        f"体温表推送时刻 {push_hhmm} 早于 15:45, 会用到未刷新的缓存"
    # 采集必须早于推送 (先有数据再推)
    assert jobs["market_position_collect"].hhmm <= push_hhmm


def test_morning_job_does_not_push():
    """09:05 那个 job **不得**再推体温表 (只补采兜底)。

    用 AST 查函数体里没有 push_thermometer 调用 —— 靠字符串匹配容易漏
    (注释里也提到过这个函数名), 故走语法树。
    """
    import ast
    import inspect

    from scheduler import __main__ as sm
    src = inspect.getsource(sm._job_market_position_morning)
    tree = ast.parse(src)
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            called.add(node.attr)
        elif isinstance(node, ast.Name):
            called.add(node.id)
    assert "push_thermometer" not in called, \
        "09:05 的 job 又推体温表了 —— 与前一天 15:50 的数据完全相同, 属重复推送"


def test_push_job_recollects_before_pushing():
    """15:55 的 job 必须**先采集再推送**。

    理由: "只推不采"有真实缺口 —— 若 15:50 那次失败, 15:55 会推一张旧卡
    (2026-09-17 手动验证时实测复现: 缓存已到 9/16, 推出去的却是 9/15 那条)。
    """
    import ast
    import inspect

    from scheduler import __main__ as sm
    src = inspect.getsource(sm._job_market_position_push)
    tree = ast.parse(src)
    called = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            called.append(node.attr)
    assert "collect" in called, "15:55 的 job 没有先采集, 可能推出旧卡"
    assert "push_thermometer" in called, "15:55 的 job 没有推送"
    assert called.index("collect") < called.index("push_thermometer"), \
        "必须先 collect 再 push"
