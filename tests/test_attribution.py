"""attribute_returns 纯函数 + ResultWriter.serialize 两新 key 测试 (Phase 2, 2026-08-13)。

契约 (前端钉死, 一字不改):
- by_sector: [{"name", "pnl", "pct"}] 按 pnl 降序; 未映射行业 → name="未标"
- by_stock_top: [{"code", "name", "pnl", "pct"}] 按 |pnl| 降序取前 10
- pct = pnl / 总净盈亏; 总和为 0 → pct 为 None
- serialize: equity_curve/trades 有才加 key, 空/失败不加, 绝不拖垮主结果, 输出过 safe_serialize
"""
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.attribution import attribute_returns
from pipeline.result_writer import PipelineResult, ResultWriter

# ---------- attribute_returns 纯函数 ----------

def test_sector_aggregation_and_sort():
    """同行业多票合并; by_sector 按 pnl 降序; pct = pnl/总净盈亏。"""
    items = [
        {"code": "A", "pnl": 100.0},
        {"code": "B", "pnl": -30.0},   # 与 A 同行业 → 合并 70
        {"code": "C", "pnl": 50.0},
    ]
    idx = {"A": "S1", "B": "S1", "C": "S2"}
    names = {"S1": "半导体", "S2": "银行"}
    out = attribute_returns(items, idx, sector_names=names)
    sectors = out["by_sector"]
    assert [s["name"] for s in sectors] == ["半导体", "银行"]
    assert sectors[0]["pnl"] == pytest.approx(70.0)
    assert sectors[0]["pct"] == pytest.approx(70.0 / 120.0)
    assert sectors[1]["pct"] == pytest.approx(50.0 / 120.0)


def test_unmapped_goes_to_untagged():
    """未映射行业的票归入 name='未标'。"""
    items = [{"code": "X", "pnl": 10.0}, {"code": "Y", "pnl": -5.0}]
    out = attribute_returns(items, {"X": "S1"}, sector_names={"S1": "半导体"})
    names = {s["name"]: s["pnl"] for s in out["by_sector"]}
    assert names["半导体"] == pytest.approx(10.0)
    assert names["未标"] == pytest.approx(-5.0)


def test_sector_name_falls_back_to_key():
    """sector_names 缺该行业 → name 回退为映射值本身 (代码)。"""
    out = attribute_returns([{"code": "A", "pnl": 1.0}], {"A": "881319.SH"})
    assert out["by_sector"][0]["name"] == "881319.SH"


def test_stock_top_aggregates_and_truncates():
    """同票多笔合并; 超过 10 只按 |pnl| 截断到前 10。"""
    items = [{"code": f"S{i:02d}", "pnl": float(i) * (1 if i % 2 else -1)}
             for i in range(1, 13)]
    # 同票两笔: S01 = 1 + 100 = 101
    items.append({"code": "S01", "pnl": 100.0})
    out = attribute_returns(items, {}, stock_names={"S01": "测试股"})
    top = out["by_stock_top"]
    assert len(top) == 10
    # |pnl| 降序: S01 (101) 第一
    assert top[0]["code"] == "S01"
    assert top[0]["pnl"] == pytest.approx(101.0)
    assert top[0]["name"] == "测试股"
    abs_pnls = [abs(t["pnl"]) for t in top]
    assert abs_pnls == sorted(abs_pnls, reverse=True)
    # 11 只票 (S01 合并) 截到 10: 最小 |pnl|=2 的 S02 被截掉
    assert "S02" not in {t["code"] for t in top}


def test_stock_name_defaults_empty():
    """stock_names 查不到 → name 空串 (前端兜底显示 code)。"""
    out = attribute_returns([{"code": "A", "pnl": 1.0}], {})
    assert out["by_stock_top"][0]["name"] == ""


def test_pct_none_when_total_zero():
    """总净盈亏为 0 → 所有 pct 为 None。"""
    items = [{"code": "A", "pnl": 100.0}, {"code": "B", "pnl": -100.0}]
    out = attribute_returns(items, {})
    assert all(s["pct"] is None for s in out["by_sector"])
    assert all(t["pct"] is None for t in out["by_stock_top"])


def test_empty_items_returns_empty_structure():
    """空 items → 空结构, 不抛。"""
    out = attribute_returns([], {})
    assert out == {"by_sector": [], "by_stock_top": []}


def test_bad_pnl_rows_skipped():
    """pnl 非数值/NaN 的行跳过, 不抛。"""
    items = [{"code": "A", "pnl": float("nan")},
             {"code": "B", "pnl": None},
             {"code": "C", "pnl": 5.0}]
    out = attribute_returns(items, {})
    assert len(out["by_stock_top"]) == 1
    assert out["by_stock_top"][0]["code"] == "C"


# ---------- ResultWriter.serialize 两新 key ----------

def _make_result(with_trades=True, with_equity=True):
    backtest = {"metrics": {}, "stop_config_summary": ""}
    if with_equity:
        backtest["equity_curve"] = pd.DataFrame({
            "date": [f"2024-01-0{i}" for i in range(1, 6)],
            "equity": [100.0, 101.0, 99.0, 102.0, 103.0],
            "drawdown": [0.0] * 5,
        })
    else:
        backtest["equity_curve"] = pd.DataFrame()
    if with_trades:
        backtest["trades"] = pd.DataFrame([
            {"stock_code": "600000.SH", "pnl": 1000.0},
            {"stock_code": "000001.SZ", "pnl": -500.0},
        ])
    else:
        backtest["trades"] = pd.DataFrame()
    return PipelineResult(selections=None, backtest=backtest, benchmark={}, reports={})


@pytest.fixture()
def mock_sector_stack(monkeypatch):
    """mock 行业映射三件套, 避免真拉 TDX。"""
    import core.data_fetcher as dfm
    import policy_kb.build_sector_index as bsi
    monkeypatch.setattr(bsi, "build_stock_sector_index",
                        lambda force_refresh=False: {"600000.SH": "S1"})
    monkeypatch.setattr(dfm.DataFetcher, "get_sector_list",
                        classmethod(lambda cls: [{"code": "S1", "name": "银行"}]))
    monkeypatch.setattr(dfm.DataFetcher, "get_name_map",
                        classmethod(lambda cls: {"600000.SH": "浦发银行"}))


def test_serialize_adds_rolling_metrics(mock_sector_stack):
    writer = ResultWriter(status_sink=lambda step, pct: None)
    resp = writer.serialize(_make_result())
    rm = resp["rolling_metrics"]
    assert rm["window"] == 60
    assert rm["dates"] == [f"2024-01-0{i}" for i in range(1, 6)]
    # 窗口不足全 None (不能是 NaN)
    assert all(v is None for v in rm["rolling_sharpe"])


def test_serialize_adds_attribution(mock_sector_stack):
    writer = ResultWriter(status_sink=lambda step, pct: None)
    resp = writer.serialize(_make_result())
    att = resp["attribution"]
    # 600000.SH → 银行 +1000; 000001.SZ 未映射 → 未标 -500
    sectors = {s["name"]: s for s in att["by_sector"]}
    assert sectors["银行"]["pnl"] == pytest.approx(1000.0)
    assert sectors["银行"]["pct"] == pytest.approx(1000.0 / 500.0)
    assert sectors["未标"]["pnl"] == pytest.approx(-500.0)
    top = {t["code"]: t for t in att["by_stock_top"]}
    assert top["600000.SH"]["name"] == "浦发银行"
    assert top["000001.SZ"]["name"] == ""


def test_serialize_omits_keys_when_empty(mock_sector_stack):
    """equity_curve/trades 为空 → 两 key 均不出现 (响应形状不变)。"""
    writer = ResultWriter(status_sink=lambda step, pct: None)
    resp = writer.serialize(_make_result(with_trades=False, with_equity=False))
    assert "rolling_metrics" not in resp
    assert "attribution" not in resp


def test_serialize_attribution_failure_does_not_break_response(monkeypatch):
    """build_stock_sector_index 抛异常 → attribution key 跳过, 其余字段正常。"""
    import policy_kb.build_sector_index as bsi

    def _boom(force_refresh=False):
        raise RuntimeError("TDX 挂了")

    monkeypatch.setattr(bsi, "build_stock_sector_index", _boom)
    writer = ResultWriter(status_sink=lambda step, pct: None)
    resp = writer.serialize(_make_result())
    assert "attribution" not in resp
    assert resp["success"] is True
    assert "rolling_metrics" in resp  # 独立 try/except, 互不影响


def test_serialize_new_keys_json_safe(mock_sector_stack):
    """两 key 必须过 safe_serialize: 递归断言无 NaN/inf。"""
    writer = ResultWriter(status_sink=lambda step, pct: None)
    resp = writer.serialize(_make_result())

    def _walk(v):
        if isinstance(v, dict):
            for x in v.values():
                _walk(x)
        elif isinstance(v, list):
            for x in v:
                _walk(x)
        elif isinstance(v, float):
            assert math.isfinite(v)

    _walk(resp["rolling_metrics"])
    _walk(resp["attribution"])
