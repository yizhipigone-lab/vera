"""VERA Web 服务器 — FastAPI 后端 + 量化前端界面。

启动: python server.py [--port 8080]
访问: http://localhost:8080
"""

import sys
import os
import traceback
from pathlib import Path
from typing import Optional

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

from utils.config_loader import ConfigLoader
from utils.logger import setup_logger, get_logger

logger = setup_logger("VERA-Server", level="INFO")

app = FastAPI(title="VERA 量化回测系统", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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
    initial_capital: float = 200000.0
    commission: float = 0.0003
    slippage: float = 0.001
    max_positions: int = 999
    min_buy_amount: float = 2000.0
    max_buy_amount: float = 10000.0
    lot_size: int = 100
    min_lots: int = 1
    cost_stop_enabled: bool = True
    cost_stop_threshold: float = -0.08
    trailing_enabled: bool = True
    trailing_activation: float = 0.05
    trailing_drawdown: float = 0.03
    ladder_enabled: bool = True
    ladder_levels: str = "0.10:0.33,0.20:0.33,0.30:1.00"
    time_enabled: bool = True
    max_hold_days: int = 20
    cond_time_enabled: bool = False
    cond_time_days: int = 7
    cond_time_profit: float = 0.01
    benchmark_indices: str = "shanghai,chuangyeban,kechuang50,zhongzhengA500"


class PipelineStatus:
    """管线运行状态追踪。"""
    def __init__(self):
        self.running = False
        self.progress = 0
        self.step = ""
        self.error = ""
        self.result = None

pipeline_status = PipelineStatus()


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

    return {
        "strategy": {"name": cfg.strategy_name or "回测"},
        "selection": {
            "formula_name": cfg.formula_name,
            "formula_arg": cfg.formula_arg,
            "universe": {"type": cfg.universe_type, "exclude_st": cfg.exclude_st},
            "period": cfg.period,
            "dividend_type": cfg.dividend_type,
        },
        "time_range": {"start": cfg.start_time, "end": cfg.end_time},
        "backtest": {
            "initial_capital": cfg.initial_capital,
            "commission": cfg.commission,
            "slippage": cfg.slippage,
            "position_sizing": {
                "max_positions": cfg.max_positions,
                "min_buy_amount": cfg.min_buy_amount,
                "max_buy_amount": cfg.max_buy_amount,
                "lot_size": cfg.lot_size,
                "min_lots": cfg.min_lots,
            },
        },
        "stop_loss": {
            "cost_stop": {"enabled": cfg.cost_stop_enabled, "threshold": cfg.cost_stop_threshold},
            "trailing_stop": {"enabled": cfg.trailing_enabled, "activation": cfg.trailing_activation, "drawdown": cfg.trailing_drawdown},
            "ladder_tp": {"enabled": cfg.ladder_enabled, "levels": ladder_levels},
            "time_stop": {"enabled": cfg.time_enabled, "max_hold_days": cfg.max_hold_days},
            "cond_time_stop": {"enabled": cfg.cond_time_enabled, "days": cfg.cond_time_days, "profit": cfg.cond_time_profit},
        },
        "benchmark": {"indices": [s.strip() for s in cfg.benchmark_indices.split(",") if s.strip()]},
    }


@app.get("/api/config/defaults")
async def get_default_config():
    """获取默认配置。"""
    try:
        cfg = ConfigLoader.load_defaults()
        return {"success": True, "config": cfg}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/config/validate")
async def validate_config(cfg: StrategyConfig):
    """校验策略配置。"""
    config_dict = _config_to_yaml_dict(cfg)
    warnings = ConfigLoader.validate_stop_config(config_dict)
    return {"success": True, "warnings": warnings, "config": config_dict}


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
async def run_pipeline(cfg: StrategyConfig):
    """执行完整回测管线。"""
    global pipeline_status

    if pipeline_status.running:
        return JSONResponse(status_code=409, content={"success": False, "error": "管线正在运行中"})

    pipeline_status.running = True
    pipeline_status.progress = 0
    pipeline_status.step = "初始化"
    pipeline_status.error = ""

    # 输入校验
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

    try:
        # 强制清 Numba JIT 缓存 + 重载
        import importlib, numba, shutil, os, glob
        nc = os.path.expanduser('~/__pycache__')
        for p in glob.glob(f'{nc}/**_simulate_core*', recursive=True):
            try: os.remove(p)
            except: pass
        # 清除项目内缓存
        for root, dirs, files in os.walk('.'):
            for f in files:
                if '_simulate_core' in f or f.endswith('.nbc') or f.endswith('.nbi'):
                    try: os.remove(os.path.join(root, f))
                    except: pass
        import backtest.stop_manager, backtest.metrics, backtest.engine
        importlib.reload(backtest.stop_manager)
        importlib.reload(backtest.metrics)
        importlib.reload(backtest.engine)
        from backtest.engine import BacktestEngine

        from core.connector import TdxConnector
        from selection.selector import StockSelector
        from selection.deduplicator import Deduplicator
        from selection.result_writer import ResultWriter
        from backtest.benchmark import BenchmarkComparator
        from report.report_generator import ReportGenerator

        config_dict = _config_to_yaml_dict(cfg)

        # Step 1: 连接 TDX
        pipeline_status.step = "连接通达信"
        pipeline_status.progress = 5
        try:
            TdxConnector.initialize()
        except Exception as e:
            pipeline_status.error = f"TDX连接失败: {e}"
            pipeline_status.running = False
            return {"success": False, "error": f"无法连接通达信: {e}"}

        # Step 2: 选股
        pipeline_status.step = "执行选股"
        pipeline_status.progress = 15
        sel_cfg = config_dict["selection"]
        selector = StockSelector(sel_cfg)
        stocks = selector.resolve_universe()

        logger.info(f"选股股票池: {len(stocks)} 只")
        selections = selector.run(
            start_time=cfg.start_time,
            end_time=cfg.end_time,
            stock_list=stocks,
        )

        if selections.empty:
            pipeline_status.error = "选股结果为空"
            pipeline_status.running = False
            TdxConnector.close()
            return {"success": False, "error":
                f"选股结果为空。请确认：\n"
                f"1. 公式 [{cfg.formula_name}] 是否存在于通达信中\n"
                f"2. 时间范围 {cfg.start_time}~{cfg.end_time} 内是否有盘后数据\n"
                f"3. 通达信客户端是否已完成盘后数据下载"}

        logger.info(f"选股完成: {len(selections)} 条记录")

        # Step 3: 回测
        pipeline_status.step = "执行回测"
        pipeline_status.progress = 40
        bt_cfg = config_dict.get("backtest", {})
        stop_cfg = config_dict.get("stop_loss", {})
        engine = BacktestEngine(bt_cfg)

        backtest_result = engine.run(
            selections=selections,
            start_time=cfg.start_time,
            end_time=cfg.end_time,
            stop_config=stop_cfg,
        )

        # Step 4: 基准对比
        pipeline_status.step = "基准对比"
        pipeline_status.progress = 70
        bench_cfg = config_dict.get("benchmark", {})
        comparator = BenchmarkComparator(bench_cfg)
        equity_curve = backtest_result.get("equity_curve", pd.DataFrame())

        benchmark_results = {}
        if not equity_curve.empty:
            benchmark_results = comparator.fetch_and_compare(
                equity_curve,
                start_time=cfg.start_time,
                end_time=cfg.end_time,
            )

        # Step 5: 生成报告
        pipeline_status.step = "生成报告"
        pipeline_status.progress = 85
        report_gen = ReportGenerator(dark_theme=True)
        date_range = f"{cfg.start_time} ~ {cfg.end_time}"
        report_outputs = report_gen.generate(
            backtest_result=backtest_result,
            benchmark_results=benchmark_results,
            strategy_name=cfg.strategy_name,
            date_range=date_range,
        )

        # 准备返回数据
        metrics = backtest_result.get("metrics", {})
        trades = backtest_result.get("trades", pd.DataFrame())
        equity_curve = backtest_result.get("equity_curve", pd.DataFrame())

        # 获取股票简称映射
        stock_names = {}
        try:
            from tqcenter import tq
            raw = tq.get_stock_list("50", list_type=1)
            for s in raw:
                if isinstance(s, dict) and s.get("Code"):
                    stock_names[s["Code"]] = s.get("Name", "")
        except Exception:
            pass

        # 序列化 (处理 NaN/Inf)
        def safe_serialize(obj):
            if isinstance(obj, (np.integer,)):
                return int(obj)
            if isinstance(obj, (np.floating,)):
                v = float(obj)
                if np.isnan(v) or np.isinf(v):
                    return None
                return v
            if isinstance(obj, float):
                if np.isnan(obj) or np.isinf(obj):
                    return None
                return obj
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            if isinstance(obj, pd.Timestamp):
                return obj.isoformat()
            return obj

        metrics_clean = {k: safe_serialize(v) for k, v in metrics.items()}

        equity_data = []
        if not equity_curve.empty:
            for _, row in equity_curve.iterrows():
                equity_data.append({
                    "date": str(row.get("date", "")),
                    "equity": safe_serialize(row.get("equity", 0)),
                    "drawdown": safe_serialize(row.get("drawdown", 0)),
                })

        trades_data = []
        if not trades.empty:
            for _, row in trades.tail(100).iterrows():
                d = {}
                for col in trades.columns:
                    d[col] = safe_serialize(row[col])
                # 补充股票简称
                code = d.get("stock_code", "")
                d["stock_name"] = stock_names.get(code, code)
                trades_data.append(d)

        benchmark_data = {}
        for name, bm_df in benchmark_results.items():
            if bm_df.empty:
                continue
            bm_list = []
            for _, row in bm_df.iterrows():
                bm_list.append({
                    "date": str(row.name if bm_df.index.name == "date" else row.get("date", "")),
                    "strategy_equity": safe_serialize(row.get("strategy_equity", 0)),
                    "index_close": safe_serialize(row.get("index_close", 0)),
                    "excess_return": safe_serialize(row.get("excess_return", 0)),
                })
            benchmark_data[name] = bm_list

        pipeline_status.progress = 100
        pipeline_status.step = "完成"
        response_data = {
            "success": True,
            "metrics": metrics_clean,
            "equity": equity_data,
            "trades": trades_data,
            "benchmarks": benchmark_data,
            "stop_config_summary": backtest_result.get("stop_config_summary", ""),
            "report_url": report_outputs.get("html", ""),
            "trade_count": len(trades),
        }
        pipeline_status.result = response_data

        # 持久化到磁盘（历史记录 + last_result）
        try:
            import json as _json
            results_dir = _PROJECT_ROOT / "output" / "results"
            results_dir.mkdir(parents=True, exist_ok=True)
            ts = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
            # 保存完整结果
            result_path = results_dir / f"{ts}.json"
            meta = {
                "id": ts, "time": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
                "formula": cfg.formula_name, "date_range": f"{cfg.start_time}~{cfg.end_time}",
                "trade_count": len(trades), "cumulative_return": metrics_clean.get("cumulative_return", 0),
            }
            with open(result_path, "w", encoding="utf-8") as f:
                _json.dump({"meta": meta, "data": response_data}, f, ensure_ascii=False, default=str)
            # 更新索引
            index_path = results_dir / "index.json"
            index_data = []
            if index_path.exists():
                with open(index_path, "r", encoding="utf-8") as f:
                    index_data = _json.load(f)
            index_data.insert(0, meta)
            with open(index_path, "w", encoding="utf-8") as f:
                _json.dump(index_data[:50], f, ensure_ascii=False)  # 保留最近50条
            # 同时更新 last_result
            persist_path = _PROJECT_ROOT / "output" / "last_result.json"
            with open(persist_path, "w", encoding="utf-8") as f:
                _json.dump(response_data, f, ensure_ascii=False, default=str)
        except Exception:
            pass

        try:
            TdxConnector.close()
        except Exception:
            pass

        pipeline_status.running = False
        return response_data

    except Exception as e:
        pipeline_status.error = str(e)
        pipeline_status.result = None
        pipeline_status.running = False
        logger.exception("管线执行失败")
        try:
            from core.connector import TdxConnector
            TdxConnector.close()
        except Exception:
            pass
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


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
    """加载指定历史回测结果。"""
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
    uvicorn.run(app, host=args.host, port=args.port)
