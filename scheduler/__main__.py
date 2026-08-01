"""scheduler/__main__.py — `python -m scheduler` 独立进程入口。

独立进程 = 不碰 VERA 主程序: 只读 trade.db, 产出月度笔记到 notes/。
注册两个 job:
    - 每月 1 日 (顺延到下一交易日) 08:30 生成上月月度笔记 (notes_gen)
    - 每交易日 18:00 检查, 只在周日真正执行周度自进化 (evolution: 复盘+教训入 playbook)

优雅停机: SIGTERM/SIGINT → graceful_shutdown 的 Event → stop()。
"""
from __future__ import annotations

import argparse

from scheduler.graceful_shutdown import install
from scheduler.vera_scheduler import VeraScheduler
from utils.logger import get_logger

_logger = get_logger("scheduler.main")


def _job_monthly_note() -> None:
    """月度笔记 job: 委托 notes_gen, 失败已在其内部兜底 (松耦合)。"""
    from notes_gen.monthly import generate_monthly_note
    path = generate_monthly_note()  # month=None → 上一自然月
    if path is None:
        _logger.warning("月度笔记生成失败 (详见 notes_gen 日志), 本周期跳过")
    else:
        _logger.info("月度笔记已生成: %s", path)


def _job_weekly_evolution() -> None:
    """周度自进化 job: 每日检查, 只在周日真正执行 (复盘+教训入 playbook)。"""
    import datetime as dt
    if dt.date.today().weekday() != 6:  # 6 = 周日
        return
    from evolution.weekly import run_weekly_evolution
    path = run_weekly_evolution()
    if path is None:
        _logger.warning("周度进化失败 (详见 evolution 日志), 本周跳过")
    else:
        _logger.info("周度进化报告已生成: %s", path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m scheduler",
        description="VERA 定时调度独立进程（月度笔记等，不碰 VERA 主程序）")
    ap.parse_args(argv)  # 目前无参数, 仅让 --help 正常打印退出 (不直接阻塞)

    stop_event = install()

    sched = VeraScheduler()
    sched.add_monthly("monthly_note", _job_monthly_note, day=1, hhmm="08:30")
    sched.add_daily("weekly_evolution", _job_weekly_evolution, hhmm="18:00")
    sched.start(block=False)

    _logger.info("scheduler 独立进程运行中, 等待退出信号 (Ctrl+C / SIGTERM)...")
    try:
        stop_event.wait()  # 阻塞到收到退出信号
    except KeyboardInterrupt:  # 双保险: 信号注册失败时仍能 Ctrl+C 退出
        _logger.info("KeyboardInterrupt, 准备停机")
    finally:
        sched.stop()
    _logger.info("scheduler 独立进程已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
