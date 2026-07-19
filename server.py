"""VERA Web 服务器 — FastAPI 后端 + 量化前端界面。

启动: python server.py [--port 8080]
访问: http://localhost:8080
"""

import sys
import os
import traceback
from pathlib import Path
from typing import Optional, Dict, Any

# F2 回归保护常量（与 pipeline/result_writer.py 共用，破解循环 import 后统一引用）
from pipeline.result_writer import ENGINE_VERSION, ENTRY_PRICE_BASIS
# C1-3: Pipeline 统一入口 + ResultWriter 序列化/落盘
from pipeline.pipeline import Pipeline
from pipeline.result_writer import ResultWriter
# 2026-07-17: 协作式停止标志 (停止回测按钮)
from core.stop_flag import BacktestStoppedError, clear_stop, request_stop

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
import pandas as pd
import numpy as np
import yaml

from utils.config_loader import ConfigLoader
from utils.logger import setup_logger, get_logger
from backtest.stop_config import (
    DEFAULT_TRAILING_ACTIVATION,
    DEFAULT_TRAILING_DRAWDOWN,
)

logger = setup_logger("VERA-Server", level="INFO")

app = FastAPI(title="VERA 量化回测系统", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:8080", "http://localhost:8080"], allow_methods=["*"], allow_headers=["*"])

# 静态文件
app.mount("/output", StaticFiles(directory=str(_PROJECT_ROOT / "output")), name="output")
app.mount("/web", StaticFiles(directory=str(_PROJECT_ROOT / "web")), name="web")

# ====== 数据模型 ======

class StrategyConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    strategy_name: str = ""
    formula_name: str = "UPN"
    formula_arg: str = "3"
    universe_type: str = "50"
    exclude_st: bool = True
    start_time: str = "20240101"
    end_time: str = "20250630"
    period: str = "1d"
    dividend_type: int = 1
    initial_capital: Optional[float] = None
    commission: Optional[float] = None
    slippage: Optional[float] = None
    max_positions: int = 999
    min_buy_amount: Optional[float] = None
    max_buy_amount: Optional[float] = None
    lot_size: Optional[int] = None
    min_lots: Optional[int] = None
    cost_stop_enabled: bool = True
    cost_stop_threshold: Optional[float] = None
    trailing_enabled: bool = True
    trailing_activation: Optional[float] = None
    trailing_drawdown: Optional[float] = None
    ladder_enabled: bool = True
    ladder_levels: str = "6:30,15:30"
    time_enabled: bool = True
    max_hold_days: Optional[int] = None
    cond_time_enabled: bool = False
    cond_time_days: Optional[int] = None
    cond_time_profit: Optional[float] = None
    first_day_enabled: bool = False
    first_day_target: Optional[float] = None
    benchmark_indices: str = "shanghai,hs300,zz500,chuangyeban,kechuang50,zhongzhengA500"
    # P-v3.4: ETF 开关 (混合选股 / 仅ETF)
    include_etf: bool = False
    etf_only: bool = False
    # P-v3.4: 行业板块 (逗号分隔代码, 如 "881319.SH,881326.SH")
    sectors: str = ""
    # 2026-07-19: 因子过滤(按公式存终审规则勾选状态)
    # 形如 {"QUANTQQ": {"enabled": true, "rules": ["dist_ma20:top10"]}}
    factor_filter: Optional[Dict[str, Any]] = None

    def get(self, key: str, default=None):
        """安全获取字段值，None 时返回默认值。"""
        DEFAULTS = {
            "initial_capital": 1000000.0, "commission": 0.0003, "slippage": 0.001,
            "min_buy_amount": 2000.0, "max_buy_amount": 20000.0,
            "lot_size": 100, "min_lots": 1,
            "cost_stop_threshold": -0.12, "trailing_activation": DEFAULT_TRAILING_ACTIVATION,
            "trailing_drawdown": DEFAULT_TRAILING_DRAWDOWN, "max_hold_days": 20,
            "cond_time_days": 7, "cond_time_profit": 0.02,
            "first_day_target": 0.03,
        }
        val = getattr(self, key, None)
        if val is None:
            return DEFAULTS.get(key, default)
        return val


class PipelineStatus:
    """管线运行状态追踪。

    不变量（前端 tryRecoverAbortedResult 依赖）:
      - result 落盘到 last_result.json 之前，running 必须为 True
      - running 翻为 False 之前，result 必须已赋值（即使失败也要 .result=None）
      - 单一写入者（/api/run 的 try/finally），无需锁
    """
    def __init__(self):
        self.running = False
        self.progress = 0
        self.step = ""
        self.error = ""
        self.result = None

pipeline_status = PipelineStatus()

# 2026-07-20 审计 H1: /api/run 检查+置位原子锁
import threading as _threading
_run_lock = _threading.Lock()

# ====== 公式体检队列(2026-07-20, 计划书 docs/plan/2026-07-20_公式体检页面_计划书.md) ======
# 严格串行; 与 /api/run 不对称互斥: 体检永远排队, 回测提交在体检运行中 → 409
sys.path.insert(0, str(_PROJECT_ROOT / "tools"))
from lab_runner import LabQueue  # noqa: E402

lab_status = LabQueue(pipeline_busy=lambda: pipeline_status.running)


# ====== 配置端点 ======

def _config_to_yaml_dict(cfg: StrategyConfig) -> dict:
    """将前端配置转为策略 YAML 字典。"""
    # 解析阶梯止盈
    ladder_levels = []
    if cfg.ladder_enabled and cfg.ladder_levels:
        for item in cfg.ladder_levels.split(","):
            parts = item.strip().split(":")
            if len(parts) == 2:
                ladder_levels.append({
                    "profit": float(parts[0]),
                    "sell_ratio": float(parts[1]),
                })

    # 选股始终用日线，回测层可用5m
    if cfg.period == "5m":
        sel_period, bt_period = "1d", "5m"
    else:
        sel_period = bt_period = cfg.period

    return {
        "strategy": {"name": cfg.strategy_name or "回测"},
        "selection": {
            "formula_name": cfg.formula_name,
            "formula_arg": cfg.formula_arg,
            "universe": {
                "type": cfg.universe_type,
                "exclude_st": cfg.exclude_st,
                # P-v3.4: ETF 开关
                "include_etf": bool(getattr(cfg, "include_etf", False)),
                "etf_only": bool(getattr(cfg, "etf_only", False)),
                # P-v3.4: 行业板块代码列表 (逗号分隔字符串 → list)
                "sectors": [s.strip() for s in str(getattr(cfg, "sectors", "") or "").split(",") if s.strip()],
            },
            "period": sel_period,
            "dividend_type": cfg.dividend_type,
        },
        "time_range": {"start": cfg.start_time, "end": cfg.end_time},
        "backtest": {
            "initial_capital": cfg.get("initial_capital", 1000000.0),
            "commission": cfg.get("commission", 0.0003),
            "slippage": cfg.get("slippage", 0.001),
            "period": bt_period,
            "position_sizing": {
                "max_positions": cfg.max_positions,
                "min_buy_amount": cfg.get("min_buy_amount", 2000.0),
                "max_buy_amount": cfg.get("max_buy_amount", 20000.0),
                "lot_size": cfg.get("lot_size", 100),
                "min_lots": cfg.get("min_lots", 1),
            },
        },
        "stop_loss": {
            # 2026-07-05: 优先级开关 (前端 cfgPriority radio 透传, 默认 trailing_first)
            "priority": str(cfg.get("priority", "trailing_first")),
            "cost_stop": {"enabled": cfg.cost_stop_enabled, "threshold": cfg.get("cost_stop_threshold", -0.12)},
            "trailing_stop": {
                "enabled": cfg.trailing_enabled,
                "activation": cfg.get(
                    "trailing_activation", DEFAULT_TRAILING_ACTIVATION
                ),
                "drawdown": cfg.get(
                    "trailing_drawdown", DEFAULT_TRAILING_DRAWDOWN
                ),
            },
            "ladder_tp": {"enabled": cfg.ladder_enabled, "levels": ladder_levels},
            "time_stop": {"enabled": cfg.time_enabled, "max_hold_days": cfg.get("max_hold_days", 20)},
            "cond_time_stop": {"enabled": cfg.cond_time_enabled, "days": cfg.get("cond_time_days", 7), "profit": cfg.get("cond_time_profit", 0.01)},
            "first_day": {"enabled": cfg.first_day_enabled, "target": cfg.get("first_day_target", 0.03)},
            # P-v3.4: 公式卖出 (formula_sell) — 前端配置透传到 engine.run(stop_config=...)
            "formula_sell": {
                "enabled": bool(cfg.get("formula_sell_enabled", False)),
                "formula_name": str(cfg.get("formula_sell_name", "")),
                "formula_arg": "",
                "sell_ratio": float(cfg.get("formula_sell_ratio", 1.0)),
                "priority": 0,
            },
        },
        "benchmark": {"indices": [s.strip() for s in cfg.benchmark_indices.split(",") if s.strip()]},
        # 2026-07-19: 因子过滤(按公式存; pipeline 在选股后/回测前应用)
        "factor_filter": cfg.factor_filter or {},
    }


@app.get("/api/config/defaults")
async def get_default_config():
    """获取默认配置。"""
    try:
        cfg = ConfigLoader.load_defaults()
        return {"success": True, "config": cfg}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/sectors")
async def get_sectors():
    """P-v3.4: 获取 128 个细分行业板块列表 (list_type=11), 带进程级缓存."""
    try:
        from core.data_fetcher import DataFetcher
        sectors = DataFetcher.get_sector_list()
        return {"success": True, "count": len(sectors), "sectors": sectors}
    except Exception as e:
        logger.error(f"获取行业板块列表失败: {e}")
        return {"success": False, "error": str(e), "sectors": [], "count": 0}


@app.post("/api/config/validate")
async def validate_config(cfg: StrategyConfig):
    """校验策略配置。"""
    config_dict = _config_to_yaml_dict(cfg)
    warnings = ConfigLoader.validate_stop_config(config_dict)
    return {"success": True, "warnings": warnings, "config": config_dict}


# ====== 前端配置存取（current.yaml） — 2026-07-10 ======
# 单一覆盖文件：保存覆盖 current.yaml（自动备份 .bak）；加载与 default.yaml 合并；删除幂等。

@app.post("/api/config/save")
async def save_config_to_file(cfg: StrategyConfig):
    """保存前端配置到 config/current.yaml（覆盖前自动复制备份 .bak）。"""
    try:
        config_dict = _config_to_yaml_dict(cfg)                      # 复用，零改动
        warnings = ConfigLoader.validate_stop_config(config_dict)    # 不阻塞，仅回传
        path = ConfigLoader.save_current(config_dict)
        return {"success": True, "warnings": warnings, "saved_at": path.stat().st_mtime}
    except PermissionError:
        # Windows: 用户在编辑器里开着 current.yaml 时 os.replace 会失败
        return {"success": False, "error": "文件被占用，请关闭编辑器（VS Code/记事本）中的 current.yaml 后重试"}
    except Exception as e:
        logger.error(f"保存配置失败: {e}")
        return {"success": False, "error": str(e)}


@app.get("/api/config/saved")
async def get_saved_config():
    """读取已保存配置（与 default.yaml 合并后的完整 dict）。不存在或解析失败返回 exists:false。"""
    try:
        if not ConfigLoader.current_exists():
            return {"success": False, "exists": False, "error": "暂无保存的配置"}
        cfg = ConfigLoader.load_current()                            # 合并后的完整 dict
        return {"success": True, "exists": True, "config": cfg}
    except yaml.YAMLError as e:
        # 用户手改 current.yaml 写坏时的兜底（不裸抛 500）
        return {"success": False, "exists": False, "error": f"current.yaml 解析失败（请检查缩进/格式）: {e}"}
    except Exception as e:
        logger.error(f"读取已保存配置失败: {e}")
        return {"success": False, "exists": False, "error": str(e)}


@app.delete("/api/config/saved")
async def delete_saved_config():
    """删除 config/current.yaml（幂等；保留 .bak 作为最后备份）。"""
    try:
        existed = ConfigLoader.delete_current()
        return {"success": True, "existed": existed}
    except Exception as e:
        logger.error(f"删除配置失败: {e}")
        return {"success": False, "error": str(e)}


@app.get("/api/factor-rules")
async def get_factor_rules(formula: str):
    """读 formula_lab 产出的过滤规则 JSON(output/reports/{formula}_filter_rules.json),
    供前端因子过滤区按公式动态渲染。未体检过返回 exists:false。"""
    import json as _json
    from lab_runner import FORMULA_RE
    if not FORMULA_RE.match(formula or ""):
        return JSONResponse(status_code=400, content={"success": False, "error": "公式名含非法字符"})
    path = Path(__file__).resolve().parent / "output" / "reports" / f"{formula}_filter_rules.json"
    if not path.exists():
        return {"success": True, "exists": False, "rules": [],
                "hint": f"{formula} 未体检, 先跑: python tools/formula_lab.py --formula {formula} --tag <短窗> --tag2 <长窗>"}
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
        return {"success": True, "exists": True, **data}
    except Exception as e:
        return {"success": False, "exists": False, "error": f"规则文件解析失败: {e}"}


# ====== 公式体检端点(2026-07-20) ======

class LabRunRequest(BaseModel):
    formulas: list
    tag: Optional[str] = None
    tag2: Optional[str] = None


def _default_tags() -> tuple:
    """缺省窗口: 近1年 / 近3年(按当日滚动), tag 格式 YYYYMMDD_YYYYMMDD。"""
    from datetime import datetime as _dt, timedelta as _td
    end = _dt.now()
    def _t(d): return d.strftime("%Y%m%d")
    return (_t(end - _td(days=365)) + "_" + _t(end),
            _t(end - _td(days=365 * 3)) + "_" + _t(end))


@app.post("/api/lab/run")
async def lab_run(req: LabRunRequest):
    """提交体检任务(永远 FIFO 排队; 回测在跑时显示排队原因)。"""
    formulas = [str(f).strip() for f in req.formulas if str(f).strip()]
    if len(formulas) > 10:
        return JSONResponse(status_code=400, content={"success": False, "error": "单次最多 10 个公式"})
    tag, tag2_default = _default_tags()
    tag = req.tag or tag
    tag2 = req.tag2 if req.tag2 is not None else tag2_default
    tag2 = tag2 or None   # 2026-07-20 冒烟修复: 空串 = 显式单窗口(报告标"待复核"); 缺省 = 近3年
    import re as _re
    for t in [tag, tag2]:
        if t and not _re.match(r"^\d{8}_\d{8}$", t):
            return JSONResponse(status_code=400, content={"success": False, "error": f"窗口格式应为 YYYYMMDD_YYYYMMDD: {t}"})
    ids, err = lab_status.submit(formulas, tag, tag2)
    if err:
        return JSONResponse(status_code=400, content={"success": False, "error": err})
    return {"success": True, "task_ids": ids, "tag": tag, "tag2": tag2,
            "queued_behind_pipeline": bool(pipeline_status.running)}


@app.get("/api/lab/status")
async def lab_status_api():
    return lab_status.snapshot()


@app.get("/api/lab/history")
async def lab_history():
    """历史体检: 按公式聚合 规则JSON + 体检报告 md。"""
    import json as _json
    out = {}
    rep_dir = Path(__file__).resolve().parent / "output" / "reports"
    for p in rep_dir.glob("*_filter_rules.json"):
        try:
            d = _json.loads(p.read_text(encoding="utf-8"))
            formula = d.get("formula") or p.name.replace("_filter_rules.json", "")
            adopted = sum(1 for r in d.get("rules", []) if r.get("adopted"))
            out.setdefault(formula, {"formula": formula, "generated_at": d.get("generated_at", ""),
                                     "rules": len(d.get("rules", [])), "adopted": adopted,
                                     "tags": d.get("tags", [])})
        except Exception:
            continue
    audit_dir = Path(__file__).resolve().parent / "docs" / "audit"
    for p in sorted(audit_dir.glob("*因子体检报告.md")):
        for formula in out:
            if f"_{formula}_" in p.name:
                out[formula]["report"] = p.name
                out[formula]["report_date"] = p.name[:10]
    return {"success": True, "items": sorted(out.values(), key=lambda x: x.get("generated_at", ""), reverse=True)}


@app.get("/api/lab/report")
async def lab_report(formula: str):
    """返回该公式最近一次体检报告 markdown。"""
    from lab_runner import FORMULA_RE
    if not FORMULA_RE.match(formula or ""):
        return JSONResponse(status_code=400, content={"success": False, "error": "公式名含非法字符"})
    audit_dir = Path(__file__).resolve().parent / "docs" / "audit"
    cands = sorted(audit_dir.glob(f"*_{formula}_*因子体检报告.md"))
    if not cands:
        return {"success": False, "error": f"{formula} 无体检报告"}
    p = cands[-1]
    try:
        return {"success": True, "file": p.name, "markdown": p.read_text(encoding="utf-8")}
    except Exception as e:
        return {"success": False, "error": f"报告读取失败: {e}"}


# ====== 管线端点 ======

@app.get("/api/status")
async def get_status():
    """获取管线运行状态。"""
    return {
        "running": pipeline_status.running,
        "progress": pipeline_status.progress,
        "step": pipeline_status.step,
        "error": pipeline_status.error,
        "has_result": pipeline_status.result is not None,
    }


@app.post("/api/run")
def run_pipeline(cfg: StrategyConfig):
    """执行完整回测管线。

    C1-3: 走 Pipeline.run + ResultWriter（统一完整流程接缝）。
    删除了: importlib.reload 三连击、5个直调 import、250行手写编排。
    进度回调由 Pipeline 内部通过 ResultWriter.on_progress 驱动 pipeline_status 单例。
    """
    global pipeline_status

    # 2026-07-20 审计 H1: 检查+置位必须原子(sync def 在线程池真并发, 竞态可双跑回测)
    with _run_lock:
        if pipeline_status.running:
            return JSONResponse(status_code=409, content={"success": False, "error": "管线正在运行中"})
        # 2026-07-20: 体检运行中 → 409(不对称互斥; 体检提交则永远排队, 见 /api/lab/run)
        if lab_status.running:
            return JSONResponse(status_code=409, content={"success": False, "error": "公式体检运行中,请稍后"})
        clear_stop()  # 2026-07-17: 清掉上一次停止残留的标志, 防新回测被秒杀
        pipeline_status.running = True
        pipeline_status.progress = 0
        pipeline_status.step = "初始化"
        pipeline_status.error = ""

    # 输入校验（保留）
    import re
    if not re.match(r'^\d{8}$', cfg.start_time) or not re.match(r'^\d{8}$', cfg.end_time):
        pipeline_status.running = False
        return {"success": False, "error": "日期格式错误，应为 YYYYMMDD（8位数字），如 20240101"}
    if cfg.start_time >= cfg.end_time:
        pipeline_status.running = False
        return {"success": False, "error": "起始日期必须早于结束日期"}
    if not cfg.formula_name.strip():
        pipeline_status.running = False
        return {"success": False, "error": "选股公式名称不能为空"}

    # C1-3: 构建 YAML 配置临时文件，Pipeline(run) 接收路径字符串
    import tempfile, os as _os
    config_dict = _config_to_yaml_dict(cfg)
    tmp_yaml = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as fp:
            yaml.safe_dump(config_dict, fp, allow_unicode=True)
            tmp_yaml = fp.name

        # C1-3: ResultWriter 作为 progress_callback 适配器，驱动 pipeline_status
        # P2-3 (2026-07-15): status_sink 显式传闭包, 不能用 None fallback —
        # python server.py 时模块名是 __main__ 非 server, from server import pipeline_status
        # 会重新导入创建克隆体, 进度更新全写到错误实例.
        def _update_status(step: str, pct: int):
            pipeline_status.step = step
            pipeline_status.progress = pct

        writer = ResultWriter(status_sink=_update_status)

        # C1-3: 一行调用统一完整流程
        # 2026-07-16: close_on_finish=False —— server 常驻进程, TDX 连接进程内长存,
        # 不在每次回测后断开。避免反复 close→reinit 触发本地握手偶发失败
        # (症状: 切周期/重跑时"无法连接到 TDX", 多点几次又好)。
        pipeline = Pipeline(tmp_yaml)
        result = pipeline.run(progress_callback=writer.on_progress, close_on_finish=False)

        # C1-3: 先断言类型，再访问 error 字段（HIGH-1 修复：防止 plain dict 先触发 get("error") 掩盖类型错误）
        from pipeline.result_writer import PipelineResult
        if not isinstance(result, PipelineResult):
            # 早期错误路径：Pipeline.run 在 TDX 连接失败等场景返回 plain dict
            pipeline_status.running = False
            return {"success": False, "error": f"Pipeline 返回类型未知: {type(result)}"}

        # C1-3: 选股为空由 Pipeline 内部返回 {error: "no_selections", ...}
        err = result.get("error")
        if err == "no_selections":
            pipeline_status.running = False
            return {"success": False, "error":
                f"选股结果为空。请确认：\n"
                f"1. 公式 [{cfg.formula_name}] 是否存在于通达信中\n"
                f"2. 时间范围 {cfg.start_time}~{cfg.end_time} 内是否有盘后数据\n"
                f"3. 通达信客户端是否已完成盘后数据下载"}

        if err:
            pipeline_status.running = False
            return {"success": False, "error": str(err)}
        response_data = writer.serialize(result)

        # C1-3: 落盘三文件（替换原来的手写 persist 块）
        writer.persist(
            response_data,
            results_dir=_PROJECT_ROOT / "output" / "results",
            last_result_path=_PROJECT_ROOT / "output" / "last_result.json",
            meta_extras={"formula": cfg.formula_name, "date_range": f"{cfg.start_time}~{cfg.end_time}"},
        )

        pipeline_status.result = response_data
        pipeline_status.running = False
        return response_data

    except BacktestStoppedError as e:
        # 2026-07-17: 用户点「停止回测」——不停 TDX 连接, 不落盘, 直接收尾
        pipeline_status.error = str(e)
        pipeline_status.result = None
        pipeline_status.running = False
        logger.info("回测被用户手动停止")
        return {"success": False, "error": str(e), "stopped": True}

    except Exception as e:
        pipeline_status.error = str(e)
        pipeline_status.result = None
        pipeline_status.running = False
        logger.exception("管线执行失败")
        try:
            from core.connector import TdxConnector
            TdxConnector.close()
        except Exception:
            # P3 (2026-07-15): 清理路径异常不阻塞响应, 但留痕 (debug 级, 不污染 INFO 日志)
            logger.debug("TdxConnector.close 异常 (清理路径, 不阻塞响应)", exc_info=True)
        return {"success": False, "error": f"{type(e).__name__}: {e}"}

    finally:
        # 清理临时 YAML 文件
        if tmp_yaml and _os.path.exists(tmp_yaml):
            try:
                _os.unlink(tmp_yaml)
            except Exception:
                # P3 (2026-07-15): 清理路径异常, debug 留痕
                logger.debug("临时 YAML 文件清理失败 (不阻塞响应)", exc_info=True)


@app.post("/api/stop")
def stop_pipeline():
    """停止当前回测 (2026-07-17)。

    协作式停止: 置全局标志, 数据拉取/回测循环在下一轮检查点抛出
    BacktestStoppedError, 通常几秒内生效 (拉取阶段最慢不超过当前这只股票)。
    """
    if not pipeline_status.running:
        return {"success": False, "error": "当前没有运行中的回测"}
    # 注意: 不写 pipeline_status (单一写入者=/api/run), 前端日志已提示"正在停止"
    request_stop()
    logger.info("收到停止回测请求")
    return {"success": True}


@app.get("/api/last_result")
async def get_last_result():
    """获取上次回测的持久化结果。"""
    persist_path = _PROJECT_ROOT / "output" / "last_result.json"
    if persist_path.exists():
        import json as _json
        with open(persist_path, "r", encoding="utf-8") as f:
            return _json.load(f)
    return {"success": False, "error": "暂无历史回测结果"}


@app.get("/api/results")
async def list_results():
    """获取历史回测列表。"""
    index_path = _PROJECT_ROOT / "output" / "results" / "index.json"
    if index_path.exists():
        import json as _json
        with open(index_path, "r", encoding="utf-8") as f:
            return _json.load(f)
    return []


@app.get("/api/results/{result_id}")
async def get_result(result_id: str):
    """加载指定历史回测结果。P2-2: result_id 正则校验防路径遍历。"""
    import re
    if not re.match(r'^\d{8}_\d{6}$', result_id):
        return JSONResponse(status_code=400, content={"success": False, "error": "result_id 格式错误"})
    result_path = _PROJECT_ROOT / "output" / "results" / f"{result_id}.json"
    if result_path.exists():
        import json as _json
        with open(result_path, "r", encoding="utf-8") as f:
            return _json.load(f)
    return {"success": False, "error": "结果不存在"}


# ====== 主页面 ======

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = _PROJECT_ROOT / "web" / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")

@app.get("/favicon.ico")
async def favicon():
    """重定向到 SVG 图标，消除 404 日志噪音。"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/web/favicon.svg")
    return "<h1>VERA Web 前端未找到，请创建 web/index.html</h1>"


# ====== 启动 ======

if __name__ == "__main__":
    import uvicorn
    import argparse
    parser = argparse.ArgumentParser(description="VERA Web 服务器")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()

    logger.info(f"VERA 量化回测系统 Web 服务器启动")
    logger.info(f"访问: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)
