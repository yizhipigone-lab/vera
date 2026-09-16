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
async def mp_history(limit: int = Query(250, ge=1, le=5000)):
    """连续录像 (按日期升序), 供页面画趋势图。"""
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
