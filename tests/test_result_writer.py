"""ResultWriter + PipelineResult 单元测试 (候选D C1)。

锁 ResultWriter 的契约 (审计 HIGH-2/MED-2 backward compat 硬约束):
- PipelineResult: 4 字段 frozen dataclass
- serialize: PipelineResult → response_data (前端依赖字段集 + NaN 安全 + stock_name 回填)
- persist: 三文件落地 (results/{ts}.json + index.json + last_result.json)
- on_progress: status_sink 回调适配 (异常被吞, 不中断管线)
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.result_writer import PipelineResult, ResultWriter


# ---------- PipelineResult ----------

def test_pipeline_result_has_four_fields():
    """PipelineResult 必须有 selections/backtest/benchmark/reports 四字段."""
    pr = PipelineResult(selections=pd.DataFrame(), backtest={}, benchmark={}, reports={})
    assert hasattr(pr, "selections")
    assert hasattr(pr, "backtest")
    assert hasattr(pr, "benchmark")
    assert hasattr(pr, "reports")


def test_pipeline_result_is_frozen():
    """PipelineResult frozen=True (不可变, 符合 coding-style 铁律)."""
    pr = PipelineResult(selections=None, backtest={}, benchmark={}, reports={})
    with pytest.raises(Exception):
        pr.backtest = {"x": 1}


# ---------- serialize ----------

def _make_result():
    """构造一个最小可序列化的 PipelineResult."""
    backtest = {
        "metrics": {"cumulative_return": 0.12, "sharpe": np.float64(1.5)},
        "trades": pd.DataFrame([{"stock_code": "000001", "entry_date": "2024-01-01", "exit_reason": "成本止损"}]),
        "equity_curve": pd.DataFrame([{"date": "2024-01-01", "equity": 1.0e6, "drawdown": 0.0}]),
        "stop_config_summary": "成本止损: -5%",
    }
    benchmark = {"沪深300": pd.DataFrame([
        {"date": "2024-01-01", "strategy_equity": 1.0, "index_close": 3000, "excess_return": 0.0}
    ])}
    reports = {"html": "/tmp/report.html", "json": "/tmp/metrics.json"}
    return PipelineResult(selections=pd.DataFrame(), backtest=backtest, benchmark=benchmark, reports=reports)


def test_serialize_emits_full_field_set():
    """serialize 输出必须含前端依赖的全部字段 (backward compat 硬约束)."""
    writer = ResultWriter()
    resp = writer.serialize(_make_result())
    required = {"success", "metrics", "equity", "trades", "benchmarks",
                "stop_config_summary", "report_url", "trade_count",
                "engine_version", "entry_price_basis"}
    missing = required - set(resp.keys())
    assert not missing, f"缺字段: {missing}"
    assert resp["success"] is True
    assert resp["report_url"] == "/tmp/report.html"
    assert resp["engine_version"] == "signal-day-close"


def test_serialize_nan_becomes_none():
    """metrics 的 NaN/Inf 必须转 None (JSON 安全, 复刻 server safe_serialize)."""
    writer = ResultWriter()
    backtest = {"metrics": {"bad": float("nan"), "inf": float("inf"), "ok": 0.5},
                "trades": pd.DataFrame(), "equity_curve": pd.DataFrame(),
                "stop_config_summary": ""}
    pr = PipelineResult(selections=None, backtest=backtest, benchmark={}, reports={})
    resp = writer.serialize(pr)
    assert resp["metrics"]["bad"] is None
    assert resp["metrics"]["inf"] is None
    assert resp["metrics"]["ok"] == 0.5


def test_serialize_trades_get_stock_name(monkeypatch):
    """trades 每行必须有 stock_name (查简称表回填, 查不到回退空串)."""
    import core.data_fetcher as dfm
    monkeypatch.setattr(dfm.DataFetcher, "get_name_map", lambda: {"000001": "平安银行"})
    writer = ResultWriter()
    resp = writer.serialize(_make_result())
    assert len(resp["trades"]) == 1
    assert resp["trades"][0]["stock_name"] == "平安银行"


# ---------- persist ----------

def test_persist_writes_three_files(tmp_path):
    """persist 必须落 results/{ts}.json + index.json + last_result.json."""
    writer = ResultWriter()
    resp = {"success": True, "trade_count": 1, "metrics": {"cumulative_return": 0.1}}
    results_dir = tmp_path / "results"
    last_path = tmp_path / "last_result.json"
    writer.persist(resp, results_dir=results_dir, last_result_path=last_path,
                   meta_extras={"formula": "TEST", "date_range": "20240101~20240601"})
    # 三文件
    ts_files = list(results_dir.glob("*.json"))
    ts_files = [f for f in ts_files if f.name != "index.json"]
    assert len(ts_files) == 1, f"应有 1 个 {{ts}}.json, 实际 {ts_files}"
    assert (results_dir / "index.json").exists()
    assert last_path.exists()
    # last_result 内容 = resp
    assert json.loads(last_path.read_text(encoding="utf-8"))["success"] is True
    # {ts}.json 是 {meta, data} 结构
    blob = json.loads(ts_files[0].read_text(encoding="utf-8"))
    assert "meta" in blob and "data" in blob
    assert blob["meta"]["formula"] == "TEST"


def test_persist_swallows_failure(tmp_path):
    """落盘失败必须被吞 (不抛, 与 server 现状一致)."""
    writer = ResultWriter()
    # last_result_path 指向一个不存在的盘符路径, 触发写入失败
    bad_path = Path("Z:/no_such_drive_xyz/last.json")
    # 不应抛
    writer.persist({"success": True}, results_dir=tmp_path / "r",
                   last_result_path=bad_path, meta_extras={})


# ---------- on_progress ----------

def test_on_progress_calls_sink():
    """on_progress(pct, step) 把 (step, pct) 转发给 status_sink."""
    sink = MagicMock()
    writer = ResultWriter(status_sink=sink)
    writer.on_progress(30, "执行回测")
    sink.assert_called_once_with("执行回测", 30)


def test_on_progress_swallows_sink_exception():
    """status_sink 抛异常时 on_progress 必须吞掉 (不中断管线)."""
    def bad_sink(step, pct):
        raise RuntimeError("sink 炸了")
    writer = ResultWriter(status_sink=bad_sink)
    writer.on_progress(50, "回测")  # 不应抛
