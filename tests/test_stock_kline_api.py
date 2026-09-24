# -*- coding: utf-8 -*-
"""个股日线端点 /api/stock/kline (TestClient) — 图表分析深挖包 Phase 3。

覆盖: rows 形状/日期 ISO/升序/OHLCV 数值、两种日期格式归一、
code 正则 422 (防注入)、None/缺列 → 空 rows、NaN/inf 行整行丢弃、
数据层异常 → 502。mock 先例: tests/test_snapshot_parity.py:450-453。
"""
import math
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from server import app  # noqa: E402

FIELDS = ("Open", "High", "Low", "Close", "Volume")


def _make_kline(rows, code="600000.SH", drop_fields=()):
    """构造 field-major dict: {'Open': DataFrame, ...}, 行索引 DatetimeIndex, 列为代码。

    rows: [(date_str, open, high, low, close, volume), ...]
    """
    dates = pd.to_datetime([r[0] for r in rows])
    out = {}
    for i, f in enumerate(FIELDS):
        if f in drop_fields:
            continue
        out[f] = pd.DataFrame({code: [r[i + 1] for r in rows]}, index=dates)
    return out


def _mock_get_kline(monkeypatch, kline=None, captured=None, raises=None):
    def _fake(*a, **kw):
        if captured is not None:
            captured["args"] = a
            captured["kwargs"] = kw
        if raises is not None:
            raise raises
        return kline
    from core.data_fetcher import DataFetcher
    monkeypatch.setattr(DataFetcher, "get_kline", _fake)


def _assert_finite(obj):
    """递归断言响应 JSON 无 NaN/inf (allow_nan=False, 漏一个就是 500)。"""
    if isinstance(obj, float):
        assert math.isfinite(obj), f"非有限值: {obj}"
    elif isinstance(obj, dict):
        for v in obj.values():
            _assert_finite(v)
    elif isinstance(obj, list):
        for v in obj:
            _assert_finite(v)


@pytest.fixture
def client():
    return TestClient(app)


GOOD_ROWS = [
    ("2024-01-02", 10.0, 10.5, 9.9, 10.2, 123456.0),
    ("2024-01-03", 10.2, 10.8, 10.1, 10.6, 234567.0),
    ("2024-01-04", 10.6, 10.7, 10.0, 10.1, 111111.0),
]


def test_kline_happy_path(client, monkeypatch):
    _mock_get_kline(monkeypatch, kline=_make_kline(GOOD_ROWS))
    r = client.get("/api/stock/kline",
                   params={"code": "600000", "start": "2024-01-01", "end": "2024-03-01"})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == "600000"
    assert body["period"] == "1d"
    rows = body["rows"]
    assert len(rows) == 3
    # 日期 ISO 格式 + 升序
    dates = [row["date"] for row in rows]
    assert dates == ["2024-01-02", "2024-01-03", "2024-01-04"]
    # OHLCV 数值逐字段对得上
    first = rows[0]
    assert first["open"] == 10.0 and first["high"] == 10.5
    assert first["low"] == 9.9 and first["close"] == 10.2
    assert first["volume"] == 123456.0
    _assert_finite(body)


def test_kline_both_date_formats_and_normalization(client, monkeypatch):
    """YYYY-MM-DD 与 YYYYMMDD 都接受, 内部归一成 YYYYMMDD 再调数据层。"""
    for start, end in [("2024-01-01", "2024-03-01"), ("20240101", "20240301")]:
        captured = {}
        _mock_get_kline(monkeypatch, kline=_make_kline(GOOD_ROWS), captured=captured)
        r = client.get("/api/stock/kline",
                       params={"code": "600000", "start": start, "end": end})
        assert r.status_code == 200
        assert captured["kwargs"]["start_time"] == "20240101"
        assert captured["kwargs"]["end_time"] == "20240301"
        assert len(r.json()["rows"]) == 3


def test_kline_suffix_lowercase_normalized(client, monkeypatch):
    """code 带小写后缀 → 归一成大写; 调数据层用标准 XXXXXX.SH 格式。"""
    captured = {}
    _mock_get_kline(monkeypatch, kline=_make_kline(GOOD_ROWS), captured=captured)
    r = client.get("/api/stock/kline", params={"code": "600000.sh"})
    assert r.status_code == 200
    assert r.json()["code"] == "600000.SH"
    assert captured["args"][0] == ["600000.SH"]


def test_kline_dividend_type_matches_engine(client, monkeypatch):
    """复权口径必须 = 回测引擎的 "front" (前复权), 否则买卖点 marker 对不上 K 线价。"""
    captured = {}
    _mock_get_kline(monkeypatch, kline=_make_kline(GOOD_ROWS), captured=captured)
    r = client.get("/api/stock/kline", params={"code": "600000"})
    assert r.status_code == 200
    assert captured["kwargs"]["dividend_type"] == "front"
    assert captured["kwargs"]["period"] == "1d"


@pytest.mark.parametrize("bad_code", [
    "60000",            # 5 位
    "6000000",          # 7 位
    "60000A",           # 含字母
    "600000.XX",        # 非法后缀
    "'; DROP TABLE--",  # 注入串
    "600000 OR 1=1",    # 注入串
])
def test_kline_bad_code_422(client, monkeypatch, bad_code):
    _mock_get_kline(monkeypatch, kline=_make_kline(GOOD_ROWS))
    r = client.get("/api/stock/kline", params={"code": bad_code})
    assert r.status_code == 422


def test_kline_none_returns_empty_rows(client, monkeypatch):
    _mock_get_kline(monkeypatch, kline=None)
    r = client.get("/api/stock/kline", params={"code": "600000"})
    assert r.status_code == 200
    assert r.json() == {"code": "600000", "period": "1d", "rows": []}


def test_kline_missing_field_returns_empty_rows(client, monkeypatch):
    """缺 Close 列 → 空 rows, 不崩。"""
    _mock_get_kline(monkeypatch, kline=_make_kline(GOOD_ROWS, drop_fields=("Close",)))
    r = client.get("/api/stock/kline", params={"code": "600000"})
    assert r.status_code == 200
    assert r.json()["rows"] == []


def test_kline_unknown_code_column_returns_empty_rows(client, monkeypatch):
    """返回的 DataFrame 里没有该票列 → 空 rows。"""
    _mock_get_kline(monkeypatch, kline=_make_kline(GOOD_ROWS, code="000001.SZ"))
    r = client.get("/api/stock/kline", params={"code": "600000"})
    assert r.status_code == 200
    assert r.json()["rows"] == []


def test_kline_nan_and_inf_rows_dropped(client, monkeypatch):
    """含 NaN/inf 的行整行丢弃, 响应递归无 NaN/inf。"""
    rows = GOOD_ROWS + [
        ("2024-01-05", 10.1, 10.3, 10.0, 10.2, float("nan")),   # volume NaN
        ("2024-01-08", 10.2, float("inf"), 10.0, 10.1, 999.0),  # high inf
        ("2024-01-09", 10.3, 10.9, 10.2, 10.8, 888.0),          # 好行
    ]
    _mock_get_kline(monkeypatch, kline=_make_kline(rows))
    r = client.get("/api/stock/kline", params={"code": "600000"})
    assert r.status_code == 200
    body = r.json()
    assert [row["date"] for row in body["rows"]] == [
        "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-09"]
    _assert_finite(body)


def test_kline_all_nan_returns_empty_rows(client, monkeypatch):
    """全部行 NaN → 200 空 rows (不是 500)。"""
    rows = [("2024-01-02", float("nan"),) * 5 + (float("nan"),)]
    _mock_get_kline(monkeypatch, kline=_make_kline(rows))
    r = client.get("/api/stock/kline", params={"code": "600000"})
    assert r.status_code == 200
    assert r.json()["rows"] == []


def test_kline_fetch_exception_returns_502(client, monkeypatch):
    _mock_get_kline(monkeypatch, raises=ConnectionError("TDX 连接失败"))
    r = client.get("/api/stock/kline", params={"code": "600000"})
    assert r.status_code == 502
    assert "detail" in r.json()
