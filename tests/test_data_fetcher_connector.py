"""C5 — DataFetcher connector 注入缝隙测试。

验证 set_connector 注入的 mock 被使用, 不触达真实 TDX。
"""
from __future__ import annotations

import pandas as pd
import pytest

from core.data_fetcher import DataFetcher


class FakeTq:
    def __init__(self):
        self.calls = []

    def get_stock_list(self, *args, **kwargs):
        self.calls.append(("get_stock_list", args, kwargs))
        return [{"Code": "000001.SZ"}, {"Code": "600519.SH"}]

    def get_market_data(self, **kw):
        self.calls.append(("get_market_data", kw))
        # 返回最小合法结构
        idx = pd.bdate_range("2026-01-02", periods=3)
        return {
            "Close": pd.DataFrame({"000001.SZ": [10.0, 11.0, 12.0]}, index=idx),
            "ErrorId": "0",
        }


class FakeConnector:
    def __init__(self):
        self.connected = False
        self.tq_obj = FakeTq()

    def ensure_connected(self):
        self.connected = True

    def tq(self):
        return self.tq_obj


@pytest.fixture(autouse=True)
def _reset():
    yield
    DataFetcher.reset_connector()


def test_default_connector_is_tdx():
    """未注入时, _connector() 返回 TdxConnector（默认单例）。"""
    from core.connector import TdxConnector
    assert DataFetcher._connector() is TdxConnector


def test_injected_connector_used_by_get_stock_universe():
    """set_connector(mock) → get_stock_universe 走 mock, 不触达 TDX。"""
    mock = FakeConnector()
    DataFetcher.set_connector(mock)
    codes = DataFetcher.get_stock_universe("50")
    assert mock.connected is True  # ensure_connected 走了 mock
    assert codes == ["000001.SZ", "600519.SH"]
    assert mock.tq_obj.calls[0][0] == "get_stock_list"


def test_injected_connector_used_by_get_kline():
    """get_kline 走注入的 mock tq。"""
    mock = FakeConnector()
    DataFetcher.set_connector(mock)
    data = DataFetcher.get_kline(["000001.SZ"], start_time="20260101", end_time="20260110")
    assert "Close" in data
    assert any(c[0] == "get_market_data" for c in mock.tq_obj.calls)


def test_reset_connector_restores_default():
    from core.connector import TdxConnector
    mock = FakeConnector()
    DataFetcher.set_connector(mock)
    assert DataFetcher._connector() is mock
    DataFetcher.reset_connector()
    assert DataFetcher._connector() is TdxConnector
