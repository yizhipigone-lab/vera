# -*- coding: utf-8 -*-
"""sentiment_api.py — /api/sentiment/* 路由 (2026-09-20)。

薄层：只做参数解析 + 轻量聚合 + 转发。取数一律走属主模块：
- 异动台账 → `brain.news_dedup.NewsDedup.alerts_between`（store 契约）
- 机构研究雷达 → `brain.sgpjbg_radar.recent_reports`（SQL 留在属主内，本层不碰 schema）

三个端点（薄路由按 HTTP 端点计，不按方法数计）：
- `GET /api/sentiment/alerts`   舆情异动台账（区间 + 规则/标的筛选，只读）
- `GET /api/sentiment/summary`  近 N 天分布（按日 / 按规则 / 标的 Top）
- `GET /api/sentiment/sgpjbg`   机构研究雷达（周报列表 / 指定周报正文 / 近期报告元数据）

**只读**：本模块不提供任何写端点，打开页面绝不产生数据。

铁律 1（最高优先）：本文件绝不 import `trade`。舆情是"只报告显示、不联仓位调度"，
`tests/test_sentiment_api.py` 有 AST 静态断言（照 `test_market_position.py` 的写法）。
铁律 8：路由层按端点计，实现全部下沉到属主深模块。

页面口径纪律（2026-09-20）：本页是**台账**（"当时发生了什么"），不是**指标**
（"接下来会怎样"）。故只给计数与原始记录，**不给评分、不给权重、不给总分** ——
`core/market_validity.py` 已证伪宽度类指标的预测力，舆情同族。
"""
from __future__ import annotations

import datetime as dt
import re
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, Query

from utils.logger import get_logger
from utils.sysutil import project_root

logger = get_logger(__name__)

router = APIRouter()

_REPORTS_DIR = project_root() / "output" / "reports"
# 周报文件名白名单（同时用于列表识别与路径穿越防护）
_WEEKLY_RE = re.compile(r"^sgpjbg_研究雷达周报_(\d{4}-\d{2}-\d{2})\.md$")
_MAX_LIMIT = 5000


# ── 小工具 ───────────────────────────────────────────────────────────

def _day_bounds(date_str: str) -> tuple[float, float]:
    """'YYYY-MM-DD' → (当日 00:00:00, 23:59:59) 的 Unix 秒（本地时区）。

    数据里的 ts 是本地时间戳（`news_dedup` 用 `time.time()`），故此处也用本地解析。
    格式不对抛 ValueError，由调用方转成可读错误 —— 不静默。
    """
    d = dt.datetime.strptime(date_str, "%Y-%m-%d")
    start = d.timestamp()
    return start, start + 86400 - 1


def _ts_fields(ts: int) -> dict:
    t = dt.datetime.fromtimestamp(ts)
    return {"date": t.strftime("%Y-%m-%d"), "time": t.strftime("%H:%M:%S")}


@contextmanager
def _open_dedup():
    """打开舆情库。**测试用 monkeypatch 替换本函数**，不碰生产库。

    `NewsDedup` 是 `data/sentiment/news_seen.db` 的唯一入口（铁律：不写第二份 SQL）。
    """
    from brain.news_dedup import NewsDedup
    d = NewsDedup()
    try:
        yield d
    finally:
        d.close()


def _rule_names() -> dict:
    from brain.alert_rules import RULE_NAMES
    return RULE_NAMES


# ── 端点 ─────────────────────────────────────────────────────────────

@router.get("/api/sentiment/alerts")
async def sentiment_alerts(
    date: str | None = Query(None, description="YYYY-MM-DD，给了就只看这一天"),
    start: str | None = Query(None, description="区间起点 YYYY-MM-DD"),
    end: str | None = Query(None, description="区间终点 YYYY-MM-DD"),
    rule: str | None = Query(None, description="按规则筛：stock_sentiment / sector_cluster / index_move / volume_anomaly"),
    code: str | None = Query(None, description="按标的代码筛"),
    limit: int = Query(500, ge=1, le=_MAX_LIMIT),
):
    """舆情异动台账（只读）。

    优先级：`date` > (`start`,`end`) > 今天。`alerts_between` 已按 ts 倒序。
    库读不到 → 返回空结构 + `note`，**不 500**（页面显示"暂不可用"而不是崩）。
    """
    try:
        if date:
            s, e = _day_bounds(date)
            label = date
        else:
            s_date = start or end or dt.date.today().isoformat()
            e_date = end or start or dt.date.today().isoformat()
            s, _ = _day_bounds(s_date)
            _, e = _day_bounds(e_date)
            label = s_date if s_date == e_date else f"{s_date} ~ {e_date}"
    except ValueError as ex:
        return {"success": False, "error": f"日期格式应为 YYYY-MM-DD（{ex}）"}

    rn = _rule_names()
    try:
        with _open_dedup() as dedup:
            rows = dedup.alerts_between(s, e, rule=rule, code=code, limit=limit)
    except Exception as ex:      # 库缺失 / 表缺失 / 锁 —— 一律降级，不让页面崩
        logger.warning("舆情台账读取异常 (降级空): %s", ex)
        return {"success": True, "range": label, "items": [], "total": 0,
                "bullish": 0, "bearish": 0, "rule_names": rn,
                "note": f"舆情库暂不可用：{ex}"}

    items = [{**r, **_ts_fields(r["ts"]),
              "rule_name": rn.get(r["rule"], r["rule"])} for r in rows]
    return {
        "success": True, "range": label, "items": items, "total": len(items),
        "bullish": sum(1 for r in rows if (r.get("polarity") or 0) > 0),
        "bearish": sum(1 for r in rows if (r.get("polarity") or 0) < 0),
        "rule_names": rn,
    }


@router.get("/api/sentiment/summary")
async def sentiment_summary(days: int = Query(30, ge=1, le=365)):
    """近 N 天分布：按日计数 / 按规则计数 / 标的 Top（**纯计数，不是评分**）。

    只做台账级统计，不做任何加权或预测性指标（见模块 docstring 的口径纪律）。
    """
    end = dt.date.today()
    start = end - dt.timedelta(days=days - 1)
    s, _ = _day_bounds(start.isoformat())
    _, e = _day_bounds(end.isoformat())
    rn = _rule_names()
    try:
        with _open_dedup() as dedup:
            rows = dedup.alerts_between(s, e, limit=_MAX_LIMIT)
    except Exception as ex:
        logger.warning("舆情分布读取异常 (降级空): %s", ex)
        return {"success": True, "days": days, "by_day": [], "by_rule": [],
                "top_codes": [], "total": 0, "note": f"舆情库暂不可用：{ex}"}

    by_day: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    codes: dict[str, dict] = {}
    for r in rows:
        by_day[_ts_fields(r["ts"])["date"]] = by_day.get(_ts_fields(r["ts"])["date"], 0) + 1
        by_rule[r["rule"]] = by_rule.get(r["rule"], 0) + 1
        k = r.get("code") or "?"
        c = codes.setdefault(k, {"code": k, "name": r.get("name") or "",
                                 "count": 0, "net": 0.0})
        c["count"] += 1
        c["net"] = round(c["net"] + (r.get("polarity") or 0.0), 4)

    return {
        "success": True, "days": days,
        "start": start.isoformat(), "end": end.isoformat(), "total": len(rows),
        "by_day": [{"date": d, "count": by_day[d]} for d in sorted(by_day)],
        "by_rule": [{"rule": k, "name": rn.get(k, k), "count": v}
                    for k, v in sorted(by_rule.items(), key=lambda x: -x[1])],
        "top_codes": sorted(codes.values(), key=lambda x: (-x["count"], x["code"]))[:10],
    }


@router.get("/api/sentiment/sgpjbg")
async def sentiment_sgpjbg(
    name: str | None = Query(None, description="周报文件名；给了就只返该篇正文"),
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(50, ge=1, le=500),
):
    """机构研究雷达（只读）。

    不带 `name` → 周报清单 + 近 N 天报告元数据（按热度降序）；
    带 `name` → 该篇周报 Markdown 正文（文件名白名单 + 目录内校验，防路径穿越）。
    """
    try:
        if name:
            p = _resolve_weekly(name)
            if p is None:
                return {"success": False, "error": f"无此周报或文件名不合法：{name}"}
            return {"success": True, "name": p.name,
                    "markdown": p.read_text(encoding="utf-8")}

        from brain.sgpjbg_radar import recent_reports
        reports = []
        if _REPORTS_DIR.is_dir():
            for f in _REPORTS_DIR.glob("sgpjbg_研究雷达周报_*.md"):
                m = _WEEKLY_RE.match(f.name)
                if m:
                    reports.append({"name": f.name, "date": m.group(1),
                                    "size": f.stat().st_size})
        reports.sort(key=lambda x: x["date"], reverse=True)
        return {"success": True, "reports": reports,
                "recent": recent_reports(days=days, limit=limit)}
    except Exception as ex:
        logger.warning("sgpjbg 读取异常: %s", ex, exc_info=True)
        return {"success": False, "error": str(ex)}


def _resolve_weekly(name: str) -> Path | None:
    """周报文件名 → 受控路径。白名单正则 + resolve 后必须仍在 reports 目录内。"""
    if not _WEEKLY_RE.match(name):
        return None
    p = (_REPORTS_DIR / name).resolve()
    if p.parent != _REPORTS_DIR.resolve() or not p.is_file():
        return None
    return p
