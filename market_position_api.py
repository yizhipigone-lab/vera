# -*- coding: utf-8 -*-
"""market_position_api.py — /api/market_position/* 路由 (2026-09-17)。

薄层: 只做参数校验 + 转发, 实现全在 core.market_position_runner (深模块)。
照 data_cache_api.py(薄) / kline_cache_maintenance(厚) 的既有分工。

铁律守护: 本文件与 core/market_position*.py 均不得 import trade
(业务铁律 1: 大盘研判只报告, 不联仓位调度), tests/test_market_position.py
有 AST 静态断言。
"""
from fastapi import APIRouter, HTTPException, Query

from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.get("/api/market_position/latest")
async def mp_latest():
    """最新一条大盘位置记录 + 最近 7 条走势 (页面首屏)。"""
    try:
        from core import market_position_runner as mpr
        rec = mpr.latest()
        if rec is None:
            return {"success": True, "record": None,
                    "reason": "还没有连续录像, 先跑 "
                              "python tools/market_position_collect.py --backfill"}
        return {"success": True, "record": rec,
                "recent": mpr.history(limit=7),
                "lines": len(mpr.history(limit=0))}
    except Exception as e:      # 松耦合: 本页挂了不影响其他页签
        logger.warning("大盘位置读取异常: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


@router.get("/api/market_position/history")
async def mp_history(limit: int = Query(250, ge=0, le=5000)):
    """连续录像 (按日期升序), 供页面画趋势图。

    `limit=0` = **全部**（与 `market_position_runner.history(limit=0)` 同一口径,
    箱线图按牛/震荡/熊分组要用全量, 不能只有最近 250 天）。
    **2026-09-17 实测抓到的坑**: 原来这里写 `ge=1`, 而箱线图 `drawBox()` 就传
    `limit=0` → FastAPI 返回 **422** (`{"detail": ...}`), 前端按 JSON 解析得到
    `d.success` 为 undefined → 静默返回 → **箱线图整张空白, 一个错误提示都没有**。
    """
    try:
        from core import market_position_runner as mpr
        return {"success": True, "items": mpr.history(limit=limit)}
    except Exception as e:
        logger.warning("大盘位置历史读取异常: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


@router.get("/api/market_position/mirror")
async def mp_mirror(top_n: int = Query(5, ge=1, le=10)):
    """历史照镜子 (最像的 N 天 + 之后 20/60 日实际走势)。"""
    try:
        from core import market_position_runner as mpr
        return {"success": True, **mpr.mirror(top_n=top_n)}
    except Exception as e:
        logger.warning("大盘位置照镜子异常: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


@router.get("/api/market_position/shadow")
async def mp_shadow():
    """候选择时规则的影子回放 (只记录不交易, T+1 口径)。"""
    try:
        from core import market_position_runner as mpr
        return {"success": True, **mpr.shadow_replay()}
    except Exception as e:
        logger.warning("大盘位置影子回放异常: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


@router.get("/api/market_position/momentum_buckets")
async def mp_momentum_buckets():
    """前期12月涨跌幅 → 未来12月收益 分桶 (三指数并排, 只读本地缓存)。"""
    try:
        from core import market_position_runner as mpr
        return {"success": True, **mpr.momentum_buckets()}
    except Exception as e:
        logger.warning("大盘位置分桶统计异常: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


@router.get("/api/market_position/report")
async def mp_report():
    """完整「大盘体温表」Markdown (页面「查看完整报告」用)。"""
    try:
        from core import market_position_runner as mpr
        rec = mpr.latest()
        if rec is None:
            raise HTTPException(404, "还没有连续录像")
        return {"success": True, "markdown": mpr.thermometer_md(rec)}
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("大盘位置报告生成异常: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


@router.get("/api/market_position/review")
async def mp_review(asof: str | None = Query(None, description="按哪一天做，默认今天")):
    """当日「盘后复盘」Markdown（2026-09-17 M6，页面「今日复盘」用）。

    **薄层只转发**：组装全在 `notes_gen/daily.py`（深模块）。
    本文件的 `import notes_gen` 是**允许的** —— 铁律 1 的守护是
    「大盘位置 ↔ 交易」之间不许互相 import；复盘编排器在**更外层**，
    它单向 import 两边，且**反向有 AST 断言**（`trade/` 不许 import `notes_gen`）。
    本端点**只读**，不写盘、不推送。
    """
    try:
        from notes_gen import daily as drev
        r = drev.build_review(asof=asof)
        return {"success": True, "asof": r["asof"], "markdown": drev.review_md(r)}
    except Exception as e:
        logger.warning("盘后复盘生成异常: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


@router.post("/api/market_position/collect")
def mp_collect(backfill: bool = Query(False)):
    """手动采集一次 (同步端点 → FastAPI 自动丢线程池, 不堵事件循环)。

    backfill=true 走全量历史 (~1 分钟); 日常模式 ~45 秒。与调度器 job
    并发时由 runner 的串行锁挡下, 返回 409 而不是写坏录像。
    """
    from core import market_position_runner as mpr
    try:
        res = mpr.collect(bars=mpr.BACKFILL_BARS if backfill else mpr.DEFAULT_BARS,
                          write=True)
    except Exception as e:
        logger.warning("大盘位置手动采集异常: %s", e, exc_info=True)
        raise HTTPException(500, f"采集异常: {e}") from None
    if not res.get("ok"):
        raise HTTPException(409, res.get("reason") or "采集失败")
    return {"success": True, **{k: v for k, v in res.items() if k != "snapshot"}}


# ════════════════ 大盘环境仪表盘 (2026-09-18 四页签改造) ════════════════
# 打分引擎 core/market_score / 事件引擎 core/market_events / 编排
# core/market_dashboard_runner 均为只读研判, **不 import trade**(业务铁律 1),
# 与既有 market_position 同一套 AST 守护。薄层只做转发, 不重复实现逻辑。


@router.get("/api/market_position/dashboard")
async def mp_dashboard():
    """最新仪表盘完整数据 (四个页签共用: 总览/指标明细/事件跟踪/历史走势)。"""
    try:
        from core import market_dashboard_runner as mdr
        snap = mdr.latest()
        if snap is None:
            return {"success": True, "snapshot": None,
                    "reason": "还没有仪表盘快照，先跑 refresh_close 或点页面「手动刷新」"}
        return {"success": True, "snapshot": snap, "status": mdr.status()}
    except Exception as e:  # 松耦合: 本页挂了不影响其他页签
        logger.warning("大盘仪表盘读取异常: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


@router.get("/api/market_position/dashboard/history")
async def mp_dashboard_history(days: int = Query(30, ge=1, le=500)):
    """历史总分序列 (TAB4 趋势图, days ∈ 7/30/90)。"""
    try:
        from core import market_dashboard_runner as mdr
        return {"success": True, "items": mdr.history(days)}
    except Exception as e:
        logger.warning("大盘仪表盘历史读取异常: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


@router.get("/api/market_position/dashboard/erp_series")
async def mp_dashboard_erp_series():
    """ERP 股债性价比全历史序列 (总览页「22 年长卷」图用; 只读本地文件)。"""
    try:
        from core import market_dashboard_runner as mdr
        return mdr.erp_series()
    except Exception as e:
        logger.warning("ERP 序列读取异常: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


@router.get("/api/market_position/dashboard/status")
async def mp_dashboard_status():
    """更新状态 (上次更新/下次更新时点/各维度数据截止) —— 状态栏与倒计时用。"""
    try:
        from core import market_dashboard_runner as mdr
        return {"success": True, **mdr.status()}
    except Exception as e:
        logger.warning("大盘仪表盘状态读取异常: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


@router.post("/api/market_position/dashboard/refresh")
def mp_dashboard_refresh():
    """手动全量刷新 (状态栏「手动刷新」按钮用; 同步端点 → FastAPI 自动丢线程池)。"""
    from core import market_dashboard_runner as mdr
    try:
        res = mdr.refresh_close(write=True)
    except Exception as e:
        logger.warning("大盘仪表盘手动刷新异常: %s", e, exc_info=True)
        raise HTTPException(500, f"刷新异常: {e}") from None
    if not res.get("ok"):
        raise HTTPException(409, res.get("reason") or "刷新失败")
    snap = res["snapshot"]
    return {"success": True, "date": snap["date"],
            "final_total": snap["scores"]["final_total"],
            "label": snap["scores"]["label"]}


@router.post("/api/market_position/dashboard/event")
async def mp_dashboard_event_add(level: str = Query(..., description="epic/major/minor"),
                                 title: str = Query(..., description="事件简述"),
                                 score: float = Query(..., description="初始修正分(±), 会被 clamp 到该等级上限"),
                                 logic: str = Query("", description="打分逻辑(大白话)")):
    """人工录入一个重大事件 (TAB3 事件跟踪用; 本期事件靠人工录入, 自动扫描二期再做)。"""
    import datetime as _dt
    from core import market_events as me
    try:
        e = me.add_event(level=level, title=title, initial_score=score,
                         start_date=_dt.date.today(), logic=logic)
    except ValueError as ex:
        raise HTTPException(400, str(ex)) from None
    except Exception as e:
        logger.warning("事件录入异常: %s", e, exc_info=True)
        raise HTTPException(500, f"事件录入异常: {e}") from None
    return {"success": True, "event": e}
