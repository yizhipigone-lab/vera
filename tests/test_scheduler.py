"""scheduler/ 测试 — 交易日历 / 定时器触发逻辑 / 异常隔离 / 优雅停机。

网络零依赖, 全部本地。exchange_calendars 已装走精确历, 未装走内置假日表,
两条路径在本仓库 2026 年口径下结论一致; 降级路径用 monkeypatch 强制。
"""
from __future__ import annotations

import datetime as dt
import signal
import threading

import pytest

from scheduler import trading_calendar as tc
from scheduler import vera_scheduler as vs
from scheduler.graceful_shutdown import install

# ── 交易日历 ─────────────────────────────────────────────────

class TestTradingCalendar:
    def test_weekday_is_trading_day(self):
        assert tc.is_trading_day(dt.date(2026, 1, 5)) is True   # 周一

    def test_weekend_not_trading_day(self):
        assert tc.is_trading_day(dt.date(2026, 1, 3)) is False  # 周六
        # A 股周末一律休市: 2026-01-04 虽是国务院调休工作日, 周日仍不开市
        assert tc.is_trading_day(dt.date(2026, 1, 4)) is False
        assert tc.is_trading_day(dt.date(2026, 6, 6)) is False  # 周六

    def test_new_year_holiday(self):
        assert tc.is_trading_day(dt.date(2026, 1, 1)) is False  # 元旦 (周四)

    def test_next_trading_day_skips_weekend(self):
        assert tc.next_trading_day(dt.date(2026, 1, 9)) == dt.date(2026, 1, 12)

    def test_fallback_path(self, monkeypatch):
        """强制降级: _XCAL=None, 走内置假日表。"""
        monkeypatch.setattr(tc, "_XCAL", None)
        monkeypatch.setattr(tc, "_XCAL_TRIED", True)
        assert tc.is_trading_day(dt.date(2026, 1, 5)) is True    # 普通周一
        assert tc.is_trading_day(dt.date(2026, 2, 16)) is False  # 春节 (周一)
        assert tc.is_trading_day(dt.date(2026, 5, 1)) is False   # 劳动节 (周五)
        assert tc.is_trading_day(dt.date(2026, 1, 4)) is False   # 调休周日仍休市
        assert tc.is_trading_day(dt.date(2026, 6, 6)) is False   # 普通周六
        assert tc.next_trading_day(dt.date(2026, 9, 30)) >= dt.date(2026, 10, 9)

    def test_fallback_warning_logged(self, monkeypatch, caplog):
        """库加载失败时记 warning 并降级 (不抛异常)。"""
        import builtins
        monkeypatch.setattr(tc, "_XCAL", None)
        monkeypatch.setattr(tc, "_XCAL_TRIED", False)
        real_import = builtins.__import__
        def fake_import(name, *a, **kw):
            if name == "exchange_calendars":
                raise ImportError("模拟缺失")
            return real_import(name, *a, **kw)
        monkeypatch.setattr(builtins, "__import__", fake_import)
        with caplog.at_level("WARNING", logger="scheduler.trading_calendar"):
            assert tc.is_trading_day(dt.date(2026, 1, 5)) is True
        assert "降级" in caplog.text


# ── 定时器触发逻辑 (纯函数 + run_pending 注入时间) ───────────

@pytest.fixture
def fake_weekday_calendar(monkeypatch):
    """把交易日判断替换为"周一~周五", 与真实假日表解耦。"""
    monkeypatch.setattr(vs, "is_trading_day", lambda d: d.weekday() < 5)
    def _next(d):
        cur = d + dt.timedelta(days=1)
        while cur.weekday() >= 5:
            cur += dt.timedelta(days=1)
        return cur
    monkeypatch.setattr(vs, "next_trading_day", _next)


def _at(y, m, d, hh, mm=0):
    return dt.datetime(y, m, d, hh, mm)


class TestDailyJob:
    def test_due_after_hhmm_on_trading_day(self, fake_weekday_calendar):
        calls = []
        sched = vs.VeraScheduler()
        sched.add_daily("j", lambda: calls.append(1), "09:00")
        # 2026-06-01 周一
        assert sched.run_pending(_at(2026, 6, 1, 8, 59)) == 0
        assert sched.run_pending(_at(2026, 6, 1, 9, 0)) == 1
        assert calls == [1]

    def test_not_due_on_weekend(self, fake_weekday_calendar):
        sched = vs.VeraScheduler()
        sched.add_daily("j", lambda: None, "09:00")
        assert sched.run_pending(_at(2026, 6, 6, 10, 0)) == 0  # 周六

    def test_no_double_fire_same_day(self, fake_weekday_calendar):
        calls = []
        sched = vs.VeraScheduler()
        sched.add_daily("j", lambda: calls.append(1), "09:00")
        sched.run_pending(_at(2026, 6, 1, 9, 30))
        sched.run_pending(_at(2026, 6, 1, 14, 0))  # 同一天再扫不重复
        sched.run_pending(_at(2026, 6, 2, 9, 30))  # 次日再触发
        assert calls == [1, 1]


class TestMonthlyJob:
    def test_fires_on_month_day(self, fake_weekday_calendar):
        calls = []
        sched = vs.VeraScheduler()
        sched.add_monthly("m", lambda: calls.append(1), day=1, hhmm="08:30")
        assert sched.run_pending(_at(2026, 6, 1, 8, 29)) == 0
        assert sched.run_pending(_at(2026, 6, 1, 8, 30)) == 1
        assert sched.run_pending(_at(2026, 6, 2, 8, 30)) == 0  # 当月不再触发
        assert sched.run_pending(_at(2026, 7, 1, 8, 30)) == 1  # 次月再触发
        assert calls == [1, 1]

    def test_rolls_to_next_trading_day(self, fake_weekday_calendar):
        """2026-08-01 是周六 → 顺延到 08-03 周一触发。"""
        calls = []
        sched = vs.VeraScheduler()
        sched.add_monthly("m", lambda: calls.append(1), day=1, hhmm="08:30")
        assert sched.run_pending(_at(2026, 8, 1, 9, 0)) == 0   # 周六不触发
        assert sched.run_pending(_at(2026, 8, 3, 8, 30)) == 1   # 顺延到周一
        assert sched.run_pending(_at(2026, 8, 4, 8, 30)) == 0   # 当月只此一次
        assert calls == [1]

    def test_invalid_day_rejected(self, fake_weekday_calendar):
        sched = vs.VeraScheduler()
        with pytest.raises(ValueError):
            sched.add_monthly("m", lambda: None, day=31)


class TestWeeklyJob:
    """add_weekly (2026-09-05 体检 P0-2): 每周日触发, 不看交易日。

    背景: 周度进化/sgpjbg 周报原用 daily+内部 weekday 检查 —— daily 要求
    交易日而周日休市, 周日分支永远不可达。weekly 语义按星期几直接触发。
    """

    def test_fires_on_sunday_ignoring_trading_day(self, fake_weekday_calendar):
        """2026-06-07 是周日 (非交易日) → 18:00 仍应触发。"""
        calls = []
        sched = vs.VeraScheduler()
        sched.add_weekly("w", lambda: calls.append(1), weekday=6, hhmm="18:00")
        assert sched.run_pending(_at(2026, 6, 7, 17, 59)) == 0
        assert sched.run_pending(_at(2026, 6, 7, 18, 0)) == 1
        assert calls == [1]

    def test_not_due_other_weekdays(self, fake_weekday_calendar):
        sched = vs.VeraScheduler()
        sched.add_weekly("w", lambda: None, weekday=6, hhmm="18:00")
        assert sched.run_pending(_at(2026, 6, 6, 18, 30)) == 0  # 周六
        assert sched.run_pending(_at(2026, 6, 8, 18, 30)) == 0  # 周一

    def test_no_double_fire_same_week_then_next_week(self,
                                                     fake_weekday_calendar):
        calls = []
        sched = vs.VeraScheduler()
        sched.add_weekly("w", lambda: calls.append(1), weekday=6, hhmm="18:00")
        sched.run_pending(_at(2026, 6, 7, 18, 30))    # 第一个周日
        sched.run_pending(_at(2026, 6, 7, 23, 0))     # 同日再扫不重复
        sched.run_pending(_at(2026, 6, 9, 18, 30))    # 周中不触发
        sched.run_pending(_at(2026, 6, 14, 18, 0))    # 第二个周日再触发
        assert calls == [1, 1]

    def test_invalid_weekday_rejected(self):
        sched = vs.VeraScheduler()
        with pytest.raises(ValueError):
            sched.add_weekly("w", lambda: None, weekday=7)


class TestFaultIsolation:
    def test_one_job_crash_does_not_block_others(self, fake_weekday_calendar):
        calls = []
        def boom():
            raise RuntimeError("模拟 job 崩溃")
        sched = vs.VeraScheduler()
        sched.add_daily("bad", boom, "09:00")
        sched.add_daily("good", lambda: calls.append(1), "09:00")
        fired = sched.run_pending(_at(2026, 6, 1, 9, 30))
        assert fired == 2          # 两个都到点触发了
        assert calls == [1]        # 好 job 照常执行
        # 下一周期调度器还活着, 好 job 继续跑
        sched.run_pending(_at(2026, 6, 2, 9, 30))
        assert calls == [1, 1]


class TestStartStop:
    def test_thread_fires_and_stops(self, monkeypatch):
        # 2026-08-01 修复: 本用例验的是线程启停机制, 与交易日历无关 ——
        # 原复用 fake_weekday_calendar (周一~周五), 周末跑套件时 daily job
        # 永不 due 必失败。改为恒交易日, 与日期解耦。
        monkeypatch.setattr(vs, "is_trading_day", lambda d: True)
        fired = threading.Event()
        sched = vs.VeraScheduler(tick_seconds=0.02)
        sched.add_daily("j", fired.set, "00:00")  # 任意时刻都到点
        sched.start(block=False)
        try:
            assert fired.wait(timeout=2.0), "后台线程未在 2s 内触发 job"
        finally:
            sched.stop()
        assert sched._thread is None


class TestIntervalJob:
    """add_interval: 盘中按固定间隔触发, 首次立即触发, 仅交易日+交易时段。"""

    _WIN = ("09:30-11:30", "13:00-15:00")  # 与 sentiment_pipeline.TRADING_HOURS 一致

    def test_first_fire_immediate_in_window(self, fake_weekday_calendar):
        calls = []
        sched = vs.VeraScheduler()
        sched.add_interval("tick", lambda: calls.append(1),
                           interval_min=10, trading_hours=self._WIN)
        # 10:00 在上午窗口内, 首次启动立即触发
        assert sched.run_pending(_at(2026, 6, 1, 10, 0)) == 1
        assert calls == [1]

    def test_not_due_before_window(self, fake_weekday_calendar):
        calls = []
        sched = vs.VeraScheduler()
        sched.add_interval("tick", lambda: calls.append(1),
                           interval_min=10, trading_hours=self._WIN)
        # 09:00 盘前 (窗口外) → 不触发
        assert sched.run_pending(_at(2026, 6, 1, 9, 0)) == 0
        assert calls == []

    def test_not_due_in_lunch_break(self, fake_weekday_calendar):
        calls = []
        sched = vs.VeraScheduler()
        sched.add_interval("tick", lambda: calls.append(1),
                           interval_min=10, trading_hours=self._WIN)
        # 12:00 午间休市 (两窗口之间) → 不触发
        assert sched.run_pending(_at(2026, 6, 1, 12, 0)) == 0

    def test_afternoon_window_fires(self, fake_weekday_calendar):
        calls = []
        sched = vs.VeraScheduler()
        sched.add_interval("tick", lambda: calls.append(1),
                           interval_min=10, trading_hours=self._WIN)
        # 14:00 下午窗口 → 首次触发
        assert sched.run_pending(_at(2026, 6, 1, 14, 0)) == 1

    def test_respects_interval(self, fake_weekday_calendar):
        calls = []
        sched = vs.VeraScheduler()
        sched.add_interval("tick", lambda: calls.append(1),
                           interval_min=10, trading_hours=self._WIN)
        assert sched.run_pending(_at(2026, 6, 1, 9, 30)) == 1   # 首次触发
        assert sched.run_pending(_at(2026, 6, 1, 9, 35)) == 0   # 5min < 10min 间隔
        assert sched.run_pending(_at(2026, 6, 1, 9, 40)) == 1   # 10min 到点再触发
        assert len(calls) == 2

    def test_weekend_does_not_fire(self, fake_weekday_calendar):
        """周末即便时刻在窗口内也不触发 (盘中只在交易日存在)。"""
        calls = []
        sched = vs.VeraScheduler()
        sched.add_interval("tick", lambda: calls.append(1),
                           interval_min=10, trading_hours=self._WIN)
        # 2026-06-06 周六 10:00 (窗口内但非交易日)
        assert sched.run_pending(_at(2026, 6, 6, 10, 0)) == 0
        assert calls == []

    def test_no_window_all_day(self, fake_weekday_calendar):
        """trading_hours=() 空 → 不受时段/交易日约束 (每日按间隔)。"""
        calls = []
        sched = vs.VeraScheduler()
        sched.add_interval("tick", lambda: calls.append(1),
                           interval_min=10, trading_hours=())
        # 周六 08:00, 无窗口约束 → 首次触发
        assert sched.run_pending(_at(2026, 6, 6, 8, 0)) == 1

    def test_interval_min_zero_raises(self):
        sched = vs.VeraScheduler()
        with pytest.raises(ValueError):
            sched.add_interval("tick", lambda: None, interval_min=0)

    def test_interval_min_negative_raises(self):
        sched = vs.VeraScheduler()
        with pytest.raises(ValueError):
            sched.add_interval("tick", lambda: None, interval_min=-5)


# ── 优雅停机 ─────────────────────────────────────────────────

class TestGracefulShutdown:
    def test_signal_sets_event(self):
        prev = signal.getsignal(signal.SIGINT)  # 先存原处理器 (install 会覆盖)
        ev = install()
        assert not ev.is_set()
        try:
            handler = signal.getsignal(signal.SIGINT)
            handler(signal.SIGINT, None)  # 模拟收到 SIGINT
            assert ev.is_set()
        finally:
            signal.signal(signal.SIGINT, prev)  # 恢复默认, 不污染其他测试


# ── 触发记录持久化 (2026-08-27 事件) ─────────────────────────
# 事件: 22:07 / 22:42 两次启动 VERA, 各补发一条舆情日报。
# 根因: last_fired 是进程内存态, 重启失忆 → 过了 hhmm 的 daily job 全补触发。
# 裁决 (用户): "没发要补发, 发过别重发" → state_path 持久化 last_fired。

class TestPersistentFiredState:
    def test_restart_does_not_refire_daily(self, tmp_path, fake_weekday_calendar):
        state = str(tmp_path / "scheduler_state.json")
        fired = []
        s1 = vs.VeraScheduler(state_path=state)
        s1.add_daily("sentiment_daily", lambda: fired.append(1), "15:05")
        # 2026-06-01 周一 22:07 (过了 15:05): 首次启动, 今天没发过 → 补发
        assert s1.run_pending(_at(2026, 6, 1, 22, 7)) == 1
        assert fired == [1]
        # 同进程内不重复
        assert s1.run_pending(_at(2026, 6, 1, 22, 30)) == 0
        # 模拟重启: 新实例读同一状态文件 → 今天已发过, 不再补发
        s2 = vs.VeraScheduler(state_path=state)
        s2.add_daily("sentiment_daily", lambda: fired.append(1), "15:05")
        assert s2.run_pending(_at(2026, 6, 1, 22, 42)) == 0
        assert fired == [1]
        # 次日 (周二) 重启: 新的一天, 照常触发
        s3 = vs.VeraScheduler(state_path=state)
        s3.add_daily("sentiment_daily", lambda: fired.append(1), "15:05")
        assert s3.run_pending(_at(2026, 6, 2, 22, 42)) == 1
        assert fired == [1, 1]

    def test_monthly_state_also_persisted(self, tmp_path, fake_weekday_calendar):
        state = str(tmp_path / "scheduler_state.json")
        fired = []
        s1 = vs.VeraScheduler(state_path=state)
        s1.add_monthly("monthly_note", lambda: fired.append(1), day=1, hhmm="08:30")
        assert s1.run_pending(_at(2026, 6, 1, 9, 0)) == 1
        s2 = vs.VeraScheduler(state_path=state)
        s2.add_monthly("monthly_note", lambda: fired.append(1), day=1, hhmm="08:30")
        assert s2.run_pending(_at(2026, 6, 1, 12, 0)) == 0  # 重启不补发
        assert fired == [1]

    def test_corrupt_state_fails_open(self, tmp_path, fake_weekday_calendar):
        """状态文件损坏 → 当作没发过 (宁可补发不可漏发, 用户裁决)。"""
        state = tmp_path / "scheduler_state.json"
        state.write_text("{broken json", encoding="utf-8")
        fired = []
        s = vs.VeraScheduler(state_path=str(state))
        s.add_daily("j", lambda: fired.append(1), "15:05")
        assert s.run_pending(_at(2026, 6, 1, 22, 7)) == 1

    def test_no_state_path_keeps_memory_only(self, fake_weekday_calendar):
        """不传 state_path: 行为与旧版完全一致 (纯内存防重)。"""
        fired = []
        s = vs.VeraScheduler()
        s.add_daily("j", lambda: fired.append(1), "15:05")
        assert s.run_pending(_at(2026, 6, 1, 22, 7)) == 1
        assert s.run_pending(_at(2026, 6, 1, 22, 42)) == 0
        assert fired == [1]
