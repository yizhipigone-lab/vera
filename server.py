"""VERA Web 服务器 — FastAPI 后端 + 量化前端界面。

启动: python server.py [--port 8080]
访问: http://localhost:8080

2026-08-01 批次5 C4c 拆分 (纯移动不改行为):
- config_mapper.py: StrategyConfig 模型 + _config_to_yaml_dict (本文件 re-export 兼容旧 import 路径)
- lab_api.py: /api/lab/* 路由 (create_lab_router 工厂注入 lab_status/pipeline_status)
- research_api.py: /api/research/* 路由 (政策影响 + 对话大脑)
"""

import json
import re
import sys
from pathlib import Path

# 2026-07-17: 协作式停止标志 (停止回测按钮)
from core.stop_flag import BacktestStoppedError, clear_stop, request_stop

# C1-3: Pipeline 统一入口 + ResultWriter 序列化/落盘
from pipeline.pipeline import Pipeline
from pipeline.result_writer import ResultWriter

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# C4c: 配置模型/映射与两个路由模块 (re-export 保持 from server import StrategyConfig 可用)
from config_mapper import StrategyConfig, _config_to_yaml_dict  # noqa: F401
from lab_api import create_lab_router
from research_api import router as research_router
from utils.config_loader import ConfigLoader
from utils.logger import setup_logger

logger = setup_logger("VERA-Server", level="INFO")


def _read_json(path: Path):
    """读取 JSON 文件 (UTF-8) 并解析, 供结果类端点复用。"""
    return json.loads(path.read_text(encoding="utf-8"))

app = FastAPI(title="VERA 量化回测系统", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])  # 2026-08-18: 放行局域网手机访问

# 静态文件
app.mount("/output", StaticFiles(directory=str(_PROJECT_ROOT / "output")), name="output")
app.mount("/web", StaticFiles(directory=str(_PROJECT_ROOT / "web")), name="web")


@app.on_event("startup")
def _startup_cache_check():
    """启动自检: K线缓存不新鲜则后台补拉 (2026-08-14, 5M 回测超时事故)。

    与 scheduler 每日 15:45 的定时补拉是双保险 —— 调度器没常驻/电脑关机/
    周末启动回测时, 靠这个自检兜底。非阻塞 (检查毫秒级, 补拉在后台线程),
    fail-soft (缓存问题永不挡服务启动)。
    """
    try:
        from core.kline_cache_maintenance import ensure_cache_fresh
        logger.info(f"K线缓存启动自检: {ensure_cache_fresh(trigger='server_startup')}")
    except Exception as e:
        logger.warning(f"K线缓存启动自检异常 (不影响服务): {e}")

# ====== 数据模型 ======
# StrategyConfig / _config_to_yaml_dict 已抽至 config_mapper.py (C4c), 上方 re-export。

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
_last_served_pct = 0.0  # 2026-07-26: /api/status 进度单调不回退 guard

# ====== 公式体检队列(2026-07-20, 计划书 docs/plan/2026-07-20_公式体检页面_计划书.md) ======
# 严格串行; 与 /api/run 不对称互斥: 体检永远排队, 回测提交在体检运行中 → 409
from core.lab_runner import LabQueue  # noqa: E402

lab_status = LabQueue(pipeline_busy=lambda: pipeline_status.running)

# C4c: 抽出的路由模块 (路由注册语义不变, 路径/方法逐个平移)
app.include_router(create_lab_router(lab_status, pipeline_status))
app.include_router(research_router)
from data_cache_api import router as data_cache_router  # 2026-08-14: 数据准备 TAB
app.include_router(data_cache_router)


# ====== 配置端点 ======

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
    """校验策略配置（前端未调用，save 端点自带 validate；保留供外部脚本使用）。"""
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
    from core.lab_runner import formula_name_ok
    if not formula_name_ok(formula or ""):
        return JSONResponse(status_code=400, content={"success": False, "error": "公式名含非法字符"})
    path = _PROJECT_ROOT / "output" / "reports" / f"{formula}_filter_rules.json"
    if not path.exists():
        return {"success": True, "exists": False, "rules": [],
                "hint": f"{formula} 未体检, 先跑: python tools/formula_lab.py --formula {formula} --tag <短窗> --tag2 <长窗>"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {"success": True, "exists": True, **data}
    except Exception as e:
        return {"success": False, "exists": False, "error": f"规则文件解析失败: {e}"}


# ====== 管线端点 ======

@app.get("/api/status")
async def get_status():
    """获取管线运行状态。

    2026-07-26: 融合 core.progress 细粒度进度 (选股批次/ST过滤/取数/loop)。
    粗点位 (_cb) 与细粒度取 max, 单调不回退 (formula_sell 嵌套调用可能乱序上报);
    detail/eta_s 为 additive 字段, 旧前端不读不受影响。
    """
    global _last_served_pct
    prog = pipeline_status.progress
    step = pipeline_status.step
    detail = ""
    eta = -1.0
    if pipeline_status.running:
        from core import progress as _progress
        snap = _progress.snapshot()
        if snap["ts"] > 0:
            prog = max(prog, snap["pct"])
            detail = snap["detail"]
            eta = snap["eta_s"]
            # 细粒度阶段名替换粗粒度 step (如 "准备选股参数" → "公式选股")
            step = _progress.STAGE_NAMES.get(snap["stage"], step)
    prog = max(prog, _last_served_pct)
    _last_served_pct = prog
    return {
        "running": pipeline_status.running,
        "progress": prog,
        "step": step,
        "detail": detail,
        "eta_s": round(eta, 1),
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
        # 2026-07-26: 细粒度进度状态一并重置 (防上次回测的阶段/ETA 残留)
        from core import progress as _progress
        _progress.reset()
        global _last_served_pct
        _last_served_pct = 0.0
        pipeline_status.running = True
        pipeline_status.progress = 0
        pipeline_status.step = "初始化"
        pipeline_status.error = ""

    # 输入校验（保留）
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
    import os as _os
    import tempfile
    config_dict = _config_to_yaml_dict(cfg)
    # 2026-08-06 HIGH#5: validate 下沉到 /api/run (原仅 save/validate 端点调).
    # 不阻塞回测, 仅 warning 入日志, 便于直调 API/yaml 路径暴露 ladder 比例错配。
    _stop_warnings = ConfigLoader.validate_stop_config(config_dict)
    if _stop_warnings:
        logger.warning("止损止盈配置告警 (不阻塞): %s", _stop_warnings)
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
        return _read_json(persist_path)
    return {"success": False, "error": "暂无历史回测结果"}


@app.get("/api/results")
async def list_results():
    """获取历史回测列表。"""
    index_path = _PROJECT_ROOT / "output" / "results" / "index.json"
    if index_path.exists():
        return _read_json(index_path)
    return []


@app.get("/api/results/{result_id}")
async def get_result(result_id: str):
    """加载指定历史回测结果。P2-2: result_id 正则校验防路径遍历。"""
    if not re.match(r'^\d{8}_\d{6}$', result_id):
        return JSONResponse(status_code=400, content={"success": False, "error": "result_id 格式错误"})
    result_path = _PROJECT_ROOT / "output" / "results" / f"{result_id}.json"
    if result_path.exists():
        return _read_json(result_path)
    return {"success": False, "error": "结果不存在"}


# ====== 主页面 ======

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = _PROJECT_ROOT / "web" / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>VERA Web 前端未找到，请创建 web/index.html</h1>"


@app.get("/m", response_class=HTMLResponse)
async def mobile():
    """移动版入口 (2026-08-18): 手机局域网访问 http://<lan-ip>:8080/m。"""
    html_path = _PROJECT_ROOT / "web" / "mobile.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>VERA 移动版未找到，请创建 web/mobile.html</h1>"

@app.get("/favicon.ico")
async def favicon():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/web/favicon.svg")


# ====== 分析 Tab 端点 ======

@app.get("/api/calendar")
async def api_calendar(year: int = 0, month: int = 0):
    """交易日历 (TDX 数据源, 降级本地 JSON)。"""
    from datetime import date as _date
    now = _date.today()
    y = year if year > 0 else now.year
    m = month if 1 <= month <= 12 else now.month
    # 当月天数
    import calendar as _cal
    days_in_month = _cal.monthrange(y, m)[1]
    # YYYYMMDD 格式 (匹配 TDX 全仓约定)
    start_ym = f"{y}{m:02d}01"
    end_ym = f"{y}{m:02d}{days_in_month}"
    # 尝试 TDX (带 5s 超时)
    trading_dates: set = set()
    source = "tdx"
    try:
        from core.data_fetcher import DataFetcher
        dates_list = DataFetcher.get_trading_dates(
            "SH", start_time=start_ym, end_time=end_ym)
        # TDX 返回 YYYYMMDD, 归一化到 YYYY-MM-DD
        trading_dates = set()
        for d in dates_list:
            ds = str(d)
            if len(ds) == 8:
                trading_dates.add(f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}")
            elif len(ds) == 10:
                trading_dates.add(ds.replace("-", "")[0:4] + "-" + ds.replace("-", "")[4:6] + "-" + ds.replace("-", "")[6:8])
    except Exception:
        # 降级: 本地 JSON
        source = "local_json"
        cal_path = _PROJECT_ROOT / "data" / "trading_calendar.json"
        if cal_path.exists():
            try:
                cal_data = json.loads(cal_path.read_text("utf-8"))
                dates = cal_data.get("dates", [])
                # 过滤当月日期
                prefix = f"{y}-{m:02d}-"
                trading_dates = {d for d in dates if d.startswith(prefix)}
            except Exception:
                trading_dates = set()
    # 构造当月全部日期
    result = {}
    for d in range(1, days_in_month + 1):
        date_str = f"{y}-{m:02d}-{d:02d}"
        dt = _date(y, m, d)
        is_trading = date_str in trading_dates
        result[date_str] = {
            "is_trading": is_trading,
            "weekday": dt.weekday(),  # 0=Mon
        }
    return {"year": y, "month": m, "trading_calendar": result, "data_source": source}


@app.get("/api/benchmark/history")
async def api_benchmark_history(
    indices: str = "shanghai,hs300,chuangyeban,kechuang50,zhongzhengA500",
    start: str = "", end: str = "",
):
    """拉取基准指数日线 (分析 Tab 权益曲线基准对比)。
    indices: 逗号分隔的指数名; start/end: YYYY-MM-DD。"""
    from core.data_fetcher import DataFetcher
    # 2026-08-08 修复: 前端传 YYYY-MM-DD, get_kline 只认 YYYYMMDD,
    # 此前直接抛 ValueError 被静默吞掉 → 基准恒空 (权益曲线无对比线)
    start = start.replace("-", "")
    end = end.replace("-", "")
    index_names = [n.strip() for n in indices.split(",") if n.strip()]
    result: dict = {}
    for name in index_names:
        code = DataFetcher.INDEX_CODES.get(name)
        if not code:
            continue
        try:
            kline = DataFetcher.get_kline([code], start_time=start,
                                          end_time=end, period="1d",
                                          dividend_type="none")
            if kline is None or "Close" not in kline:
                result[name] = []
                continue
            # get_kline returns field-major: {"Close": DataFrame with code columns, DatetimeIndex}
            close_df = kline["Close"]
            if close_df is None or close_df.empty or code not in close_df.columns:
                result[name] = []
                continue
            series = close_df[code].dropna()
            records = []
            for idx_val, val in series.items():
                d = str(idx_val)[:10]
                records.append({"date": d, "close": round(float(val), 2)})
            result[name] = records
        except Exception:
            result[name] = []
    return result


@app.get("/api/stock/kline")
def api_stock_kline(
    code: str = Query(..., pattern=r"(?i)^\d{6}(\.(SH|SZ|BJ))?$"),
    start: str = Query("", pattern=r"^(\d{8}|\d{4}-\d{2}-\d{2})?$"),
    end: str = Query("", pattern=r"^(\d{8}|\d{4}-\d{2}-\d{2})?$"),
):
    """单笔交易 K 线回放日线 (图表分析深挖包 Phase 3, 2026-08-14)。

    薄 adapter: 校验 → 归一 → get_kline → field-major 转 rows → 异常映射。
    - code 正则白名单 (6位数字+可选 SH/SZ/BJ 后缀) 防注入 TDX 查询, 不匹配 → 422。
    - start/end 同时接受 YYYYMMDD 和 YYYY-MM-DD, 归一成 YYYYMMDD 再调数据层
      (照抄 /api/benchmark/history 2026-08-08 修复教训: 未归一直接抛错被静默吞)。
    - 复权口径 dividend_type="front" (前复权): 与回测引擎一致
      (backtest/engine.py:213 硬编码 "front", engine.run docstring 注明与
      pipeline.assert_consistent 对齐), 保证买卖点 marker 和 K 线价格对得上;
      benchmark 端点用 "none" 是指数口径, 不适用于个股回放。
    - 任何字段 NaN/inf 的行整行丢弃 (FastAPI allow_nan=False, 漏一个就 500)。
    - 无数据/缺列 → 200 空 rows; 数据层异常 → 502 {"detail": ...}。
    """
    import math
    from core.data_fetcher import DataFetcher
    from utils.code_normalizer import normalize as _normalize_code

    code_in = code.strip().upper()
    tdx_code = _normalize_code(code_in) or code_in  # 补默认后缀, 如 600000 → 600000.SH
    start = start.replace("-", "")
    end = end.replace("-", "")
    try:
        kline = DataFetcher.get_kline(
            [tdx_code], start_time=start, end_time=end,
            period="1d", dividend_type="front")
    except Exception as e:
        logger.error(f"/api/stock/kline 数据层异常 ({tdx_code}): {e}")
        raise HTTPException(status_code=502, detail=f"K线数据获取失败: {e}")

    rows = []
    close_df = (kline or {}).get("Close")
    if close_df is not None and not close_df.empty and tdx_code in close_df.columns:
        series = {}
        for f in ("Open", "High", "Low", "Close", "Volume"):
            df = kline.get(f)
            series[f] = df[tdx_code] if (df is not None and tdx_code in df.columns) else None
        for idx in sorted(close_df.index):
            vals = []
            for f in ("Open", "High", "Low", "Close", "Volume"):
                s = series[f]
                v = s.get(idx) if s is not None else None
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    v = float("nan")
                vals.append(v)
            if not all(math.isfinite(v) for v in vals):
                continue  # 含 NaN/inf/缺失的行整行丢弃
            rows.append({
                "date": idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10],
                "open": vals[0], "high": vals[1], "low": vals[2],
                "close": vals[3], "volume": vals[4],
            })
    return {"code": code_in, "period": "1d", "rows": rows}


# ====== 启动 ======

if __name__ == "__main__":
    import argparse

    import uvicorn
    parser = argparse.ArgumentParser(description="VERA Web 服务器")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", type=str, default="0.0.0.0")  # 2026-08-18: 局域网手机访问
    args = parser.parse_args()

    logger.info("VERA 量化回测系统 Web 服务器启动")
    logger.info(f"访问: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)
