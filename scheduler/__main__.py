"""scheduler/__main__.py — `python -m scheduler` 独立进程入口。

独立进程 = 后台周期 job 容器: 月度笔记 / 周度进化 / 盘中舆情扫描。
不碰 VERA 主程序: 只读 trade.db (notes_gen); 舆情模块物理隔离 trade/ (AST 断言
绝不 import trade, 守业务铁律 1: 情绪/舆情只报告显示, 不联入任何交易决策)。
进程日志落盘 output/logs/scheduler.log (2026-09-05 体检 P0-1, 见 main 内 attach)。
注册 job:
    - 每月 1 日 (顺延到下一交易日) 08:30 生成上月月度笔记 (notes_gen)
    - 每周日 18:00 周度自进化 (evolution; weekly 语义不看交易日, P0-2 修复)
    - 盘中每 interval_min 分钟一轮舆情扫描 (brain.sentiment_pipeline,
      仅交易日+交易时段触发; 配置 config/sentiment.yaml, 注册失败不影响前两个 job)
    - 每交易日盘后 (默认 15:05) 舆情日报聚合推送 (run_daily_report)
    - 每交易日盘后 15:45 K 线缓存补尾段 (5m→1d→1m, 子进程 fire-and-forget,
      防"缓存过期→回测全量重拉"集中爆发, 2026-08-14 5M 回测超时事故)
    - 每日 17:30 sgpjbg 研究热度雷达抓取 (免费元数据落库, 2026-09-04 决策)
    - 每周日 18:30 机构研究动向周报 (独立周报; weekly 语义不看交易日, P0-2 修复)
    - 每交易日 15:50 大盘位置采集 (读本地日线缓存落连续录像; 排在 15:45 缓存
      补尾段之后); **15:55 补采 + 推「大盘体温表」到飞书**; 次日 09:05 只补采
      (兜底不推送)。2026-09-17 用户纠错: 体温表是收盘价算出来的, 天生属于"盘后",
      原"次日 09:05 推"实测与前一天 15:50 得到同一天数据、记录逐字段相同 → 零新信息

优雅停机: SIGTERM/SIGINT → graceful_shutdown 的 Event → stop() + 关 dedup。
"""
from __future__ import annotations

import argparse

from dotenv import load_dotenv

from scheduler.graceful_shutdown import install
from scheduler.vera_scheduler import VeraScheduler
from utils.logger import attach_file_logger, get_logger
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
    """周度自进化 job: 每周日 18:00 由 weekly 调度触发 (复盘+教训入 playbook)。"""
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


# ── sgpjbg 研究热度雷达 (2026-09-04 决策记录第二批) ─────────────────────
# 只抓免费元数据落库 + 周日生成独立周报; 绝不联入交易决策（同舆情铁律）。


def _job_sgpjbg_fetch() -> None:
    """每日抓 sgpjbg 最新报告元数据落库（模块内部全程 fail-soft）。"""
    from brain.sgpjbg_radar import fetch_latest
    stats = fetch_latest()
    _logger.info("sgpjbg 抓取: %s", stats)


def _job_sgpjbg_weekly() -> None:
    """机构研究动向周报 job: 每周日 18:30 由 weekly 调度触发 (独立周报)。"""
    from brain.sgpjbg_radar import weekly_report
    path = weekly_report()
    if path is None:
        _logger.warning("sgpjbg 周报生成跳过 (近 7 天无数据)")
    else:
        _logger.info("sgpjbg 周报已生成: %s", path)


# ── 大盘位置连续录像 (2026-09-17) ─────────────────────────────
# 三个 job 分工 (2026-09-17 用户指出设计错误后重定):
#   15:50  只采集 —— 让页面当晚就是新数据
#   15:55  **补采 + 推「大盘体温表」** —— 体温表是收盘价算出来的, 天生属于"盘后"
#   09:05  只补采 (不推送) —— 兜底用; 隔夜简报尚未实现
#
# 【为什么不在早上推】(用户 2026-09-17 指出 + 实测确认)
#   `expected_last_trading_day()` 在"当日 15:50"与"次日 09:05"返回**同一天**
#   (次日 09:05 还没到 15:00, 函数回退到上一交易日) → 两次 collect 算出的记录
#   **逐字段相同** → 早上那张卡一个新数字都没有, 等于把昨天的作业今早再交一遍。
#   体温表用的全是当天收盘价, 拖到第二天早上不会变。
#   计划书: docs/plan/2026-09-17_大盘位置与趋势研判_计划书.md §17
#
# 采集读本地日线缓存, 不联网、不触发补拉。


def _job_market_position_collect() -> None:
    """每交易日 15:50: 采集大盘位置落连续录像 (只采集, 不推送)。

    排在 15:45 K 线缓存补尾段之后 —— 缓存没刷新的话指标就是旧的。
    """
    from core import market_position_runner as mpr
    res = mpr.collect(bars=mpr.DEFAULT_BARS, write=True)
    if not res.get("ok"):
        _logger.warning("大盘位置采集失败: %s", res.get("reason"))
        return
    _logger.info("大盘位置: 数据日期 %s%s, 本次 %d 条, 录像共 %d 条",
                 res["asof"], " (滞后)" if res["stale"] else "",
                 res["records"], res["lines"])


def _job_market_position_push() -> None:
    """每交易日 15:55: **先补采 → 再推「盘后复盘」（内含大盘体温表）**。

    两个设计点，都有实测依据：

    1. **为什么这里要再采集一次**（15:50 已经采过）：采集幂等且只要十几秒，
       而"只推送不采集"有个真实缺口 —— 若 15:50 那次因 TDX 断/机器卡而失败，
       15:55 就会把**昨天那条旧记录**推出去（2026-09-17 手动验证时实测复现过）。
       先采后推把这条缺口堵死。
    2. **为什么推的是「复盘」而不是单独的体温表**（2026-09-17 M6 合并）：
       大盘位置计划书 §17.4 定的是「体温表与复盘报告合成一条消息」——
       两个 job 各推一次会让用户在同一天同一刻收到两条内容重叠的卡。
       复盘报告本身就把体温表当**盘面段**复用（`notes_gen/daily.py` 单向 import），
       所以这里推一次复盘 = 体温表 + 账户 + 留意清单，一条看完。
    """
    from core import market_position_runner as mpr
    res = mpr.collect(bars=mpr.DEFAULT_BARS, write=True)
    if not res.get("ok"):
        _logger.warning("大盘位置 15:55 补采失败 (仍尝试出复盘): %s",
                        res.get("reason"))
    elif res.get("stale"):
        _logger.warning("大盘位置数据滞后 (数据日期 %s, 应有 %s) —— 报告会带滞后标记; "
                        "排查: 跑 python tools/market_position_collect.py 看 reason",
                        res.get("asof"), res.get("expected"))
    try:
        from notes_gen import daily as daily_review
        r = daily_review.run_daily_review(write=True, push=True)
    except Exception as e:       # 报告线失败绝不影响别的 job
        _logger.warning("盘后复盘生成/推送失败: %s", e)
        return
    push = r.get("push") or {}
    if push.get("ok"):
        _logger.info("盘后复盘已推飞书: %s 张卡 (落盘 %s)",
                     (push.get("feishu") or {}).get("cards"), r.get("path"))
    else:
        _logger.info("盘后复盘未推送: %s", push.get("feishu"))


def _job_market_position_morning() -> None:
    """每交易日 09:05: **兜底补采 + 推「隔夜简报」**。

    两件事，顺序不能反：
    1. **兜底补采**：机器昨天 15:50/15:55 若没开机，早上把大盘位置录像补齐，
       让页面数据不滞后。**这一步不推任何东西** —— 实测证伪过「早上推体温表」
       （15:50 与次日 09:05 的数据同一天、逐字段相同，等于把昨天作业今早再交一遍）。
    2. **隔夜简报**（2026-09-17 用户拍板选 (b)，M7 补做）：只放"过了一夜新发生的"
       —— 美股隔夜收盘 / 港股 / 南向资金（复用 `brain.market_panel.market_snapshot()`），
       体温表**只作一句背景**。**没有隔夜新信息就不发空卡**；昨天 15:55 没推成则附带补发。

    计划书: docs/plan/2026-09-17_大盘位置与趋势研判_计划书.md §17.4
    """
    from core import market_position_runner as mpr
    res = mpr.collect(bars=mpr.DEFAULT_BARS, write=True)
    if not res.get("ok"):
        _logger.warning("大盘位置盘前兜底补采失败: %s", res.get("reason"))
    else:
        _logger.info("大盘位置盘前兜底补采完成: 数据日期 %s, 录像共 %d 条",
                     res["asof"], res["lines"])
    try:
        from notes_gen import morning as brief
        r = brief.run_morning_brief(push=True, write=True)
    except Exception as e:      # 简报失败绝不影响别的 job
        _logger.warning("隔夜简报生成/推送失败: %s", e)
        return
    if r.get("ok"):
        _logger.info("隔夜简报已处理: %s", {k: v for k, v in r.items() if k != "push"})
    else:
        _logger.info("隔夜简报未推送: %s", r.get("reason"))


def _register_market_position(sched: VeraScheduler) -> None:
    """注册大盘位置三个 job（2026-09-17：抽取成函数以便单测直接验时刻）。

    见文件头注释「为什么不在早上推」。**推送时刻必须 ≥15:45**（缓存补尾段之后），
    否则会拿旧数据出报告 —— 由 `tests/test_scheduler_main.py` 的回归锁看着。
    """
    sched.add_daily("market_position_collect", _job_market_position_collect,
                    hhmm="15:50")
    sched.add_daily("market_position_push", _job_market_position_push,
                    hhmm="15:55")
    sched.add_daily("market_position_morning", _job_market_position_morning,
                    hhmm="09:05")


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

    # 进程日志落盘 (2026-09-05 体检 P0-1): 此前日志只进控制台窗口,
    # 关窗即丢 → 调度停摆/内部错误无从复查。挂到 root, 各模块沿
    # propagate 一并落文件; 幂等, 重复启动不双写。
    attach_file_logger(str(project_root() / "output" / "logs"
                          / "scheduler.log"))
    _logger.info("scheduler 文件日志: %s", project_root() / "output" / "logs"
                 / "scheduler.log")

    # 触发记录持久化 (2026-08-27 双发事件修复): 重启记得"今天发过了",
    # 过了点的 daily job 真没发过才补发, 发过不重发。
    sched = VeraScheduler(
        state_path=str(project_root() / "data" / "scheduler_state.json"))
    # 月度笔记: 触发日(9/1)错过会因断档永久丢失 (体检 P2-2 教训) →
    # 触发日起 7 天内补发一次; 已发/超窗不补。
    sched.add_monthly("monthly_note", _job_monthly_note, day=1, hhmm="08:30",
                      catchup_days=7)
    # 周度 job 用 add_weekly (不看交易日, 周日休市也要跑) —
    # 2026-09-05 体检 P0-2: daily+weekday 检查组合下周日分支不可达
    sched.add_weekly("weekly_evolution", _job_weekly_evolution,
                     weekday=6, hhmm="18:00")
    sched.add_daily("kline_cache_refresh", _job_kline_cache_refresh, hhmm="15:45")
    sched.add_daily("sgpjbg_fetch", _job_sgpjbg_fetch, hhmm="17:30")
    sched.add_weekly("sgpjbg_weekly", _job_sgpjbg_weekly,
                     weekday=6, hhmm="18:30")
    # 大盘位置: 15:50 采集(排在 15:45 缓存补尾段之后) → 15:55 补采+推体温表
    # → 次日 09:05 只补采(兜底, 不推送)。详见 _register_market_position。
    _register_market_position(sched)
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
