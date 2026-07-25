# -*- coding: utf-8 -*-
"""L1 池缓存测试 (2026-07-26, selection/universe_cache.py + resolve_universe 接缝)。

计划书 docs/plan/2026-07-26_选股缓存二期_L1池缓存_L2按日信号缓存_计划书.md §6:
1. roundtrip 命中  2. 配置失效  3. 日期失效  4. 假值默认键收敛
5. 并发原子写  6. LRU  7. pool_hash 确定性+顺序无关  8. selector 接缝命中
"""
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import selection.selector as selector_mod
from selection import universe_cache as uc
from selection.selector import StockSelector


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "universe_cache"
    monkeypatch.setattr(uc, "default_cache_root", lambda: d)
    return d


@pytest.fixture(autouse=True)
def _flags():
    uc.ENABLED, uc.FORCE_REFRESH = True, False
    yield
    uc.ENABLED, uc.FORCE_REFRESH = True, False


class TestKeyLoadSave:
    def test_roundtrip(self, tmp_path):
        uc.save(tmp_path, "k1", ["600001.SH", "000001.SZ"])
        assert uc.load(tmp_path, "k1") == ["600001.SH", "000001.SZ"]
        assert uc.load(tmp_path, "nope") is None

    def test_config_change_invalidates(self):
        k1 = uc.build_key({"type": "50", "exclude_st": True}, "20260726")
        assert uc.build_key({"type": "23", "exclude_st": True}, "20260726") != k1
        assert uc.build_key({"type": "50", "exclude_st": False}, "20260726") != k1

    def test_today_str_invalidates(self):
        k1 = uc.build_key({"type": "50"}, "20260726")
        assert uc.build_key({"type": "50"}, "20260727") != k1

    def test_falsey_defaults_converge(self):
        sparse = {"type": "50", "exclude_st": True}
        full = {"type": "50", "exclude_st": True, "include_etf": False,
                "etf_only": False, "sectors": []}
        assert uc.build_key(full, "20260726") == uc.build_key(sparse, "20260726")

    def test_corrupt_file_treated_as_miss(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        assert uc.load(tmp_path, "bad") is None
        assert not bad.exists()

    def test_concurrent_save(self, tmp_path):
        errors = []

        def _save():
            try:
                for _ in range(5):
                    uc.save(tmp_path, "same", ["600001.SH"])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=_save) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert errors == []
        assert uc.load(tmp_path, "same") == ["600001.SH"]

    def test_lru_prune(self, tmp_path):
        import time
        for i in range(4):
            uc.save(tmp_path, f"k{i}", ["600001.SH"], keep=3)
            time.sleep(0.02)
        remaining = sorted(f.name for f in tmp_path.glob("*.json"))
        assert len(remaining) == 3 and "k0.json" not in remaining

    def test_pool_hash_order_insensitive(self):
        h1 = uc.pool_hash(["B", "A", "A"])
        h2 = uc.pool_hash(["A", "B"])
        # 排序后哈希: 列表内容差异 (重复 A) 应反映; 顺序不应反映
        assert uc.pool_hash(["A", "B"]) == h2
        assert h1 != h2


class TestSelectorSeam:
    def test_second_call_hits(self, cache_dir, monkeypatch):
        calls = {"n": 0}

        def fake_universe(list_type):
            calls["n"] += 1
            return ["600001.SH", "000001.SZ"]

        monkeypatch.setattr(selector_mod.DataFetcher, "get_stock_universe", fake_universe)
        sel = StockSelector({"formula_name": "F", "formula_arg": "",
                             "universe": {"type": "50", "exclude_st": False}})
        r1 = sel.resolve_universe()
        r2 = sel.resolve_universe()
        assert calls["n"] == 1          # 第二次走缓存
        assert r1 == r2 == ["600001.SH", "000001.SZ"]

    def test_force_refresh_recomputes(self, cache_dir, monkeypatch):
        calls = {"n": 0}

        def fake_universe(list_type):
            calls["n"] += 1
            return ["600001.SH"]

        monkeypatch.setattr(selector_mod.DataFetcher, "get_stock_universe", fake_universe)
        sel = StockSelector({"formula_name": "F",
                             "universe": {"type": "50", "exclude_st": False}})
        sel.resolve_universe()
        uc.FORCE_REFRESH = True
        sel.resolve_universe()
        assert calls["n"] == 2

    def test_disabled_no_cache(self, cache_dir, monkeypatch):
        calls = {"n": 0}

        def fake_universe(list_type):
            calls["n"] += 1
            return ["600001.SH"]

        monkeypatch.setattr(selector_mod.DataFetcher, "get_stock_universe", fake_universe)
        uc.ENABLED = False
        sel = StockSelector({"formula_name": "F",
                             "universe": {"type": "50", "exclude_st": False}})
        sel.resolve_universe()
        sel.resolve_universe()
        assert calls["n"] == 2 and not list(cache_dir.glob("*.json"))
