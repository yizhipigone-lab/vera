# -*- coding: utf-8 -*-
"""公式体检端点 /api/lab/* (2026-08-01 批次5 C4c 从 server.py 抽出, 纯移动不改行为)。

严格串行; 与 /api/run 不对称互斥: 体检永远排队, 回测提交在体检运行中 → 409
(409 guard 在 server.py /api/run, 不在本模块)。

路由不直接持有全局状态: create_lab_router(lab_status, pipeline_status) 工厂注入,
server.py 建队列后 include_router, 保持测试对 server.lab_status 的 monkeypatch 语义。
"""
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.lab_runner import formula_name_ok

_ROOT = Path(__file__).resolve().parent


class LabRunRequest(BaseModel):
    formulas: list
    tag: Optional[str] = None
    tag2: Optional[str] = None


def _default_tags() -> tuple:
    """缺省窗口: 近1年 / 近3年(按当日滚动), tag 格式 YYYYMMDD_YYYYMMDD。"""
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    end = _dt.now()
    def _t(d): return d.strftime("%Y%m%d")
    return (_t(end - _td(days=365)) + "_" + _t(end),
            _t(end - _td(days=365 * 3)) + "_" + _t(end))


def create_lab_router(lab_status, pipeline_status) -> APIRouter:
    """构建 /api/lab/* 路由。lab_status: LabQueue 单例; pipeline_status: 管线状态单例。"""
    router = APIRouter()

    @router.post("/api/lab/run")
    async def lab_run(req: LabRunRequest):
        """提交体检任务(永远 FIFO 排队; 回测在跑时显示排队原因)。"""
        formulas = [str(f).strip() for f in req.formulas if str(f).strip()]
        if len(formulas) > 10:
            return JSONResponse(status_code=400, content={"success": False, "error": "单次最多 10 个公式"})
        tag, tag2_default = _default_tags()
        tag = req.tag or tag
        tag2 = req.tag2 if req.tag2 is not None else tag2_default
        tag2 = tag2 or None   # 2026-07-20 冒烟修复: 空串 = 显式单窗口(报告标"待复核"); 缺省 = 近3年
        for t in [tag, tag2]:
            if t and not re.match(r"^\d{8}_\d{8}$", t):
                return JSONResponse(status_code=400, content={"success": False, "error": f"窗口格式应为 YYYYMMDD_YYYYMMDD: {t}"})
        ids, err = lab_status.submit(formulas, tag, tag2)
        if err:
            return JSONResponse(status_code=400, content={"success": False, "error": err})
        return {"success": True, "task_ids": ids, "tag": tag, "tag2": tag2,
                "queued_behind_pipeline": bool(pipeline_status.running)}

    @router.get("/api/lab/status")
    async def lab_status_api():
        return lab_status.snapshot()

    @router.post("/api/lab/stop")
    async def lab_stop():
        """停止当前运行中的体检任务 (2026-07-25): 杀子进程 + 标 cancelled。"""
        ok, msg = lab_status.stop_current()
        return {"success": ok, "message": msg}

    @router.get("/api/lab/history")
    async def lab_history():
        """历史体检: 按公式聚合 规则JSON + 体检报告 md。
        聚合计算已下沉 core.lab_runner.history_items (2026-09-15 深模块治理)。"""
        from core.lab_runner import history_items
        return {"success": True, "items": history_items()}

    @router.get("/api/lab/report")
    async def lab_report(formula: str):
        """返回该公式最近一次体检报告 markdown。"""
        if not formula_name_ok(formula or ""):
            return JSONResponse(status_code=400, content={"success": False, "error": "公式名含非法字符"})
        audit_dir = _ROOT / "docs" / "audit"
        cands = sorted(audit_dir.glob(f"*_{formula}_*因子体检报告.md"))
        if not cands:
            return {"success": False, "error": f"{formula} 无体检报告"}
        p = cands[-1]
        try:
            return {"success": True, "file": p.name, "markdown": p.read_text(encoding="utf-8")}
        except Exception as e:
            return {"success": False, "error": f"报告读取失败: {e}"}

    return router
