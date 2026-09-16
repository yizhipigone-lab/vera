# -*- coding: utf-8 -*-
"""数据准备 TAB 端点 /api/data_cache/* (2026-08-14)。

K 线缓存管理台：状态总览 / 手动补拉 / 补拉日志。
松耦合：core.kline_cache_maintenance 挂了只影响本 TAB，其余页面零感知。
实现全在 maintenance 模块（深模块），本文件只做参数校验 + 转发。
"""
import re

from fastapi import APIRouter

from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter()

_DATE_RE = re.compile(r"^\d{8}$")
_VALID_PERIODS = ("1d", "5m", "1m")
_VALID_UNIVERSES = ("5", "50", "23", "24", "25", "28", "51", "52", "53")


@router.get("/api/data_cache/status")
async def data_cache_status():
    """缓存状态总览: 各 period 股票数/数据起止/是否过期 + 是否补拉中。"""
    try:
        from core.kline_cache_maintenance import cache_status
        return {"success": True, **cache_status()}
    except Exception as e:  # 松耦合兜底
        logger.warning(f"缓存状态查询异常: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.post("/api/data_cache/backfill")
async def data_cache_backfill(body: dict):
    """手动补拉: {period, start, end?, universe?, limit?} → 后台子进程。

    已在运行返 success=False + reason（不重复起）。
    """
    period = (body.get("period") or "").strip()
    start = (body.get("start") or "").strip()
    end = (body.get("end") or "").strip()
    universe = (body.get("universe") or "").strip() or None
    try:
        # 2026-09-16 审计 P2 修复: 原在 try 外, 非法值 (如 "abc") 的 ValueError
        # 直接穿透 → 500; 移入 try 走既有错误返回
        limit = int(body.get("limit") or 0)
    except (TypeError, ValueError):
        return {"success": False, "error": "limit 需为整数 (0~10000)"}
    if period not in _VALID_PERIODS:
        return {"success": False, "error": f"period 限 {_VALID_PERIODS}"}
    if not _DATE_RE.match(start):
        return {"success": False, "error": "start 需 YYYYMMDD"}
    if end and not _DATE_RE.match(end):
        return {"success": False, "error": "end 需 YYYYMMDD"}
    if universe and universe not in _VALID_UNIVERSES:
        return {"success": False, "error": f"universe 限 {_VALID_UNIVERSES}"}
    if not 0 <= limit <= 10000:
        return {"success": False, "error": "limit 限 0~10000"}
    try:
        from core.kline_cache_maintenance import start_backfill
        r = start_backfill(segments=[(period, start)], trigger="manual_tab",
                           universe=universe, end=end, limit=limit)
        return {"success": r["started"], **r}
    except Exception as e:
        logger.warning(f"手动补拉启动异常: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.get("/api/data_cache/log")
async def data_cache_log(tail: int = 80):
    """补拉日志尾部（进度显示）。tail 限 1~500。"""
    try:
        from core.kline_cache_maintenance import read_log_tail
        tail = max(1, min(500, int(tail)))
        return {"success": True, "lines": read_log_tail(tail)}
    except Exception as e:
        logger.warning(f"补拉日志读取异常: {e}", exc_info=True)
        return {"success": False, "error": str(e), "lines": []}
