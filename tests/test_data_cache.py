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


class TestDataCacheTTL:
    """2026-08-01 (D6): TTL 惰性过期 —— has_* 判过期即 miss (调用方回源),
    get_* 语义不变 (仍返回原值, 不返回 None)。时钟注入, 不 sleep。"""

    def _cache(self, now):
        # clock 读列表首元素, 测试里改 now[0] 即拨时间
        return DataCache(clock=lambda: now[0])

    def test_fresh_set_hits(self):
        now = [1000.0]
        c = self._cache(now)
        c.set_sector_list([{"code": "1"}])
        c.set_sector_stocks("1", ["a"])
        c.set_name_map({"a": "A"})
        now[0] += 3600          # 1h 后: 全部远未过期 → 命中
        assert c.has_sector_list()
        assert c.has_sector_stocks("1")
        assert c.has_name_map()

    def test_sector_expires_after_24h(self):
        now = [1000.0]
        c = self._cache(now)
        c.set_sector_list([{"code": "1"}])
        c.set_sector_stocks("1", ["a"])
        now[0] += 25 * 3600     # 25h > 24h TTL → miss (回源)
        assert not c.has_sector_list()
        assert not c.has_sector_stocks("1")
        # get_* 语义不变: 旧值还在, 不返回 None/空
        assert c.get_sector_list() == [{"code": "1"}]
        assert c.get_sector_stocks("1") == ["a"]

    def test_name_map_expires_after_7d(self):
        now = [1000.0]
        c = self._cache(now)
        c.set_name_map({"a": "A"})
        now[0] += 6 * 24 * 3600     # 6d < 7d → 仍命中
        assert c.has_name_map()
        now[0] += 2 * 24 * 3600     # 8d > 7d → miss
        assert not c.has_name_map()
        assert c.get_name_map() == {"a": "A"}   # get 语义不变

    def test_sector_stocks_ttl_is_per_key(self):
        now = [1000.0]
        c = self._cache(now)
        c.set_sector_stocks("old", ["a"])
        now[0] += 25 * 3600
        c.set_sector_stocks("new", ["b"])   # 过期后新写的 key
        assert not c.has_sector_stocks("old")
        assert c.has_sector_stocks("new")

    def test_refetch_after_expiry_hits_again(self):
        """过期 miss → 回源重 set → 恢复命中 (回源路径契约)。"""
        now = [1000.0]
        c = self._cache(now)
        c.set_sector_list([{"code": "1"}])
        now[0] += 25 * 3600
        assert not c.has_sector_list()
        c.set_sector_list([{"code": "2"}])  # 模拟回源重写
        assert c.has_sector_list()
        assert c.get_sector_list() == [{"code": "2"}]

    def test_clear_resets_ttl_state(self):
        now = [1000.0]
        c = self._cache(now)
        c.set_sector_stocks("1", ["a"])
        c.clear_sector()
        assert "1" not in c._sector_stocks_ts


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
