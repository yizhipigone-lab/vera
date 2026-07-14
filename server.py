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
import yaml

from utils.config_loader import ConfigLoader
from utils.logger import setup_logger, get_logger

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

    def get(self, key: str, default=None):
        """安全获取字段值，None 时返回默认值。"""
        DEFAULTS = {
            "initial_capital": 1000000.0, "commission": 0.0003, "slippage": 0.001,
            "min_buy_amount": 2000.0, "max_buy_amount": 20000.0,
            "lot_size": 100, "min_lots": 1,
            "cost_stop_threshold": -0.12, "trailing_activation": 0.08,
            "trailing_drawdown": 0.05, "max_hold_days": 20,
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
            "trailing_stop": {"enabled": cfg.trailing_enabled, "activation": cfg.get("trailing_activation", 0.08), "drawdown": cfg.get("trailing_drawdown", 0.05)},
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
    """执行完整回测管线。P2-1: 改为同步路由，FastAPI 自动丢线程池，不再阻塞事件循环。"""
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
        import importlib
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

        # 候选 E: TDX 就绪 (避免 5→15 黑区)
        pipeline_status.step = "TDX 就绪"
        pipeline_status.progress = 8

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
        # 候选 E: 回测引擎就绪 (避免 15→40 黑区)
        # 修复 2026-07-14: 原顺序 progress 40(执行)→30(准备) 倒退,改为先准备(30)再执行(40),进度单调递增 15→30→40→70
        pipeline_status.step = "准备回测"
        pipeline_status.progress = 30
        bt_cfg = config_dict.get("backtest", {})
        stop_cfg = config_dict.get("stop_loss", {})
        engine = BacktestEngine(bt_cfg)
        pipeline_status.step = "执行回测"
        pipeline_status.progress = 40

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
        # 传入回测周期，让基准对齐5m
        if "period" in config_dict.get("backtest", {}) and "period" not in bench_cfg:
            bench_cfg = {**bench_cfg, "period": config_dict["backtest"]["period"]}
        comparator = BenchmarkComparator(bench_cfg)
        equity_curve = backtest_result.get("equity_curve", pd.DataFrame())

        benchmark_results = {}
        if not equity_curve.empty:
            benchmark_results = comparator.fetch_and_compare(
                equity_curve,
                start_time=cfg.start_time,
                end_time=cfg.end_time,
            )
        # 候选 E: 基准完成 (避免 70→85 黑区)
        pipeline_status.step = "基准完成"
        pipeline_status.progress = 75

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

        # P3-2 加速: 报告已生成即通知前端(95%), 序列化+写盘不挡轮询
        pipeline_status.progress = 95
        pipeline_status.step = "准备返回数据"

        metrics = backtest_result.get("metrics", {})
        trades = backtest_result.get("trades", pd.DataFrame())
        equity_curve = backtest_result.get("equity_curve", pd.DataFrame())

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

        # P3-2 加速: trades 序列化 — iterrows 5180 行极慢, 改 to_dict + 批量处理
        trades_data = []
        if not trades.empty:
            trades_data = trades.to_dict(orient="records")
            for d in trades_data:
                for k, v in list(d.items()):
                    d[k] = safe_serialize(v)
            # 查全量简称表 (带进程级缓存, 首次 ~1s, 后续 O(1))
            try:
                from core.data_fetcher import DataFetcher
                name_map = DataFetcher.get_name_map()
            except Exception:
                name_map = {}
            for d in trades_data:
                code = d.get("stock_code", "")
                # 真实简称优先; 查不到回退空 (前端会显示代码)
                d["stock_name"] = name_map.get(code, "")

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
                # F2 回归保护: 标记买入语义 (signal-day-close: 信号日收盘价 / t+1-open: 已废止)
                "engine_version": "signal-day-close", "entry_price_basis": "close_on_signal_day",
            }
            # data 顶层也加一份, 前端 fetch response 直接读到
            if isinstance(response_data, dict):
                response_data["engine_version"] = "signal-day-close"
                response_data["entry_price_basis"] = "close_on_signal_day"
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
