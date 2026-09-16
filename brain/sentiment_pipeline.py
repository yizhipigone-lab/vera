"""brain/sentiment_pipeline.py — 舆情扫描编排层 (有 IO)。

run_sentiment_tick: 盘中每 interval_min 调一次, 编排完整一轮——
  拉新闻 → 去重 → LLM 打分 → 行情快照 → 规则判定 → 推送抑制 → 飞书推送。
run_daily_report: 盘后聚合日报 (v1-6 实现完整, 此处先占位)。

所有外部调用 (新闻源 / LLM / 行情 / 飞书) 全程 fail-soft: 任一环节挂了记
warning, 返回部分结果, 永不抛 —— 调度器单线程串行 (VeraScheduler.run_pending),
抛了会拖垮其他 job (月度笔记 / 周度进化)。

守业务铁律 1: 绝不 import trade/, 绝不触发交易。三重隔离断言在 v1 验收。
"""
from __future__ import annotations

import time
from pathlib import Path

from brain.alert_rules import (
    Alert, apply_push_suppression, load_config,
    rule_index_move, rule_sector_cluster, rule_stock_sentiment,
    rule_volume_anomaly,
)
from brain.news_dedup import NewsDedup, content_hash
from brain.sentiment_judge import judge_batch
from research.sentiment_notifier import get_default_notifier
from utils.logger import get_logger

logger = get_logger("brain.sentiment_pipeline")

_CONFIG_PATH = Path("config/sentiment.yaml")

# 盘中交易时段 (interval tick 仅此时段触发; 非时段 scheduler 跳过)
TRADING_HOURS = ("09:30-11:30", "13:00-15:00")


# ── 全局单例 (调度进程内复用, 避免每 tick 重开 DB) ─────────────────

_default_dedup: NewsDedup | None = None


def _get_dedup() -> NewsDedup:
    global _default_dedup
    if _default_dedup is None:
        _default_dedup = NewsDedup()
    return _default_dedup


def shutdown_dedup() -> None:
    """进程退出时关 DB (scheduler stop 时调)。"""
    global _default_dedup
    if _default_dedup is not None:
        _default_dedup.close()
        _default_dedup = None


# ── 外部数据源 (均 fail-soft, 测试可 monkeypatch) ──────────────────

def _fetch_watch_news(cfg: dict) -> list[dict]:
    """拉监控关键词相关新闻 (两源合并: 财新要闻 + 关键词搜索)。返 [{text,url}]。"""
    keywords = list(cfg.get("watch", {}).get("concepts", []))
    items: list[dict] = []
    # 源1: 财新要闻 (akshare, 全量聚合)
    try:
        from policy_pipeline.sources.news_search import fetch_news
        for n in fetch_news(limit=15):
            items.append({"text": n.get("text", ""), "url": n.get("url", ""),
                          "source": "caixin"})
    except Exception as e:
        logger.warning("财新要闻源失败 (跳过): %s", e)
    # 源2: 关键词搜索 (ddgs, 实时性更强; rate_limit 2s/次, 限 3 个关键词≈6s)
    for kw in keywords[:3]:
        try:
            from brain.search_web import search_web
            for r in search_web(kw, max_results=5, days=1):
                txt = (str(r.get("title", "")) + " " + str(r.get("snippet", ""))).strip()
                if txt:
                    items.append({"text": txt, "url": r.get("url", ""),
                                  "source": r.get("source", "search")})
        except Exception as e:
            logger.warning("search_web[%s] 失败 (跳过): %s", kw, e)
    return items


def _fetch_snapshot() -> str:
    """拉市场快照 (market_panel.market_snapshot fresh=True)。akshare 挂了返空串。"""
    try:
        from brain.market_panel import market_snapshot
        return market_snapshot(fresh=True) or ""
    except Exception as e:
        logger.warning("market_snapshot 拉取失败 (规则3/4 降级跳过): %s", e)
        return ""


def _alert_color(a: Alert) -> str:
    if a.polarity <= -0.4:
        return "red"
    if a.polarity >= 0.4:
        return "green"
    return "orange"


# ── 主编排: 一轮盘中扫描 ──────────────────────────────────────────

def run_sentiment_tick(cfg: dict | None = None, *, dedup: NewsDedup | None = None,
                       notifier=None) -> dict:
    """盘中舆情扫描一轮 (scheduler 每 interval_min 调一次)。全程 fail-soft。

    返回 {scanned, new, scored, alerts_fired, alerts_pushed}。
    dedup / notifier 可注入 (测试用); 默认全局单例。
    """
    cfg = cfg or load_config(_CONFIG_PATH)
    dedup = dedup or _get_dedup()
    notifier = notifier or get_default_notifier()
    now = time.time()
    stats = {"scanned": 0, "new": 0, "scored": 0,
             "alerts_fired": 0, "alerts_pushed": 0}

    # 1-2. 拉新闻 + 去重
    try:
        news = _fetch_watch_news(cfg)
    except Exception as e:
        logger.exception("拉新闻异常 (本轮跳过): %s", e)
        news = []
    stats["scanned"] = len(news)
    try:
        fresh = dedup.filter_unseen(news) if news else []
    except Exception as e:
        logger.warning("去重异常 (降级全当新): %s", e)
        fresh = news
    stats["new"] = len(fresh)

    # 3-4. 打分 + 标记已见
    scored: list[dict] = []
    if fresh:
        max_n = int(cfg.get("news", {}).get("max_per_scan", 15))
        batch = [{"text": n.get("text", ""), "url": n.get("url", ""), "ts": now}
                 for n in fresh[:max_n]]
        try:
            results = judge_batch(batch)
        except Exception as e:
            logger.exception("LLM 打分异常 (本轮无打分): %s", e)
            results = []
        for n, r in zip(fresh[:max_n], results):
            chash = n.get("_hash") or content_hash(n.get("text", ""), n.get("url"))
            pol = r.get("polarity") if isinstance(r, dict) and "error" not in r else None
            try:
                dedup.mark_news_seen(chash, polarity=pol, url=n.get("url"))
            except Exception:
                pass
            if isinstance(r, dict) and "error" not in r:
                r.setdefault("text", n.get("text", ""))
                scored.append(r)
    stats["scored"] = len(scored)

    # 5. 行情快照 (双保险: _fetch_snapshot 内部已 fail-soft, 此处兜底防未知异常穿透)
    try:
        snapshot = _fetch_snapshot()
    except Exception as e:
        logger.warning("行情快照异常 (规则3/4 降级跳过): %s", e)
        snapshot = ""

    # 6. 过四条规则
    alerts: list[Alert] = []
    alerts += rule_stock_sentiment(scored, cfg)
    alerts += rule_sector_cluster(scored, cfg)
    alerts += rule_index_move(snapshot, cfg, ts=now)
    alerts += rule_volume_anomaly(snapshot, cfg, ts=now)
    stats["alerts_fired"] = len(alerts)

    # 7. 推送抑制
    pushed = apply_push_suppression(alerts, dedup, cfg)

    # 8. 飞书推送 (notifier 内部已 fail-soft + worker 线程, 不阻塞) + 落库供日报聚合
    for a in pushed:
        try:
            notifier.notify_alert(
                a.to_payload(),
                title=f"舆情异动·{a.name or a.code}",
                color=_alert_color(a))
        except Exception as e:
            logger.warning("推送异常 (跳过该条): %s", e)
        try:  # 落库异动明细 (盘后日报聚合用); 失败不影响推送
            dedup.log_alert(a)
        except Exception as e:
            logger.warning("异动落库异常 (跳过该条): %s", e)
    stats["alerts_pushed"] = len(pushed)

    logger.info("舆情 tick 完成: %s", stats)
    return stats


# ── 盘后日报 (v1-6) ────────────────────────────────────────────────

def _today_start_ts() -> float:
    """今日 00:00 本地时间戳 (日报聚合窗口 = 当个交易日)。"""
    import datetime as _dt
    midnight = _dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def _build_daily_payload(summary: dict, ts: float) -> dict:
    """日报 summary → 飞书 _build_summary_card 所需 payload。"""
    alerts = summary.get("alerts", [])
    # top_movers: 全部异动按 |polarity|×strength 降序, top 10
    ranked = sorted(alerts, key=lambda a: abs(a.get("polarity") or 0)
                    * (a.get("strength") or 0), reverse=True)
    top_movers = [{"code": a.get("code") or "", "name": a.get("name") or "",
                   "polarity": a.get("polarity") or 0.0}
                  for a in ranked[:10]]
    # sector_heat: 板块类规则 (rule=sector_cluster) 按 name 聚合平均 polarity
    bucket: dict[str, list[float]] = {}
    for a in alerts:
        if a.get("rule") == "sector_cluster" and a.get("name"):
            bucket.setdefault(a["name"], []).append(a.get("polarity") or 0.0)
    sector_heat = [{"name": n, "score": sum(v) / len(v)}
                   for n, v in bucket.items()]
    sector_heat.sort(key=lambda s: abs(s["score"]), reverse=True)
    return {"date": time.strftime("%Y-%m-%d", time.localtime(ts)),
            "ts": ts, "top_movers": top_movers, "sector_heat": sector_heat[:8]}


def run_daily_report(cfg: dict | None = None, *, dedup: NewsDedup | None = None,
                     notifier=None) -> dict:
    """盘后舆情日报 (15:05 推送)。聚合当日 alert_log → 飞书 summary card。

    全程 fail-soft: 聚合/推送任一环节挂了返空统计, 永不抛 (调度器串行, 抛了
    拖垮其他 job)。无异动也推一张"今日无显著异动"卡 (证监控存活)。
    """
    cfg = cfg or load_config(_CONFIG_PATH)
    dedup = dedup or _get_dedup()
    notifier = notifier or get_default_notifier()
    now = time.time()
    since = _today_start_ts()

    try:
        summary = dedup.daily_alert_summary(since)
    except Exception as e:
        logger.warning("日报聚合异常 (降级空): %s", e)
        summary = {"alerts": [], "bullish": 0, "bearish": 0, "total": 0}

    payload = _build_daily_payload(summary, now)
    try:
        notifier.notify_summary(payload, title="舆情日报")
    except Exception as e:
        logger.warning("日报推送异常: %s", e)

    logger.info("日报推送: alerts=%s bullish=%s bearish=%s",
                summary["total"], summary["bullish"], summary["bearish"])
    return {"alerts": summary["total"], "bullish": summary["bullish"],
            "bearish": summary["bearish"]}
