"""core/kline_view 变形纯函数测试 (2026-09-15 深模块治理新增)。

锁住: 无数据 → 空列表; close_records 日期升序 + 两位小数 + dropna;
ohlcv_rows 含 NaN/缺失字段的行整行丢弃 (FastAPI allow_nan=False 防线)。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.kline_view import close_records, ohlcv_rows


def _kline(close_vals, with_ohlv=True):
    idx = pd.to_datetime(["2026-09-10", "2026-09-11", "2026-09-14"])
    df = pd.DataFrame({"600000.SH": close_vals}, index=idx)
    k = {"Close": df}
    if with_ohlv:
        for f in ("Open", "High", "Low", "Volume"):
            k[f] = pd.DataFrame({"600000.SH": close_vals}, index=idx)
    return k


def test_empty_inputs():
    assert close_records(None, "600000.SH") == []
    assert close_records({}, "600000.SH") == []
    assert ohlcv_rows({"Close": pd.DataFrame()}, "600000.SH") == []


def test_close_records_dropna_and_round():
    k = _kline([10.1234, np.nan, 10.9876])
    recs = close_records(k, "600000.SH")
    assert recs == [{"date": "2026-09-10", "close": 10.12},
                    {"date": "2026-09-14", "close": 10.99}]


def test_ohlcv_rows_drops_nan_rows():
    k = _kline([1.0, np.nan, 3.0])
    rows = ohlcv_rows(k, "600000.SH")
    assert [r["date"] for r in rows] == ["2026-09-10", "2026-09-14"]
    assert rows[0]["open"] == 1.0 and rows[0]["volume"] == 1.0


def test_ohlcv_rows_missing_field_drops_row():
    k = _kline([1.0, 2.0, 3.0], with_ohlv=False)  # 只有 Close, 其余字段缺
    assert ohlcv_rows(k, "600000.SH") == []
