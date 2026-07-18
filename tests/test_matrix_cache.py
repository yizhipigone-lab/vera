"""矩阵级缓存测试 (2026-07-18, backtest/matrix_cache.py + engine.run 接缝)。

钉死五件事:
1. save/load roundtrip 数组逐格一致, 指纹不符自动失效
2. engine.run 两次跑 (miss→hit) 进核心循环的矩阵**逐字节一致**, 且第二次不取数
3. 选股结果变化 → key 变 (不会错吃旧矩阵)
4. K 线数据更新 (指纹变) → 失效
5. degrade_5m=on 时缓存整体跳过; LRU 只留最近 N 份
"""
from __future__ import annotations

import json
import os
import time

import numpy as np
import pandas as pd
import pytest

import backtest.engine as engine_module
from backtest import matrix_cache as mc
from backtest.engine import BacktestEngine, ENGINE_VERSION
from core.data_fetcher import DataFetcher


def _small_prep(seed=0):
    idx = pd.date_range("2025-01-02 09:35", periods=96, freq="5min")
    cols = ["600001", "000001"]
    rng = np.random.default_rng(seed)
    close = pd.DataFrame(rng.random((96, 2)) + 10, index=idx, columns=cols)
    entries = pd.DataFrame(rng.random((96, 2)) < 0.05, index=idx, columns=cols)
    return {
        "close": close, "entries": entries,
        "high": close.values * 1.01, "low": close.values * 0.99,
        "open": close.values.copy(),
        "tradable": np.ones((96, 2), dtype=bool),
        "last_tradable_idx": np.array([95, 95], dtype=np.int64),
        "idx": idx, "cols": cols,
        "degraded_np": None, "degrade_res": None,
    }


def _selections(codes=("600001", "000001")):
    return pd.DataFrame([
        {"select_date": pd.Timestamp("2025-01-02"), "stock_code": c} for c in codes])


@pytest.fixture
def kline_dir(tmp_path, monkeypatch):
    """K 线缓存目录指向 tmp (指纹隔离, 不碰真实 data/kline_cache)。"""
    d = tmp_path / "kline_cache"
    d.mkdir()
    monkeypatch.setattr(DataFetcher, "_KLINE_CACHE_DIR", str(d))
    return d


class TestRoundTrip:
    def test_save_load_equal(self, tmp_path, kline_dir):
        prep = _small_prep()
        mc.save(tmp_path / "mc", "k1", ENGINE_VERSION, prep)
        out = mc.load(tmp_path / "mc", "k1", ENGINE_VERSION)
        assert out is not None
        pd.testing.assert_frame_equal(out["close"], prep["close"], check_freq=False)
        pd.testing.assert_frame_equal(out["entries"], prep["entries"], check_freq=False)
        for f in ("high", "low", "open", "tradable", "last_tradable_idx"):
            np.testing.assert_array_equal(np.asarray(out[f]), np.asarray(prep[f]))
        assert list(out["cols"]) == prep["cols"]
        assert len(out["idx"]) == 96

    def test_fingerprint_mismatch_invalidates(self, tmp_path, kline_dir):
        prep = _small_prep()
        mc.save(tmp_path / "mc", "k1", ENGINE_VERSION, prep)
        # K 线缓存出现新文件 → 指纹变 → 失效
        f = kline_dir / "600001.parquet"
        f.write_bytes(b"x")
        os.utime(f, (time.time() + 10, time.time() + 10))
        assert mc.load(tmp_path / "mc", "k1", ENGINE_VERSION) is None

    def test_none_fields_roundtrip(self, tmp_path, kline_dir):
        prep = _small_prep()
        prep["high"] = None
        prep["low"] = None
        mc.save(tmp_path / "mc", "k1", ENGINE_VERSION, prep)
        out = mc.load(tmp_path / "mc", "k1", ENGINE_VERSION)
        assert out is not None and out["high"] is None and out["low"] is None

    def test_corrupt_meta_treated_as_miss(self, tmp_path, kline_dir):
        prep = _small_prep()
        mc.save(tmp_path / "mc", "k1", ENGINE_VERSION, prep)
        (tmp_path / "mc" / "k1" / "meta.json").write_text("{bad json", encoding="utf-8")
        assert mc.load(tmp_path / "mc", "k1", ENGINE_VERSION) is None
        assert not (tmp_path / "mc" / "k1").exists(), "坏目录应被清理"


class TestKey:
    def test_selections_change_changes_key(self):
        k1 = mc.build_key(_selections(), "20250101", "20260628", "5m", 75, True, ENGINE_VERSION)
        k2 = mc.build_key(_selections(("600001", "000002")), "20250101", "20260628", "5m", 75, True, ENGINE_VERSION)
        k3 = mc.build_key(_selections(), "20250101", "20260628", "5m", 60, True, ENGINE_VERSION)
        assert k1 != k2, "选股结果变 → key 必须变"
        assert k1 != k3, "窗口长度变 → key 必须变"
        # 同输入稳定
        assert k1 == mc.build_key(_selections(), "20250101", "20260628", "5m", 75, True, ENGINE_VERSION)


class TestLRU:
    def test_prune_keeps_newest(self, tmp_path, kline_dir):
        root = tmp_path / "mc"
        for i in range(4):
            mc.save(root, f"k{i}", ENGINE_VERSION, _small_prep(seed=i), keep=3)
            # 保证 mtime 可区分
            meta = root / f"k{i}" / "meta.json"
            os.utime(meta, (1000 + i, 1000 + i))
        remaining = sorted(d.name for d in root.iterdir() if d.is_dir())
        assert remaining == ["k1", "k2", "k3"], f"LRU 应只留 3 份: {remaining}"


def _make_5m_windowed(n_days=2):
    """小尺寸 5m 数据 (标准时刻) + 全 True mask, 供 mock get_kline_windowed。"""
    idx = pd.DatetimeIndex([])
    for d in range(n_days):
        day = pd.Timestamp("2025-01-02") + pd.Timedelta(days=d)
        idx = idx.append(pd.date_range(f"{day:%Y-%m-%d} 09:35", periods=48, freq="5min"))
    close = pd.DataFrame(
        10.0 + np.arange(len(idx))[:, None] * 0.01 + np.arange(2)[None, :],
        index=idx, columns=["600001", "000001"])
    kline = {"Close": close, "High": close * 1.01, "Low": close * 0.99,
             "Open": close.copy()}
    mask = pd.DataFrame(True, index=idx, columns=close.columns)
    return kline, mask


class TestEngineSeam:
    def _run_twice(self, tmp_path, monkeypatch, kline_dir):
        kline, mask = _make_5m_windowed()
        calls = {"n": 0}

        def fake_windowed(selections, period, window_trading_days, dividend_type,
                          fill_data, *, use_cache=False):
            calls["n"] += 1
            return kline, mask

        monkeypatch.setattr(DataFetcher, "get_kline_windowed", fake_windowed)
        monkeypatch.setattr(BacktestEngine, "_filter_limit_up",
                            lambda self, entries, prices: entries)
        captured = []

        def fake_core(price_np, entry_np, *args, **kwargs):
            captured.append({
                "price": price_np.copy(), "entry": entry_np.copy(),
                "tradable": kwargs["tradable_np"].copy(),
                "lti": kwargs["last_tradable_idx"].copy(),
                "high": kwargs["high_np"].copy(), "low": kwargs["low_np"].copy(),
                "open": kwargs["open_np"].copy(),
            })
            return np.full(price_np.shape[0], 100000.0), np.empty((0, 9))

        monkeypatch.setattr(engine_module, "_simulate_core_v3", fake_core)
        eng = BacktestEngine({"period": "5m", "matrix_cache": True,
                              "matrix_cache_dir": str(tmp_path / "mc")})
        sel = _selections()
        eng.run(selections=sel, start_time="20250102", end_time="20250103",
                stop_config={})
        eng.run(selections=sel, start_time="20250102", end_time="20250103",
                stop_config={})
        return calls, captured

    def test_hit_skips_fetch_and_identical(self, tmp_path, monkeypatch, kline_dir):
        calls, captured = self._run_twice(tmp_path, monkeypatch, kline_dir)
        assert calls["n"] == 1, "第二次应命中缓存, 不再取数"
        assert len(captured) == 2
        a, b = captured
        for f in ("price", "entry", "tradable", "lti", "high", "low", "open"):
            np.testing.assert_array_equal(a[f], b[f], err_msg=f"{f} 两次跑不一致")

    def test_degrade_on_skips_cache(self, tmp_path, monkeypatch, kline_dir):
        used = {"load": 0, "save": 0}
        monkeypatch.setattr(mc, "load", lambda *a, **k: used.__setitem__("load", used["load"] + 1))
        monkeypatch.setattr(mc, "save", lambda *a, **k: used.__setitem__("save", used["save"] + 1))
        monkeypatch.setattr(BacktestEngine, "_prepare_run_matrices",
                            lambda self, *a: None)  # 取数空 → _empty_result 提前返回
        eng = BacktestEngine({"period": "5m", "matrix_cache": True,
                              "matrix_cache_dir": str(tmp_path / "mc"),
                              "degrade_5m": True})
        eng.run(selections=_selections(), start_time="20250102",
                end_time="20250103", stop_config={})
        assert used == {"load": 0, "save": 0}, "degrade_5m=on 时缓存必须整体跳过"
