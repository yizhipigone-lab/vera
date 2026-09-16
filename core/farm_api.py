# -*- coding: utf-8 -*-
"""farm_api — /api/farm/* 路由(工厂注入 FarmRunner + pipeline_status)。

- 闸门提交在任一闸门运行中 → 409; 回测闸门另检查 pipeline 回测互斥。
- reports 只读 data/formula_farm/reports/*.md, 文件名白名单(防路径穿越)。
"""
import re
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "data" / "formula_farm" / "reports"
SAFE_FILE = re.compile(r"^[\w\-\.\u4e00-\u9fff]+\.md$")


def create_farm_router(farm, pipeline_status):
    router = APIRouter()

    def _start(gate):
        if gate == "backtest" and getattr(pipeline_status, "running", False):
            raise HTTPException(409, "回测流水线正在运行, 回测闸门请先等它结束")
        run, err = farm.start(gate)
        if run is None:
            raise HTTPException(409, err)
        return {"ok": True, "gate": gate}

    @router.get("/api/farm/status")
    def api_farm_status():
        return farm.status()

    @router.post("/api/farm/check")
    def api_farm_check():
        return _start("check")

    @router.post("/api/farm/onboard")
    def api_farm_onboard():
        return _start("onboard")

    @router.post("/api/farm/verify")
    def api_farm_verify():
        """第四闸门: 定量复核 (重画检测 + 未来函数甄别)。"""
        return _start("verify")

    @router.post("/api/farm/backtest")
    def api_farm_backtest():
        return _start("backtest")

    @router.post("/api/farm/stop")
    def api_farm_stop():
        farm.stop()
        return {"ok": True}

    @router.get("/api/farm/reports")
    def api_farm_reports():
        items = []
        if REPORTS.is_dir():
            for p in sorted(REPORTS.glob("*.md"), key=lambda x: -x.stat().st_mtime):
                items.append({"file": p.name,
                              "mtime": time.strftime("%Y-%m-%d %H:%M",
                                                     time.localtime(p.stat().st_mtime)),
                              "size": p.stat().st_size})
        return {"items": items}

    @router.get("/api/farm/report")
    def api_farm_report(file: str = Query(...)):
        if not SAFE_FILE.match(file):
            raise HTTPException(400, "非法文件名")
        p = REPORTS / file
        if not p.exists():
            raise HTTPException(404, "报告不存在")
        return {"file": file, "markdown": p.read_text(encoding="utf-8")}

    return router
