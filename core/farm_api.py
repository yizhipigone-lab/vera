# -*- coding: utf-8 -*-
"""farm_api — /api/farm/* 路由(工厂注入 FarmRunner + pipeline_status)。

- 闸门提交在任一闸门运行中 → 409; 回测闸门另检查 pipeline 回测互斥。
- reports 只读 data/formula_farm/reports/*.md, 文件名白名单(防路径穿越)。
"""
import os
import re
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "data" / "formula_farm" / "reports"
RUNS = ROOT / "data" / "formula_farm" / "runs"
FARM_DATA = ROOT / "data" / "formula_farm"
SAFE_FILE = re.compile(r"^[\w\-\.\u4e00-\u9fff]+\.md$")
GATE_NAMES = ("check", "onboard", "verify", "backtest")
SAFE_GS = re.compile(r"^GS\d{4}$")   # 回填接口的 gs 白名单 (防任意键探测)
#: 单次返回日志上限 (字符) —— 入库千条全量日志可能上 MB, 超出截尾并标注
LOG_MAX_CHARS = 500_000


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

    @router.get("/api/farm/prefill")
    def api_farm_prefill(gs: str = Query(...)):
        """达标榜「→ 回测页」回填包 (2026-09-16 计划书)。

        逻辑全在 core.farm_summary.backtest_prefill, 本路由只做白名单+错误映射。
        """
        if not SAFE_GS.match(gs):
            raise HTTPException(400, "非法 GS 编号")
        from core import farm_summary
        try:
            return farm_summary.backtest_prefill(str(FARM_DATA), gs)
        except KeyError:
            raise HTTPException(404, "该 GS 不在档案 (未入库或无粗扫结果)") from None
        except ValueError as e:
            raise HTTPException(409, str(e)) from None

    @router.get("/api/farm/log")
    def api_farm_log(gate: str = Query(...)):
        """闸门完整日志 (2026-09-16 失败病因结构化配套: 页面「查看完整日志」)。

        路径只信两处: last_status.json 里本系统自己记的 log_file,
        或 runs/ 目录下按文件名 glob 的最新一份 —— 均校验必须落在 RUNS 内,
        不接受页面传来的任意路径。
        """
        if gate not in GATE_NAMES:
            raise HTTPException(400, "未知闸门")
        last = (farm.last_status() or {}).get(gate) or {}
        lf = last.get("log_file")
        # 复审 LOW: last_status.json 可被手工改动, 非字符串/坏路径不能炸成 500
        p = Path(lf) if isinstance(lf, str) and lf else None
        try:
            if not p or not p.exists():
                cands = sorted(RUNS.glob("*/%s.log" % gate),
                               key=lambda x: -x.stat().st_mtime)
                p = cands[0] if cands else None
            if not p or not p.exists():
                raise HTTPException(404, "暂无日志 (该闸门还没跑过或未落盘)")
            rp = p.resolve()
        except OSError:
            # glob/stat 与文件删除的竞态 (TOCTOU) —— 按"没有日志"处理不炸 500
            raise HTTPException(404, "暂无日志 (读取竞态)") from None
        if RUNS.resolve() not in rp.parents:
            raise HTTPException(400, "非法日志路径")
        # 复审 LOW: 大日志不全读 —— 超 2 倍上限只 tail-read 尾部再解码
        try:
            size = rp.stat().st_size
            truncated = size > LOG_MAX_CHARS * 2
            if truncated:
                with open(rp, "rb") as f:
                    f.seek(-LOG_MAX_CHARS * 2, os.SEEK_END)
                    text = f.read().decode("utf-8", errors="replace")
            else:
                text = rp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raise HTTPException(404, "暂无日志 (读取竞态)") from None
        if len(text) > LOG_MAX_CHARS:
            text = "……(日志过长, 只显示最后 %d 字符)……\n\n" % LOG_MAX_CHARS \
                   + text[-LOG_MAX_CHARS:]
        return {"gate": gate, "file": str(rp), "truncated": truncated, "log": text}

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
