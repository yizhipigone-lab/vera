"""scheduler/__main__.py — `python -m scheduler` 独立进程入口。

独立进程 = 后台周期 job 容器: 月度笔记 / 周度进化 / 盘中舆情扫描。
不碰 VERA 主程序: 只读 trade.db (notes_gen); 舆情模块物理隔离 trade/ (AST 断言
绝不 import trade, 守业务铁律 1: 情绪/舆情只报告显示, 不联入任何交易决策)。
注册 job:
    - 每月 1 日 (顺延到下一交易日) 08:30 生成上月月度笔记 (notes_gen)
    - 每交易日 18:00 检查, 只在周日真正执行周度自进化 (evolution)
    - 盘中每 interval_min 分钟一轮舆情扫描 (brain.sentiment_pipeline,
      仅交易日+交易时段触发; 配置 config/sentiment.yaml, 注册失败不影响前两个 job)
    - 每交易日盘后 (默认 15:05) 舆情日报聚合推送 (run_daily_report)
    - 每交易日盘后 15:45 K 线缓存补尾段 (5m→1d→1m, 子进程 fire-and-forget,
      防"缓存过期→回测全量重拉"集中爆发, 2026-08-14 5M 回测超时事故)

优雅停机: SIGTERM/SIGINT → graceful_shutdown 的 Event → stop() + 关 dedup。
"""
from __future__ import annotations

import argparse

from dotenv import load_dotenv

from scheduler.graceful_shutdown import install
from scheduler.vera_scheduler import VeraScheduler
from utils.logger import get_logger
from utils.sysutil import project_root

# 加载项目根 .env → FEISHU_WEBHOOK_URL 等 (舆情推送 webhook)。
# 2026-08-14 修复: 此前 scheduler 不加载 .env, 舆情推送器读 os.environ 拿不到
# webhook 直接 no-op (只在日志留一行"未配置环境变量"), 用户收不到舆情卡片。
# trade_main.py 同样用 load_dotenv, 此处对齐。
load_dotenv(project_root() / ".env")

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


def _job_sentiment_tick() -> None:
    """盘中舆情扫描一轮 (run_sentiment_tick 内部已全程 fail-soft, 永不抛;
    scheduler.run_pending 另有外层 try/except 兜底, 双保险)。"""
    from brain.sentiment_pipeline import run_sentiment_tick
    stats = run_sentiment_tick()
    _logger.info("舆情 tick: scanned=%s new=%s pushed=%s",
                 stats.get("scanned"), stats.get("new"), stats.get("alerts_pushed"))


def _job_sentiment_daily() -> None:
    """盘后舆情日报 (run_daily_report 内部 fail-soft, 永不抛)。"""
    from brain.sentiment_pipeline import run_daily_report
    stats = run_daily_report()
    _logger.info("舆情日报: alerts=%s bullish=%s bearish=%s",
                 stats.get("alerts"), stats.get("bullish"), stats.get("bearish"))


# ── 每日盘后 K 线缓存补尾段 (2026-08-14) ─────────────────────
#
# 背景: 5M 回测取数超时事故 —— 缓存尾段停在 07-31, 回测时几百只票逐只走 TDX
# 补尾段 + 分红 shift 全量重拉, 取数 1 小时+。检查/补拉逻辑在
# core/kline_cache_maintenance.py（与 server 启动自检共用同一入口，双保险:
# 定时任务兜底 + 启动自检防"调度器没常驻/电脑关机"漏跑）。


def _job_kline_cache_refresh() -> None:
    """每交易日盘后触发: 委托 core.kline_cache_maintenance（新鲜则秒回 no-op，
    不新鲜则后台线程+子进程补拉，不阻塞调度循环）。"""
    from core.kline_cache_maintenance import ensure_cache_fresh
    r = ensure_cache_fresh(trigger="scheduler_daily")
    _logger.info("盘后缓存检查: %s", r)


def _register_sentiment(sched: VeraScheduler) -> None:
    """注册盘中舆情扫描 interval job。

    间隔读 config/sentiment.yaml (缺失走 DEFAULT_CONFIG); brain 导入/配置异常则
    跳过注册 —— 月度笔记 / 周度进化照常跑 (fail-soft 分级: 单功能挂 ≠ 进程挂)。
    """
    try:
        from pathlib import Path
        from brain.alert_rules import load_config
        from brain.sentiment_pipeline import TRADING_HOURS
        cfg = load_config(Path("config/sentiment.yaml"))
        interval = float(cfg.get("scan", {}).get("interval_min", 10))
        daily_hhmm = cfg.get("scan", {}).get("daily_report_hhmm", "15:05")
        sched.add_interval("sentiment_tick", _job_sentiment_tick,
                           interval_min=interval, trading_hours=TRADING_HOURS)
        sched.add_daily("sentiment_daily", _job_sentiment_daily, hhmm=daily_hhmm)
        # 启动飞书推送 worker 线程 (daemon; 必须显式 start, 否则队列只入不出 → 告警全丢。
        # 测试用 FakeNotifier 注入测不到此洞, 故在此显式启动。webhook 未配则 no-op)
        from research.sentiment_notifier import get_default_notifier
        get_default_notifier().start()
        _logger.info("舆情扫描已注册: 盘中每 %g 分钟 + 日报 %s", interval, daily_hhmm)
    except Exception as e:
        _logger.warning("舆情扫描注册失败 (跳过, 不影响其他 job): %s", e)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m scheduler",
        description="VERA 定时调度独立进程（月度笔记等，不碰 VERA 主程序）")
    ap.parse_args(argv)  # 目前无参数, 仅让 --help 正常打印退出 (不直接阻塞)

    stop_event = install()

    sched = VeraScheduler()
    sched.add_monthly("monthly_note", _job_monthly_note, day=1, hhmm="08:30")
    sched.add_daily("weekly_evolution", _job_weekly_evolution, hhmm="18:00")
    sched.add_daily("kline_cache_refresh", _job_kline_cache_refresh, hhmm="15:45")
    _register_sentiment(sched)
    sched.start(block=False)

    _logger.info("scheduler 独立进程运行中, 等待退出信号 (Ctrl+C / SIGTERM)...")
    try:
        stop_event.wait()  # 阻塞到收到退出信号
    except KeyboardInterrupt:  # 双保险: 信号注册失败时仍能 Ctrl+C 退出
        _logger.info("KeyboardInterrupt, 准备停机")
    finally:
        sched.stop()
        try:  # drain 飞书推送队列后停 worker (若本轮起过); 失败不阻塞退出
            from research.sentiment_notifier import get_default_notifier
            get_default_notifier().stop()
        except Exception:
            pass
        try:  # 关舆情去重库 (若本轮起过); 失败不阻塞退出
            from brain.sentiment_pipeline import shutdown_dedup
            shutdown_dedup()
        except Exception:
            pass
    _logger.info("scheduler 独立进程已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
