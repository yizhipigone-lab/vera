# -*- coding: utf-8 -*-
"""parquet_cache 公共原语测试 (2026-08-01, 批次2: B1 .tmp 竞态修复 + B2 样板收编)。

钉死四件事:
1. B1 验收: 三个 selection 缓存两线程同写一个 key 不抛 FileNotFoundError
   (原固定 .tmp 名必现互踩, kline_cache 07-23 同类事故)
2. key 兼容: pcu.blake2b_key 与各模块旧拼接逐字节一致 (落盘文件名不变,
   存量缓存不失效) — 用独立手写参考实现交叉验证, 防"两边一起改错"
3. tmp_path_for: 唯一性 + 后缀语义
4. cache_root 注册表: set_root 覆盖, 未注册回退默认
"""
from __future__ import annotations

import hashlib
import threading

import pandas as pd
import pytest

from selection import selection_cache as sc
from selection import signal_day_cache as sdc
from selection import universe_cache as uc
from utils import parquet_cache as pcu


def _ref_key(*parts, digest_size=16, seed=b"", sep=b"\x00"):
    """各缓存模块旧哈希拼接的手写参考实现 (不 import pcu, 交叉验证用)。"""
    h = hashlib.blake2b(seed, digest_size=digest_size)
    for part in parts:
        h.update(str(part).encode("utf-8"))
        if sep is not None:
            h.update(sep)
    return h.hexdigest()


class TestKeyCompatibility:
    def test_selection_cache_key_unchanged(self):
        kw = dict(formula_name="FOO", formula_arg="3",
                  universe_cfg={"type": "23", "exclude_st": True, "sectors": []},
                  start_time="20240101", end_time="20241231",
                  period="1d", dividend_type=1, today_str="20260724")
        import json
        uni_json = json.dumps(sc._normalize_universe(kw["universe_cfg"]),
                              sort_keys=True, ensure_ascii=False, default=str)
        expected = _ref_key("FOO", "3", uni_json, "20240101", "20241231",
                            "1d", 1, "20260724", sc.SCHEMA_VERSION)
        assert sc.build_key(**kw) == expected

    def test_signal_day_combo_key_unchanged(self):
        expected = _ref_key("F", "", "poolh", "1d", 1, sdc.SCHEMA_VERSION)
        assert sdc._combo_key("F", "", "poolh", "1d", 1) == expected

    def test_universe_keys_unchanged(self):
        import json
        uni_json = json.dumps(sc._normalize_universe({"type": "50"}),
                              sort_keys=True, ensure_ascii=False, default=str)
        assert uc.build_key({"type": "50"}, "20260726") == \
            _ref_key(uni_json, "20260726", uc.SCHEMA_VERSION)
        assert uc.pool_hash(["B", "A"]) == _ref_key("A", "B")

    def test_matrix_cache_key_unchanged(self):
        from backtest import matrix_cache as mc
        sel = pd.DataFrame([
            {"select_date": pd.Timestamp("2025-01-02"), "stock_code": "600001"}])
        s = sel[["stock_code", "select_date"]].copy()
        s["select_date"] = pd.to_datetime(s["select_date"])
        s = s.sort_values(["stock_code", "select_date"]).reset_index(drop=True)
        row_hashes = pd.util.hash_pandas_object(s, index=False).values
        expected = _ref_key("20250101", "20260628", "5m", 75, True, "v1",
                            mc.SCHEMA_VERSION,
                            seed=row_hashes.tobytes(), sep=None)
        assert mc.build_key(sel, "20250101", "20260628", "5m", 75, True,
                            "v1") == expected


class TestTmpPathFor:
    def test_unique_per_call(self, tmp_path):
        target = tmp_path / "k1.parquet"
        a, b = pcu.tmp_path_for(target), pcu.tmp_path_for(target)
        assert a != b, "pid+uuid 临时名必须每次唯一 (B1 修复核心)"
        assert a.parent == target.parent
        assert a.name.startswith("k1.parquet.") and a.name.endswith(".tmp")

    def test_preserves_hidden_dotfile(self, tmp_path):
        """matrix_cache 的 .{key} 隐藏 tmp 目录语义: 点前缀不得丢失。"""
        t = pcu.tmp_path_for(tmp_path / ".abc")
        assert t.name.startswith(".abc.") and t.name.endswith(".tmp")


class TestRootRegistry:
    def test_set_get_root(self, tmp_path):
        pcu.set_root("test_reg", tmp_path / "x")
        try:
            assert pcu.get_root("test_reg", tmp_path / "fallback") == tmp_path / "x"
            assert pcu.get_root("test_reg_unset", tmp_path / "fb") == tmp_path / "fb"
        finally:
            pcu._roots.pop("test_reg", None)

    def test_module_default_root_via_registry(self, tmp_path):
        """default_cache_root 经注册表可覆盖 (conftest 一行式隔离的接缝)。"""
        pcu.set_root("universe_cache", tmp_path / "uc")
        try:
            assert uc.default_cache_root() == tmp_path / "uc"
        finally:
            pcu._roots.pop("universe_cache", None)


class TestConcurrentSameKey:
    """B1 验收: 两线程同写一个 key 不抛 FileNotFoundError。

    原固定 .tmp 名下一个线程 replace 移走 tmp 后另一线程必抛
    FileNotFoundError; pid+uuid 独立 tmp 后物理上不可能互踩。
    """

    @staticmethod
    def _hammer(save_fn):
        errors = []

        def _save():
            try:
                for _ in range(20):
                    save_fn()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=_save) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert errors == [], f"并发写同 key 抛出异常: {errors}"

    def test_selection_cache(self, tmp_path):
        df = pd.DataFrame({"stock_code": ["600001.SH", "000001.SZ"],
                           "select_date": pd.to_datetime(["2025-01-02"] * 2),
                           "formula_name": ["F"] * 2})
        self._hammer(lambda: sc.save(tmp_path, "same", df))
        assert sc.load(tmp_path, "same") is not None

    def test_signal_day_cache(self, tmp_path):
        df = pd.DataFrame({"stock_code": ["600001.SH"],
                           "select_date": pd.to_datetime(["2025-01-02"]),
                           "formula_name": ["F"]})
        root = tmp_path / "sdc"
        self._hammer(lambda: sdc._save_day(root, "combo", "20250102", df))
        assert sdc._load_day(root, "combo", "20250102") is not None

    def test_universe_cache(self, tmp_path):
        self._hammer(lambda: uc.save(tmp_path, "same", ["600001.SH"]))
        assert uc.load(tmp_path, "same") == ["600001.SH"]
