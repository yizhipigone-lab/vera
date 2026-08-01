"""scheduler/vera_scheduler.py — 轻量定时器 (纯 stdlib: threading + time)。

落地偏差说明: 计划书写的是 `schedule` pip 库, 此处刻意改用 stdlib
(threading.Event.wait + 轮询到点判断) 实现 —— 零第三方依赖、无安装/
卸载负担, 接口面 (add_daily / add_monthly / start / stop) 已足够小,
不值得为一个 cron 循环引入外部包。

铁律:
    - 只在交易日触发 (trading_calendar 判断); 月度 job 顺延到下一交易日。
    - 每个 job 独立 try/except: 单个 job 崩了记日志继续跑 (松耦合)。
    - 触发判定与时间源解耦: run_pending(now=...) 可注入时间, 便于测试。
"""
from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass
from typing import Callable

from scheduler.trading_calendar import is_trading_day, next_trading_day
from utils.logger import get_logger

_logger = get_logger("scheduler.vera_scheduler")


@dataclass
class _Job:
    """一个已注册的定时任务。"""
    name: str
    func: Callable[[], None]
    hhmm: str                    # "HH:MM" 触发时刻
    kind: str = "daily"          # "daily" | "monthly"
    month_day: int = 1           # monthly: 每月第几日 (顺延到下一交易日)
    last_fired: str = ""         # 最近一次触发的周期键 (防同一周期重复触发)


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    hh, mm = hhmm.split(":")
    return int(hh), int(mm)


def monthly_fire_date(year: int, month: int, day: int) -> dt.date:
    """月度 job 的实际触发日: 当月第 day 日顺延到下一个交易日 (纯函数)。"""
    d = dt.date(year, month, day)
    return d if is_trading_day(d) else next_trading_day(d)


def _period_key(job: _Job, d: dt.date) -> str:
    """防重键: daily 按天, monthly 按月。"""
    return d.isoformat() if job.kind == "daily" else d.strftime("%Y-%m")


def is_due(job: _Job, now: dt.datetime) -> bool:
    """纯函数: 该 job 在 now 时刻是否到点应触发。"""
    today = now.date()
    hh, mm = _parse_hhmm(job.hhmm)
    if (now.hour, now.minute) < (hh, mm):
        return False
    if job.kind == "daily":
        if not is_trading_day(today):
            return False
    else:  # monthly: 只在"顺延后的触发日"当天触发
        if monthly_fire_date(today.year, today.month, job.month_day) != today:
            return False
    return _period_key(job, today) != job.last_fired


class VeraScheduler:
    """轻量定时器。注册 job → start 后台轮询 → stop 优雅停。"""

    def __init__(self, tick_seconds: float = 1.0):
        self._jobs: list[_Job] = []
        self._tick = tick_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ── 注册 ─────────────────────────────────────────────────

    def add_daily(self, name: str, func: Callable[[], None], hhmm: str) -> _Job:
        """注册每日 job: 每个交易日 hhmm 过后触发一次。"""
        _parse_hhmm(hhmm)  # 提前校验格式
        job = _Job(name=name, func=func, hhmm=hhmm, kind="daily")
        with self._lock:
            self._jobs.append(job)
        return job

    def add_monthly(self, name: str, func: Callable[[], None],
                    day: int = 1, hhmm: str = "08:30") -> _Job:
        """注册月度 job: 每月第 day 日 (顺延到下一交易日) hhmm 过后触发。"""
        _parse_hhmm(hhmm)
        if not 1 <= day <= 28:
            raise ValueError(f"monthly day 限 1~28 (避免月末天数不齐): {day}")
        job = _Job(name=name, func=func, hhmm=hhmm, kind="monthly", month_day=day)
        with self._lock:
            self._jobs.append(job)
        return job

    # ── 触发 ─────────────────────────────────────────────────

    def run_pending(self, now: dt.datetime | None = None) -> int:
        """扫一遍所有 job, 触发到点的。返回触发数。单个 job 异常不拖垮其他。"""
        now = now or dt.datetime.now()
        with self._lock:
            jobs = list(self._jobs)
        fired = 0
        for job in jobs:
            try:
                if not is_due(job, now):
                    continue
            except Exception as e:  # 判定本身也可能出意外 (松耦合)
                _logger.warning("job [%s] 到点判定异常, 本周期跳过: %s", job.name, e)
                continue
            job.last_fired = _period_key(job, now.date())
            fired += 1
            try:
                _logger.info("job [%s] 触发 (%s %s)", job.name,
                             now.date().isoformat(), job.hhmm)
                job.func()
            except Exception as e:  # 松耦合: 单 job 崩了记日志继续跑
                _logger.exception("job [%s] 执行失败 (不影响其他 job): %s", job.name, e)
        return fired

    # ── 生命周期 ─────────────────────────────────────────────

    def start(self, block: bool = True) -> None:
        """启动调度循环。block=True 在当前线程跑; False 起 daemon 线程。"""
        if block:
            self._loop()
            return
        if self._thread is not None and self._thread.is_alive():
            _logger.warning("scheduler 已在运行, 忽略重复 start")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="vera-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """置停止标志并等后台线程退出。block=True 模式下由循环内检查退出。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        _logger.info("scheduler 启动, 已注册 %d 个 job", len(self._jobs))
        while not self._stop.is_set():
            try:
                self.run_pending()
            except Exception as e:  # 双保险: run_pending 内部已逐 job 兜底
                _logger.exception("scheduler 轮询异常 (继续跑): %s", e)
            self._stop.wait(self._tick)
        _logger.info("scheduler 停止")
