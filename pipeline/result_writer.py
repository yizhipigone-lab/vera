"""typed 结果写入器 (候选D C1) — 把 server.py 手写的 safe_serialize + 落盘抽成独立模块。

职责:
- PipelineResult: Pipeline.run 的 typed 返回 (frozen dataclass, 取代原 plain dict)
- ResultWriter: PipelineResult → 前端响应序列化 + 三文件落盘 + 进度回调适配

设计约束 (审计 HIGH-2/MED-2/隐患3):
- serialize 输出字段集 = server.py 现状 (backward compat 硬约束, web/vera-ui.js 依赖)
- response_data 同时用于 /api/run 返回 + last_result.json + {ts}.json 的 data (三共用, 改形状三处同断)
- stock_name 回填 (查简称表) 必须保留 (server.py:459-466 现有逻辑, Pipeline.run 不做)
- status_sink 用 callable 注入避免循环 import (server import ResultWriter, ResultWriter 延迟 import server)

本模块管"前端响应+落盘"（原 selection/result_writer.py 已于候选 D 清理时删除）。
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

from backtest.result import BacktestResult

# P1-3 (2026-07-15): 模块级 logger, 替代 4 处内联 `import logging` + `logging.getLogger(__name__)`
from utils.logger import get_logger

logger = get_logger(__name__)

# F2 回归保护: 标记买入语义 (signal-day-close = 信号日收盘价买入, 业务铁律2)
ENGINE_VERSION = "signal-day-close"
ENTRY_PRICE_BASIS = "close_on_signal_day"


@dataclass(frozen=True)
class PipelineResult:
    """Pipeline.run 的 typed 返回值 (取代原 plain dict).

    C1-2: 加 dict-like 访问方法，兼容 main.py 的 result["backtest"] / result.get(...) 写法。
    """
    selections: Optional[pd.DataFrame]
    backtest: Optional[BacktestResult]   # C3: engine.run() 返 BacktestResult (dict-like, 兼容老 dict 调用)
    benchmark: dict
    reports: dict           # {"html","json"}
    error: Optional[str] = None   # P0-1 (2026-07-15): 失败路径也返 PipelineResult, error 字段携带错误信息

    # C1-2 dict-like 访问: result["backtest"] / result.get(...)
    # P0-1 修订: "error" 进 _FIELDS — 失败路径需要 main.py:52 的 "error" in result 命中
    _FIELDS = ("selections", "backtest", "benchmark", "reports", "error")

    def __getitem__(self, key):
        if key in self._FIELDS:
            return getattr(self, key)
        raise KeyError(key)

    def __contains__(self, key):
        """支持 'key' in result 写法 (main.py:52 兼容)."""
        return key in self._FIELDS

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


def safe_serialize(obj):
    """递归 NaN/Inf/ndarray/Timestamp → JSON 安全值 (T-H-1, 2026-07-15).

    升级为递归版本: dict/list/tuple/set/frozenset 内嵌套的 NaN/Inf 也会被转为 None。
    不可变: 所有容器返回新对象，不修改入参。
    """
    # 1. np.integer / np.bool_ → int (numpy 2.x 中 np.bool_ 非 np.integer 子类, 需显式捕获)
    if isinstance(obj, (np.integer, np.bool_)):
        return int(obj)
    # 2. np.floating → float + NaN/Inf→None (必须在 float 前:
    #    np.float64 同时匹配 isinstance(obj, float)，若 float 在前会把 np.float64 原样返回)
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        if np.isnan(v) or np.isinf(v):
            return None
        return v
    # 3. Python float → NaN/Inf→None
    if isinstance(obj, float):
        if np.isnan(obj) or np.isinf(obj):
            return None
        return obj
    # 4. np.ndarray → .tolist() (必须在 list 前: 防对 ndarray 逐元素递归)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    # 5. pd.Timestamp → isoformat
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    # 6. dict → 递归 sanitize 每个 value
    if isinstance(obj, dict):
        return {k: safe_serialize(v) for k, v in obj.items()}
    # 7. list → 递归 sanitize 每个元素
    if isinstance(obj, list):
        return [safe_serialize(v) for v in obj]
    # 8. tuple → 转为 list 并递归 (JSON 无 tuple)
    if isinstance(obj, tuple):
        return [safe_serialize(v) for v in obj]
    # 9. set / frozenset → 转为 list 并递归 (JSON 无 set)
    if isinstance(obj, (set, frozenset)):
        return [safe_serialize(v) for v in obj]
    # 10. pd.NaT → None (NaTType 非 Timestamp 子类，用 is 身份判断)
    if obj is pd.NaT:
        return None
    # 11. catch-all: int / str / bool / None / 未知对象 → 原样返回
    return obj


class ResultWriter:
    """进度回调 adapter + 序列化 + 落盘 (从 server.py:419-531 抽出)."""

    def __init__(self, status_sink: Optional[Callable[[str, int], None]] = None):
        # status_sink 默认延迟写 server.pipeline_status 单例 (防循环 import)
        self._status_sink = status_sink

    def _sink(self, step: str, pct: int) -> None:
        if self._status_sink is not None:
            try:
                self._status_sink(step, pct)
            except Exception:
                # 回调异常不中断管线 (复刻 pipeline._cb 语义)
                # P1-3 (2026-07-15): 用模块级 logger, 替代原内联 import logging
                logger.warning("status_sink 回调异常", exc_info=True)
        else:
            # P2-3 (2026-07-15): python server.py 时模块名是 __main__ 非 server,
            # from server import pipeline_status 会重新导入创建克隆体导致进度丢失.
            # 优先走 __main__ 兜底.
            try:
                import sys
                main = sys.modules.get("__main__")
                ps = getattr(main, "pipeline_status", None) if main is not None else None
                if ps is None:
                    from server import pipeline_status as ps
                ps.step = step
                ps.progress = pct
            except Exception:
                # P1-3 (2026-07-15): 用模块级 logger
                logger.warning("pipeline_status 写入失败", exc_info=True)

    def on_progress(self, pct: int, step: str) -> None:
        """Pipeline.run progress_callback adapter.

        签名 (pct, step) 与 pipeline._cb(pct, name) 一致; 内部转 sink(step, pct)。
        """
        self._sink(step, pct)

    def serialize(self, result: PipelineResult) -> dict:
        """PipelineResult → 前端响应 dict (字段集 = server 现状, backward compat 硬约束).

        注意: PipelineResult 是 frozen dataclass，外部无法替换字段，但 backtest 等内部
        dict/DataFrame 仍可原地修改（frozen 只阻止 `obj.field = x`，不阻止
        `obj.backtest["key"] = val`）。调用方负责不在传入后修改 result 内部状态。"""
        backtest = result.backtest or {}
        metrics = backtest.get("metrics", {}) or {}
        trades = backtest.get("trades", pd.DataFrame())
        equity_curve = backtest.get("equity_curve", pd.DataFrame())

        metrics_clean = {k: safe_serialize(v) for k, v in metrics.items()}

        # equity 序列化 (server.py:441-448)
        equity_data = []
        if not equity_curve.empty:
            for _, row in equity_curve.iterrows():
                equity_data.append({
                    "date": str(row.get("date", "")),
                    "equity": safe_serialize(row.get("equity", 0)),
                    "drawdown": safe_serialize(row.get("drawdown", 0)),
                })

        # trades 序列化 + stock_name 回填 (server.py:450-466)
        trades_data = []
        if not trades.empty:
            trades_data = trades.to_dict(orient="records")
            for d in trades_data:
                for k, v in list(d.items()):
                    d[k] = safe_serialize(v)
            try:
                from core.data_fetcher import DataFetcher
                name_map = DataFetcher.get_name_map()
            except Exception:
                # P1-3 (2026-07-15): 用模块级 logger
                logger.warning("DataFetcher.get_name_map 失败, stock_name 将为空", exc_info=True)
                name_map = {}
            for d in trades_data:
                code = d.get("stock_code", "")
                d["stock_name"] = name_map.get(code, "")

        # benchmark 序列化 (server.py:468-480)
        benchmark_data = {}
        for name, bm_df in (result.benchmark or {}).items():
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

        resp = {
            "success": True,
            "metrics": metrics_clean,
            "equity": equity_data,
            "trades": trades_data,
            "benchmarks": benchmark_data,
            "stop_config_summary": backtest.get("stop_config_summary", ""),
            "report_url": (result.reports or {}).get("html", ""),
            "trade_count": len(trades),
            # F2 回归保护 (server.py:508-514)
            "engine_version": ENGINE_VERSION,
            "entry_price_basis": ENTRY_PRICE_BASIS,
        }
        # 2026-07-18 (计划书 §4.7 LOW-2): 5m 降级报告有才加 key, 无则响应形状不变
        degradation = backtest.get("degradation")
        if degradation is not None:
            resp["degradation"] = safe_serialize(degradation)
        return resp

    def persist(self, response: dict, *, results_dir: Path, last_result_path: Path,
                meta_extras: Optional[dict] = None) -> None:
        """落盘三文件 (复刻 server.py:496-529). 失败被吞 (与 server 现状一致)."""
        try:
            results_dir = Path(results_dir)
            results_dir.mkdir(parents=True, exist_ok=True)
            ts = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
            meta_extras = meta_extras or {}
            meta = {
                "id": ts,
                "time": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
                "trade_count": response.get("trade_count", 0),
                "cumulative_return": (response.get("metrics", {}) or {}).get("cumulative_return", 0),
                "engine_version": ENGINE_VERSION,
                "entry_price_basis": ENTRY_PRICE_BASIS,
                **meta_extras,
            }
            # data 顶层也加 engine_version/entry_price_basis (server.py:512-514)
            # 改用直接赋值(与 server.py 一致)，setdefault 在非 dict 时抛 AttributeError 被吞
            if isinstance(response, dict):
                response["engine_version"] = ENGINE_VERSION
                response["entry_price_basis"] = ENTRY_PRICE_BASIS
            # results/{ts}.json
            result_path = results_dir / f"{ts}.json"
            with open(result_path, "w", encoding="utf-8") as f:
                _json.dump({"meta": meta, "data": response}, f, ensure_ascii=False, default=str)
            # index.json (最近 50 条)
            index_path = results_dir / "index.json"
            index_data = []
            if index_path.exists():
                with open(index_path, "r", encoding="utf-8") as f:
                    index_data = _json.load(f)
            index_data.insert(0, meta)
            with open(index_path, "w", encoding="utf-8") as f:
                _json.dump(index_data[:50], f, ensure_ascii=False)
            # last_result.json
            with open(last_result_path, "w", encoding="utf-8") as f:
                _json.dump(response, f, ensure_ascii=False, default=str)
        except Exception:
            # P1-3 (2026-07-15): 用模块级 logger
            logger.warning("结果落盘失败 (不阻塞响应)", exc_info=True)
