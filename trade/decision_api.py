"""trade/decision_api.py — 当日决策台账读出接口 (2026-09-18)。

为什么单独一个文件
------------------
回答的是「今天为什么动 / 为什么没动」这一个问题, 与下单/持仓/配置三个域都不
共享状态, 只读 ``trade_app`` 的快照与库 —— 按项目里「一域一文件」的范式
(``trade/analysis_api.py`` 是先例) 单独成文件, 改这两个只读端点绝不会碰到
下单代码。

两个端点都是**纯读**:

- ``GET /api/trade/decisions?date=YYYYMMDD``            —— 某一天的决策明细;
- ``GET /api/trade/decisions/calendar?month=YYYYMM``    —— 某个月的决策日历。

读法用 ``trade/store.py`` 的只读连接 (WAL 下与写连接不互堵), 台账表里没有东西
时**读取侧合成**一行 ``NO_RUN`` 回答"为什么什么都没有" —— 写入侧永远不会去写
这一行, 因为进程都没开机, 谁来写?
"""

from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, HTTPException, Query

from utils.trading_calendar import is_trading_day as _cal_is_trading_day
from trade.decision_codes import get as _code_get
from utils.logger import get_logger

logger = get_logger(__name__)

# 台账起点 = audit / trades 表最早的一天, 再往前没有任何数据
# ("全量回填"能回的就是这么多, 见实施计划书 §3.6)
_LEDGER_START = "2026-07-27"

# 策略分组 (顺序即页面上的显示顺序)。空组也要返回 —— 前端靠它显示
# 「这条策略今天没有留痕」的灰色占位, 而不是让整组凭空消失。
GROUP_ORDER = ("rotation", "auto_buy", "exit", "ladder")
STRATEGY_LABELS = {
    "rotation": "ETF 轮动",
    "auto_buy": "尾盘选股买入",
    "exit": "止盈止损",
    "ladder": "预埋单",
    "system": "系统",
}


def decision_router(trade_app) -> APIRouter:
    """装配决策台账 2 端点。``trade_app`` 为组合根, 路由只读它的 store/config。"""
    router = APIRouter()

    def _ro():
        return trade_app.store.open_readonly()

    def _day_bounds(day: str) -> tuple[float, float]:
        """``YYYY-MM-DD`` → 当日 ``[00:00, 次日 00:00)`` 的 epoch 秒。"""
        start = _dt.datetime.strptime(day, "%Y-%m-%d").timestamp()
        return start, start + 86400.0

    def _run_trace(day: str) -> dict:
        """三条腿今天跑没跑 (只看审计表, 不看台账行 —— 台账行可能是回填来的)。

        这是卡片一状态条的数据源: 一眼分辨"今天系统真的跑过了"还是"压根没开机"。
        """
        cfg = trade_app.config
        start, end = _day_bounds(day)
        try:
            ro = _ro()
            try:
                rows = ro.execute(
                    "SELECT kind, COUNT(*) FROM audit WHERE ts >= ? AND ts < ? "
                    "GROUP BY kind", (start, end)).fetchall()
            finally:
                ro.close()
        except Exception:
            logger.warning("读当日运行痕迹失败", exc_info=True)
            return {}
        kinds = {r[0]: r[1] for r in rows}
        out: dict = {}
        if kinds.get("rotation_start") or kinds.get("rotation_summary"):
            out["rotation"] = f"{cfg.rotation.execute_time} 跑过"
        if (kinds.get("auto_buy_start") or kinds.get("auto_buy_summary")
                or kinds.get("auto_buy_skip_regime")):
            out["auto_buy"] = f"{cfg.auto_buy.time} 跑过"
        if kinds.get("eod"):
            out["eod"] = "15:05 已归档"
        return out

    def _manual_trades_today(day: str) -> int:
        """当日人工成交笔数 (让人不会误以为"系统什么都不干")。"""
        start, end = _day_bounds(day)
        try:
            ro = _ro()
            try:
                row = ro.execute(
                    "SELECT COUNT(*) FROM trades WHERE ts >= ? AND ts < ? "
                    "AND source = 'manual'", (start, end)).fetchone()
            finally:
                ro.close()
        except Exception:
            logger.warning("读当日人工成交笔数失败", exc_info=True)
            return 0
        return int(row[0] or 0) if row else 0

    def _synthesize_no_run(day: str, trading_day: bool) -> dict | None:
        """整天一行决策都没有时, 合成一行把"为什么没有"说清楚。

        **只合成、不落库** —— 进程都没开机, 写入侧没法写这一行; 而且这四种情况
        的性质完全不同, 混成一句"该日无成交"会把真故障藏起来 (2026-09-18 用户需求)。
        """
        if not trading_day:
            # 休市: 由日历格标「休市」回答, 不出"没运行"的卡
            return None
        try:
            ro = _ro()
            try:
                row = ro.execute(
                    "SELECT source FROM daily_asset WHERE date = ?",
                    (day,)).fetchone()
            finally:
                ro.close()
        except Exception:
            row = None
        source = (row[0] or "eod") if row else ""
        if not row:
            text = "这一天没有任何运行痕迹（程序没开机，也没留下别的记录）"
            head = "没有任何运行痕迹"
        elif source == "derived":
            text = ("这一天程序没开机（资产是用收盘价推算补上的），"
                    "所以没有任何决策记录")
            head = "程序没开机"
        else:
            text = ("这一天程序跑了，但没有留下决策记录"
                    "（这个功能是 2026 年 9 月 18 日才上线的）")
            head = "跑了但没留决策记录"
        return {
            "strategy": "system", "subject": "all", "action": "HOLD",
            "reason_code": "NO_RUN", "reason_label": _code_get("NO_RUN")["label"],
            "tone": _code_get("NO_RUN")["tone"],
            "reason_text": text,
            "evidence": {"daily_asset_source": source} if row else {},
            "trade_ids": "", "source": "inferred", "headline": head,
        }

    def _row_out(r: dict) -> dict:
        """台账行 → 接口行 (补上人话短标签与色调; 明细坏了给空字典)。"""
        meta = _code_get(r["reason_code"])
        return {
            "strategy": r["strategy"],
            "subject": r["subject"],
            "action": r["action"],
            "reason_code": r["reason_code"],
            "reason_label": meta["label"],
            "tone": meta["tone"],
            "reason_text": r["reason_text"],
            "evidence": r["evidence"] if isinstance(r["evidence"], dict) else {},
            "trade_ids": r["trade_ids"],
            "source": r["source"],
        }

    @router.get("/api/trade/decisions")
    def decisions(date: str = Query(default="")):
        """某一天的决策明细 (缺省今天)。``date`` 只收 8 位数字, 与页面其它
        日期查询框同口径 (web/js/trade.js 的 _dateParam)。"""
        day = _parse_day(date)
        trading_day = _cal_is_trading_day(_dt.date.fromisoformat(day))
        manual = _manual_trades_today(day)
        base = {"date": day, "trading_day": trading_day,
                "run_any": False, "run_trace": {}, "groups": [],
                "manual_trades_today": manual,
                "summary": _zero_summary()}
        if day < _LEDGER_START:
            base["note"] = (f"这一天在台账起点（{_LEDGER_START}）之前，"
                            f"当时没有任何记录")
            return base
        try:
            ro = _ro()
            try:
                rows = trade_app.store.decision.load_day(day, conn=ro)
            finally:
                ro.close()
        except Exception:
            logger.warning("读决策台账失败", exc_info=True)
            base["note"] = "查询失败：台账读取异常（不影响交易）"
            return base

        base["run_trace"] = _run_trace(day)
        if not rows:
            synth = _synthesize_no_run(day, trading_day)
            if synth is not None:
                base["groups"] = [{
                    "strategy": "system",
                    "label": STRATEGY_LABELS["system"],
                    "rows": [synth]}]
                base["summary"] = _summarize([synth])
            return base

        base["run_any"] = True
        by_group: dict[str, list] = {}
        for r in rows:
            by_group.setdefault(r["strategy"], []).append(_row_out(r))
        groups = []
        for name in GROUP_ORDER:
            groups.append({
                "strategy": name, "label": STRATEGY_LABELS.get(name, name),
                "rows": by_group.pop(name, [])})
        for name, items in by_group.items():      # 兜底: 将来新增策略也不会漏
            groups.append({"strategy": name,
                           "label": STRATEGY_LABELS.get(name, name),
                           "rows": items})
        base["groups"] = groups
        base["summary"] = _summarize([r for g in groups for r in g["rows"]])
        return base

    @router.get("/api/trade/decisions/calendar")
    def decisions_calendar(month: str = Query(default="")):
        """某个月的决策日历 (缺省当月)。每格给: 行数、色调分布、一句话摘要。

        交易日一行都没有 → 该格标「没运行」(与卡片一同一个判据)。
        但**读库失败**时不标 —— 那会把一次瞬时抖动说成"整月没开机";
        这种情况下返回空格子 + 一句 ``note``。
        """
        first, last = _parse_month(month)
        month_key = first[:7]
        start_d = _dt.date.fromisoformat(first)
        last_d = _dt.date.fromisoformat(last)
        # 读失败时必须**区分**「真的一天都没跑」与「这一下没读到」:
        # 不区分的话, 一次瞬时数据库抖动会让整月每个交易日都被标成「没运行」——
        # 页面等于在指控程序整月没开机 (2026-09-18 补测试时发现)。
        # 卡片一的端点遇到同样情况是给一句 note 然后不再合成, 两边保持一致。
        read_failed = False
        try:
            ro = _ro()
            try:
                rows = trade_app.store.decision.load_range(first, last, conn=ro)
            finally:
                ro.close()
        except Exception:
            logger.warning("读决策日历失败", exc_info=True)
            rows = []
            read_failed = True
        by_day: dict[str, list] = {}
        for r in rows:
            by_day.setdefault(r["trade_date"], []).append(r)

        days = []
        d = start_d
        while d <= last_d:
            key = d.isoformat()
            items = by_day.get(key, [])
            trading = _cal_is_trading_day(d)
            if items:
                tones: dict[str, int] = {}
                for r in items:
                    tone = _code_get(r["reason_code"])["tone"]
                    tones[tone] = tones.get(tone, 0) + 1
                days.append({
                    "date": key, "trading_day": trading, "rows": len(items),
                    "tones": tones, "headline": _headline(items),
                    "inferred": all(r["source"] == "inferred" for r in items)})
            elif not read_failed and trading and key >= _LEDGER_START:
                # 交易日但一行都没有 → 标「没运行」(与卡片一同一个判据)
                synth = _synthesize_no_run(key, True)
                days.append({
                    "date": key, "trading_day": True, "rows": 1,
                    "tones": {"muted": 1},
                    "headline": (synth or {}).get("headline", "没运行"),
                    "inferred": True})
            else:
                # 包含"读失败"这一种: 给空格子 (rows=0 → 前端显示空白),
                # 而不是凭空说它没运行。note 会说明是查询失败。
                days.append({"date": key, "trading_day": trading, "rows": 0,
                             "tones": {}, "headline": "", "inferred": False})
            d += _dt.timedelta(days=1)
        out = {"month": month_key, "days": days}
        if read_failed:
            out["note"] = "查询失败：台账读取异常（不影响交易）"
        return out

    return router


def _zero_summary() -> dict:
    return {"buy": 0, "sell": 0, "hold": 0, "fail": 0, "info": 0, "inferred": 0}


def _summarize(rows: list[dict]) -> dict:
    """按动作与可信度计数 (卡片一顶部那一行小字)。"""
    out = _zero_summary()
    for r in rows:
        key = str(r.get("action", "")).lower()
        if key in ("buy", "sell", "hold", "fail", "info"):
            out[key] += 1
        if r.get("source") == "inferred":
            out["inferred"] += 1
    return out


# 日历格摘要的优先级 (数字大者胜)。为什么不直接用 ACTION_RANK:
#   ① 那里 INFO(1) 压得住 HOLD(0) —— 会让「预埋单：阶梯止盈开关关着」这种
#      **每天都有、最不重要**的告知盖掉更值得注意的事实;
#   ② 「没有记录」(NO_RUN) 的动作是 HOLD, 但它是当天最该被看见的事之一,
#      必须单列一档, 排在真实失败之后、日常告知之前。
# 典型场景 (2026-09-11): 程序 09:15 挂预埋单时发现开关关着, 之后没到收盘就退了 ——
# 当天唯一一条真记录是"开关关着", 而"三条主策略都没有记录"才是用户该看到的。
_HEADLINE_RANK = {
    "SELL": 6, "BUY": 5, "FAIL": 4,
    "NO_RUN": 3,          # 按 reason_code 判, 不是按 action
    "INFO": 2, "HOLD": 1,
}


def _headline(rows: list[dict]) -> str:
    """日历格上那一行摘要: 取当天"最值得一眼看到"的那条。

    只在存在推断的 ``NO_RUN`` 行时才与"按动作强弱取"不同 —— 那种行只由回填写入,
    或由读取侧在整天空白时合成, 所以实时日期不受影响。
    """
    if not rows:
        return ""

    def key(r: dict) -> int:
        if r.get("reason_code") == "NO_RUN":
            return _HEADLINE_RANK["NO_RUN"]
        return _HEADLINE_RANK.get(r.get("action"), 0)

    top = max(rows, key=key)
    label = STRATEGY_LABELS.get(top["strategy"], top["strategy"])
    return f"{label}：{_code_get(top['reason_code'])['label']}"


def _parse_day(date: str) -> str:
    """``YYYYMMDD`` → ``YYYY-MM-DD``; 空 → 今天; 非法 → 422。"""
    s = (date or "").strip()
    if not s:
        return _dt.date.today().isoformat()
    if len(s) == 8 and s.isdigit():
        try:
            return _dt.datetime.strptime(s, "%Y%m%d").date().isoformat()
        except ValueError:
            pass
    raise HTTPException(422, "日期格式应为 YYYYMMDD（8 位数字，例如 20260918）")


def _parse_month(month: str) -> tuple[str, str]:
    """``YYYYMM`` → (当月第一天, 当月最后一天); 空 → 当月; 非法 → 422。"""
    s = (month or "").strip()
    if not s:
        d = _dt.date.today()
        y, m = d.year, d.month
    elif len(s) == 6 and s.isdigit():
        y, m = int(s[:4]), int(s[4:])
        if not 1 <= m <= 12:
            raise HTTPException(422, "月份格式应为 YYYYMM（例如 202609）")
    else:
        raise HTTPException(422, "月份格式应为 YYYYMM（例如 202609）")
    import calendar as _cal
    last_day = _cal.monthrange(y, m)[1]
    return (f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last_day:02d}")
