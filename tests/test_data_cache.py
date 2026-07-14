"""C6 — DataCache 抽出测试。"""
from __future__ import annotations

import pytest

from core.data_cache import DataCache


class TestDataCache:
    def test_empty_state(self):
        c = DataCache()
        assert not c.has_sector_list()
        assert not c.has_sector_stocks("x")
        assert not c.has_name_map()
        assert c.get_sector_list() == []
        assert c.get_name_map() == {}

    def test_sector_list(self):
        c = DataCache()
        c.set_sector_list([{"code": "881.SH", "name": "半导体"}])
        assert c.has_sector_list()
        assert c.get_sector_list() == [{"code": "881.SH", "name": "半导体"}]

    def test_sector_stocks(self):
        c = DataCache()
        c.set_sector_stocks("881.SH", ["000001.SZ", "600519.SH"])
        assert c.has_sector_stocks("881.SH")
        assert not c.has_sector_stocks("999.SH")
        assert c.get_sector_stocks("881.SH") == ["000001.SZ", "600519.SH"]

    def test_name_map(self):
        c = DataCache()
        c.set_name_map({"601872.SH": "招商轮船"})
        assert c.has_name_map()
        assert c.get_name_map()["601872.SH"] == "招商轮船"

    def test_clear_sector_keeps_name(self):
        c = DataCache()
        c.set_sector_list([{"code": "1"}])
        c.set_sector_stocks("1", ["a"])
        c.set_name_map({"a": "A"})
        c.clear_sector()
        assert not c.has_sector_list()
        assert not c.has_sector_stocks("1")
        assert c.has_name_map()  # name 不受影响

    def test_clear_name_keeps_sector(self):
        c = DataCache()
        c.set_sector_list([{"code": "1"}])
        c.set_name_map({"a": "A"})
        c.clear_name()
        assert c.has_sector_list()
        assert not c.has_name_map()

    def test_clear_all(self):
        c = DataCache()
        c.set_sector_list([{"code": "1"}])
        c.set_sector_stocks("1", ["a"])
        c.set_name_map({"a": "A"})
        c.clear_all()
        assert not c.has_sector_list()
        assert not c.has_sector_stocks("1")
        assert not c.has_name_map()


class TestDataFetcherCacheDelegation:
    """DataFetcher.clear_* 委托到 DataCache。"""

    @pytest.fixture(autouse=True)
    def _reset_shared_cache(self):
        """A1/T1: 每测前后 reset DataFetcher._cache, 防 assert 失败泄漏到后续测试。"""
        from core.data_fetcher import DataFetcher
        DataFetcher._cache.clear_all()
        yield
        DataFetcher._cache.clear_all()

    def test_clear_sector_clears_cache(self):
        from core.data_fetcher import DataFetcher
        DataFetcher._cache.set_sector_list([{"code": "1"}])
        DataFetcher._cache.set_sector_stocks("1", ["a"])
        DataFetcher.clear_sector_cache()
        assert not DataFetcher._cache.has_sector_list()
        assert not DataFetcher._cache.has_sector_stocks("1")

    def test_clear_name_clears_cache(self):
        from core.data_fetcher import DataFetcher
        DataFetcher._cache.set_name_map({"a": "A"})
        DataFetcher.clear_name_cache()
        assert not DataFetcher._cache.has_name_map()
