# -*- coding: utf-8 -*-
"""core/market_events.py — 大盘仪表盘·重大财经事件引擎 (2026-09-18)。

职责：管理"影响市场情绪的重大事件"清单 —— 分级、线性衰减、过期移除、合计修正分(±1)。
输出"事件修正分"供打分引擎加到基础总分上(最终总分 = 基础总分 + 事件修正分)。

设计定位(与需求/计划书一致):
- **不 import trade**(业务铁律 1, 由 tests/test_market_position.py 的 AST 断言守护)。
- 本期**只支持人工录入**(用户在页面/命令行加事件); LLM 自动扫描新闻**留接口不做**
  (计划书 §6.2 —— LLM 定级每次输出可能不同、不稳定, 二期再做且必须落库可复查)。
- 衰减按**自然日**(周末也在流逝, 事件情绪会被时间消化), 注释写明, 不与交易日混淆。
- 落库 `data/market_position/events.jsonl`, 原子写(临时文件 + os.replace, 照
  `core/market_position_runner.py:816` `_upsert` 的写法, 防崩溃写坏半个文件)。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading

from utils.sysutil import project_root

_ROOT = project_root()
EVENTS_PATH = _ROOT / "data" / "market_position" / "events.jsonl"

_LOCK = threading.Lock()

#: 事件分级(需求 §四.1, 硬编码)。max_score = 单事件修正分上限。
EVENT_LEVELS = {
    "epic":  {"name": "史诗级",  "expire_days": 30, "max_score": 1.0},
    "major": {"name": "普通重大", "expire_days": 10, "max_score": 0.5},
    "minor": {"name": "短期情绪", "expire_days": 3,  "max_score": 0.2},
}

#: 合计修正分上下限(需求 §四: 总修正上限 ±1 分)
TOTAL_ADJ_LIMIT = 1.0

#: 分级关键词(初始, 供人工录入时参考; 不作为自动定级依据 —— 自动定级二期用 LLM 且须落库)。
LEVEL_KEYWORDS = {
    "epic": ["印花税", "IPO暂停", "IPO 暂停", "平准基金", "924", "万亿级", "5万亿", "五万亿"],
    "major": ["降准", "降息", "加息", "顶层政策", "大幅超预期"],
    "minor": ["表态", "常规", "小幅调整"],
}


def _parse_date(s) -> dt.date:
    if isinstance(s, dt.date):
        return s
    return dt.datetime.strptime(str(s), "%Y-%m-%d").date()


def suggest_level(title: str) -> dict:
    """按关键词给"建议等级"(仅供参考, 人工录入时辅助; 不做自动定级的最终依据)。"""
    for level in ("epic", "major", "minor"):
        for kw in LEVEL_KEYWORDS[level]:
            if kw in title:
                return {"level": level, "by_keyword": kw,
                        "name": EVENT_LEVELS[level]["name"],
                        "expire_days": EVENT_LEVELS[level]["expire_days"]}
    return {"level": "minor", "by_keyword": None, "name": "短期情绪",
            "expire_days": EVENT_LEVELS["minor"]["expire_days"]}


def _load_events(path=None) -> list[dict]:
    p = path or EVENTS_PATH
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue  # 坏行跳过, 不拖垮整个事件引擎
    return out


def _save_events(events: list[dict], path=None) -> None:
    p = path or EVENTS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
                   encoding="utf-8")
    os.replace(tmp, p)  # 原子替换


def _days_left(event: dict, today: dt.date) -> int:
    """剩余有效天数(自然日)。首日=expire_days(满额), 每日减 1, 到 0 过期。

    常驻跟踪事件(tracker, 如美联储利率预期)不参与自然衰减/过期 —— 由每日刷新维持活性,
    返回极大值, 永远在窗内。
    """
    if event.get("tracker"):
        return 999
    start = _parse_date(event["start_date"])
    expire_days = int(event["expire_days"])
    elapsed = (today - start).days
    # clamp 到满额: 周末/节假日刷新用"最近交易日"做衰减基准(refresh_close 的设计),
    # 晚于该基准日录入的事件 elapsed 为负 —— 不封顶会让剩余天数超过满额、
    # 分数被放大到初始分之上(首日满额是口径上限)。
    return min(expire_days, expire_days - elapsed)


def _decayed_score(event: dict, today: dt.date) -> float:
    """线性衰减: 当日分 = 初始分 × (剩余天数 / 总有效期)。首日满额, 到期为 0。

    常驻跟踪事件: 分数直接取 current_score(由每日刷新按最新读数重算), 不衰减。
    """
    if event.get("tracker"):
        return round(float(event.get("current_score", event.get("initial_score", 0.0))), 4)
    left = _days_left(event, today)
    if left <= 0:
        return 0.0
    expire_days = int(event["expire_days"])
    return round(float(event["initial_score"]) * (left / expire_days), 4)


def add_event(level: str, title: str, initial_score: float, start_date,
              logic: str = "", source: str = "manual", path=None) -> dict:
    """人工录入一个事件。initial_score 正负号表方向(正=利好, 负=利空),
    绝对值会被 clamp 到该等级上限(需求: 单事件最高±上限)。"""
    if level not in EVENT_LEVELS:
        raise ValueError(f"未知事件等级: {level} (可选 {list(EVENT_LEVELS)})")
    lv = EVENT_LEVELS[level]
    cap = lv["max_score"]
    initial_score = max(-cap, min(cap, float(initial_score)))
    start = _parse_date(start_date)
    with _LOCK:
        events = _load_events(path)
        seq = sum(1 for e in events if e.get("start_date") == start.isoformat()) + 1
        event = {
            "id": f"evt_{start.strftime('%Y%m%d')}_{seq:02d}",
            "level": level,
            "level_name": lv["name"],
            "title": title,
            "initial_score": initial_score,
            "start_date": start.isoformat(),
            "expire_days": lv["expire_days"],
            "logic": logic,
            "source": source,
        }
        events.append(event)
        _save_events(events, path)
    return event


def daily_tick(today=None, path=None) -> dict:
    """每日执行(需求 §四.3): 存量事件按日衰减, 过期移除, 汇总生效事件修正分并 clamp 到 ±1。

    返回 {date, count, total_adj, active(生效清单, 按影响力从大到小), removed(本次移除的)}。
    """
    today = _parse_date(today) if today else dt.date.today()
    with _LOCK:
        events = _load_events(path)
        active, removed = [], []
        for e in events:
            left = _days_left(e, today)
            if left <= 0:
                removed.append(e)
                continue
            score_now = _decayed_score(e, today)
            active.append({**e, "days_left": left, "score_now": score_now})
        if removed:  # 有移除才重写文件(避免无谓写盘)
            _save_events(active and [{k: v for k, v in a.items()
                                      if k not in ("days_left", "score_now")} for a in active]
                         or [], path)
        total = sum(a["score_now"] for a in active)
        total_adj = max(-TOTAL_ADJ_LIMIT, min(TOTAL_ADJ_LIMIT, round(total, 4)))
        active.sort(key=lambda a: abs(a["score_now"]), reverse=True)  # 按影响力从大到小
    return {"date": today.isoformat(), "count": len(active),
            "total_adj": total_adj, "active": active,
            "removed_ids": [e["id"] for e in removed]}


def list_active(today=None, path=None) -> list[dict]:
    """当前生效事件(供 TAB3 渲染)。等价于 daily_tick 的 active, 但不改写文件。"""
    today = _parse_date(today) if today else dt.date.today()
    out = []
    for e in _load_events(path):
        left = _days_left(e, today)
        if left > 0:
            out.append({**e, "days_left": left, "score_now": _decayed_score(e, today)})
    out.sort(key=lambda a: abs(a["score_now"]), reverse=True)
    return out


def remove_event(event_id: str, path=None) -> dict:
    """按 id 删除一条事件(人工纠错用)。原子写。返回 {removed, found}。

    设计: 与管理员纠错一致 —— 删除也要可审计、可复现(CLI 调它, 不直接手改 jsonl)。
    """
    with _LOCK:
        events = _load_events(path)
        found = [e for e in events if e.get("id") == event_id]
        if not found:
            return {"removed": None, "found": False}
        kept = [e for e in events if e.get("id") != event_id]
        _save_events(kept, path)
        return {"removed": found[0], "found": True}


def upsert_tracker(tracker: str, level: str, title: str, score: float,
                   logic: str, extra: dict | None = None, today=None, path=None) -> dict:
    """维护一条"常驻跟踪事件"(如美联储利率预期): 不按自然日衰减、不过期,
    分数由每日刷新按最新读数重算(current_score), 只在其读数实质性变化时改动。

    - 首次: 创建该 tracker 的专属事件(id = f"evt_{tracker}")。
    - 之后每次刷新: 更新 start_date(维持活性) + last_checked + last_prob + logic(最新读数);
      仅当 |新分 - 旧分| ≥ 0.02 才改 current_score(=分数真正变动), 并同时更新 title
      (如会议月份)。返回 {event, changed: bool}。

    设计动机: 利率预期是"持续刷新的市场隐含概率", 不是离散发生的事; 用离散事件的
    3/10天衰减模型会失真(稳定读数下也会归零)。故单列常驻类型。
    """
    if level not in EVENT_LEVELS:
        raise ValueError(f"未知事件等级: {level}")
    today = _parse_date(today) if today else dt.date.today()
    extra = extra or {}
    with _LOCK:
        events = _load_events(path)
        ev = next((e for e in events if e.get("tracker") == tracker), None)
        if ev is None:
            lv = EVENT_LEVELS[level]
            ev = {
                "id": f"evt_{tracker}",
                "level": level,
                "level_name": lv["name"],
                "title": title,
                "initial_score": score,
                "current_score": score,
                "start_date": today.isoformat(),
                "expire_days": 30,
                "logic": logic,
                "source": "agent_scan",
                "tracker": tracker,
                "last_prob": extra,
                "last_checked": today.isoformat(),
            }
            events.append(ev)
            changed = True
        else:
            old_score = float(ev.get("current_score", ev.get("initial_score", 0.0)))
            ev["start_date"] = today.isoformat()      # 维持活性, 防止被 daily_tick 误清
            ev["last_checked"] = today.isoformat()
            ev["last_prob"] = extra
            ev["logic"] = logic
            if abs(score - old_score) >= 0.02:        # 实质性变化才动分数
                ev["current_score"] = score
                ev["title"] = title
                changed = True
            else:
                changed = False
        _save_events(events, path)
        return {"event": ev, "changed": changed}


def scan_news(*_args, **_kwargs) -> list:
    """LLM 自动扫描新闻定级 —— **本期占位不做**(计划书 §6.2/§13)。

    理由: LLM 对"事件等级"的判定每次输出可能不同, 不稳定; 而事件修正分直接加进总分,
    不稳定的输入会让总分不可复现。二期再做, 且做成"LLM 给候选 + 落库可复查 + 人工确认"。
    本期事件一律走 add_event 人工录入(可带 source="llm_scan" 标记来源)。
    """
    return []
