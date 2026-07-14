"""typed 结果写入器 (候选D C1) — 把 server.py 手写的 safe_serialize + 落盘抽成独立模块。

职责:
- PipelineResult: Pipeline.run 的 typed 返回 (frozen dataclass, 取代原 plain dict)
- ResultWriter: PipelineResult → 前端响应序列化 + 三文件落盘 + 进度回调适配

设计约束 (审计 HIGH-2/MED-2/隐患3):
- serialize 输出字段集 = server.py 现状 (backward compat 硬约束, web/vera-ui.js 依赖)
- response_data 同时用于 /api/run 返回 + last_result.json + {ts}.json 的 data (三共用, 改形状三处同断)
- stock_name 回填 (查简称表) 必须保留 (server.py:459-466 现有逻辑, Pipeline.run 不做)
- status_sink 用 callable 注入避免循环 import (server import ResultWriter, ResultWriter 延迟 import server)

命名冲突警告: 本模块的 ResultWriter 与 selection/result_writer.py:ResultWriter 同名但不同物
(本模块管"前端响应+落盘", selection 那个管"选股结果写 CSV")。import 时注意来源。
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

# F2 回归保护: 标记买入语义 (signal-day-close = 信号日收盘价买入, 业务铁律2)
ENGINE_VERSION = "signal-day-close"
ENTRY_PRICE_BASIS = "close_on_signal_day"


@dataclass(frozen=True)
class PipelineResult:
    """Pipeline.run 的 typed 返回值 (取代原 plain dict)."""
    selections: Optional[pd.DataFrame]
    backtest: dict          # {"metrics","trades","equity_curve","stop_config_summary",...}
    benchmark: dict
    reports: dict           # {"html","json"}


def safe_serialize(obj):
    """NaN/Inf/ndarray/Timestamp → JSON 安全值 (复刻自 server.py:421-437)."""
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
                pass  # 回调异常不中断管线 (复刻 pipeline._cb 语义)
        else:
            try:
                from server import pipeline_status
                pipeline_status.step = step
                pipeline_status.progress = pct
            except Exception:
                pass

    def on_progress(self, pct: int, step: str) -> None:
        """Pipeline.run progress_callback adapter.

        签名 (pct, step) 与 pipeline._cb(pct, name) 一致; 内部转 sink(step, pct)。
        """
        self._sink(step, pct)

    def serialize(self, result: PipelineResult) -> dict:
        """PipelineResult → 前端响应 dict (字段集 = server 现状, backward compat 硬约束)."""
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

        return {
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
            if isinstance(response, dict):
                response.setdefault("engine_version", ENGINE_VERSION)
                response.setdefault("entry_price_basis", ENTRY_PRICE_BASIS)
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
            pass  # 落盘失败不阻塞响应 (复刻 server.py:530-531)
