# -*- coding: utf-8 -*-
"""KlineCache TDD 测试 (Phase 1: 1d 缓存 + gap 检测)。

用 fake tdx_fetcher / calendar_fetcher mock TDX, 不依赖真实行情接口。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest

from core.kline_cache import KlineCache


# ── fake TDX fetcher / calendar ─────────────────────────────


def _make_fake_kline(stock_list, start, end, period="1d", dividend_type="front", **kwargs):
    """合成 OHLC, 每股一条递增 close。返回 get_kline 同结构 dict。**kwargs 吞 count/fill_data 等。"""
    idx = pd.bdate_range(start, end, freq="B")  # 工作日
    if period == "1d":
        bars = idx
    else:
        bars = pd.date_range(start, end, freq="5min")
        bars = bars[bars.indexer_between_time("9:35", "15:00")]
    out = {}
    for code in stock_list:
        base = 10.0 + sum(ord(c) for c in code) % 50
        close = pd.Series([base + i * 0.1 for i in range(len(bars))], index=bars)
        out.setdefault("Close", {})[code] = close
        out.setdefault("Open", {})[code] = close * 0.99
        out.setdefault("High", {})[code] = close * 1.02
        out.setdefault("Low", {})[code] = close * 0.98
        out.setdefault("Volume", {})[code] = pd.Series(1e6, index=bars)
        out.setdefault("Amount", {})[code] = close * 1e6
    # 转 dict[str, DataFrame]
    result = {"ErrorId": "0"}
    for field in ["Open", "High", "Low", "Close", "Volume", "Amount"]:
        result[field] = pd.DataFrame({c: out[field][c] for c in stock_list})
    return result


def _fake_calendar():
    """返回 2024 全年工作日 (str YYYYMMDD)。"""
    return [d.strftime("%Y%m%d") for d in pd.bdate_range("2024-01-01", "2024-12-31")]


def _make_cache(tmp_path, fetcher=None, calendar=None):
    return KlineCache(
        cache_dir=str(tmp_path / "kline_cache"),
        tdx_fetcher=fetcher or _make_fake_kline,
        calendar_fetcher=calendar or _fake_calendar,
    )


# ───────────────────────── 基础读写 ─────────────────────────


def test_cache_miss_fetch_writes_parquet(tmp_path):
    """首次取数 → parquet 落盘 + manifest 记录。"""
    cache = _make_cache(tmp_path)
    cache.get(["002008.SZ"], "2024-01-01", "2024-01-10", period="1d")
    pfile = tmp_path / "kline_cache" / "1d" / "002008.SZ.parquet"
    assert pfile.exists(), "首次取数应落盘 parquet"
    # manifest 有记录
    rows = cache._manifest_all()
    assert any(r[0] == "002008.SZ" and r[1] == "1d" for r in rows)


def test_cache_hit_no_tdx_call(tmp_path):
    """二次取数 → 命中缓存, TDX 零调用。"""
    calls = {"n": 0}

    def counting_fetcher(*a, **k):
        calls["n"] += 1
        return _make_fake_kline(*a, **k)

    cache = _make_cache(tmp_path, fetcher=counting_fetcher)
    cache.get(["002008.SZ"], "2024-01-01", "2024-01-10", period="1d")
    n_after_first = calls["n"]
    cache.get(["002008.SZ"], "2024-01-01", "2024-01-10", period="1d")
    assert calls["n"] == n_after_first, "二次取数应命中缓存, TDX 零调用"


def test_return_signature_unchanged(tmp_path):
    """返回结构 = {'Open':df,'High':df,...,'Amount':df}, 列=股票、行=date。"""
    cache = _make_cache(tmp_path)
    res = cache.get(["002008.SZ", "600519.SH"], "2024-01-01", "2024-01-10", period="1d")
    assert set(res.keys()) >= {"Open", "High", "Low", "Close", "Volume", "Amount"}
    for field in ["Open", "High", "Low", "Close", "Volume", "Amount"]:
        df = res[field]
        assert isinstance(df, pd.DataFrame)
        assert set(df.columns) == {"002008.SZ", "600519.SH"}
        assert isinstance(df.index, pd.DatetimeIndex)


# ───────────────────────── 增量 ─────────────────────────


def test_incremental_append_new_dates(tmp_path):
    """last_date 后有新日期 → 增量拉取 append, 不重拉旧段。"""
    calls = {"codes": []}

    def tracking_fetcher(stock_list, start, end, **k):
        calls["codes"].append((tuple(stock_list), str(start), str(end)))
        return _make_fake_kline(stock_list, start, end, **k)

    cache = _make_cache(tmp_path, fetcher=tracking_fetcher)
    cache.get(["002008.SZ"], "2024-01-01", "2024-01-10", period="1d")
    # 后续取更长区间 → 增量拉 01-10 之后
    cache.get(["002008.SZ"], "2024-01-01", "2024-01-20", period="1d")
    # 至少有一次增量拉取 (start 在 01-10 之后)
    has_incremental = any(s > "20240110" for (_, s, _) in calls["codes"])
    assert has_incremental, "应有增量拉取 (last_date 之后)"


# ───────────────────────── gap 检测 (1d) ─────────────────────────


def test_gap_detection_warns_and_refetch(tmp_path, caplog):
    """1d 缺日 → 告警 + 自动补拉。"""
    # 构造一个会缺日的 fetcher: 永远不返回 01-05 这天
    def gap_fetcher(stock_list, start, end, **k):
        res = _make_fake_kline(stock_list, start, end, **k)
        for field in ["Open", "High", "Low", "Close", "Volume", "Amount"]:
            res[field] = res[field].drop(pd.Timestamp("2024-01-05"), errors="ignore")
        return res

    cache = _make_cache(tmp_path, fetcher=gap_fetcher)
    with caplog.at_level("WARNING", logger="core.kline_cache"):
        cache.get(["002008.SZ"], "2024-01-01", "2024-01-10", period="1d")
    assert any("kline_gap" in r.message and "002008" in r.message for r in caplog.records), (
        "1d 缺日必须告警 (kline_gap)"
    )


def test_gap_persist_marks_intact_false(tmp_path):
    """补拉仍缺 → manifest intact=false。"""
    def always_gap_fetcher(stock_list, start, end, **k):
        res = _make_fake_kline(stock_list, start, end, **k)
        for field in ["Open", "High", "Low", "Close", "Volume", "Amount"]:
            res[field] = res[field].drop(pd.Timestamp("2024-01-05"), errors="ignore")
        return res

    cache = _make_cache(tmp_path, fetcher=always_gap_fetcher)
    cache.get(["002008.SZ"], "2024-01-01", "2024-01-10", period="1d")
    rows = cache._manifest_all()
    rec = [r for r in rows if r[0] == "002008.SZ" and r[1] == "1d"][0]
    # intact 字段 (索引 7) 应为 0/False
    intact_idx = cache.MANIFEST_COLUMNS.index("intact")
    assert not rec[intact_idx], "补拉仍缺 → intact=false"


# ───────────────────────── 原子写 ─────────────────────────


def test_atomic_write_no_half_file(tmp_path):
    """正常写入后 parquet 可读, 无 .tmp 残留。"""
    cache = _make_cache(tmp_path)
    cache.get(["002008.SZ"], "2024-01-01", "2024-01-10", period="1d")
    cache_dir = tmp_path / "kline_cache" / "1d"
    tmps = list(cache_dir.glob("*.tmp"))
    assert not tmps, "不应残留 .tmp 文件"
    import pyarrow.parquet as pq
    table = pq.read_table(cache_dir / "002008.SZ.parquet")
    assert table.num_rows > 0


# ───────────────────────── DataFetcher 集成 ─────────────────────────


def test_get_kline_use_cache_false_calls_tdx_directly(monkeypatch):
    """use_cache=False → 走 _get_kline_from_tdx 直拉, 不建缓存。"""
    from core.data_fetcher import DataFetcher
    calls = {"n": 0}

    def fake_tdx(cls, *a, **k):
        calls["n"] += 1
        return _make_fake_kline(*a, **k)

    monkeypatch.setattr(DataFetcher, "_get_kline_from_tdx", classmethod(fake_tdx))
    monkeypatch.setattr(DataFetcher, "_ensure_ready", classmethod(lambda cls: None))
    DataFetcher.get_kline(["002008.SZ"], "20240101", "20240110", use_cache=False)
    assert calls["n"] == 1, "use_cache=False 应直拉 TDX 一次"


def test_get_kline_use_cache_true_routes_via_cache(monkeypatch, tmp_path):
    """use_cache=True → 走 KlineCache, 二次命中无 TDX 调用。"""
    from core.data_fetcher import DataFetcher
    monkeypatch.setattr(DataFetcher, "_KLINE_CACHE_DIR", str(tmp_path / "kc"))
    monkeypatch.setattr(DataFetcher, "_ensure_ready", classmethod(lambda cls: None))
    monkeypatch.setattr(DataFetcher, "get_trading_dates", classmethod(
        lambda cls, *a, **k: _fake_calendar()))

    calls = {"n": 0}

    def fake_tdx(cls, *a, **k):
        calls["n"] += 1
        return _make_fake_kline(*a, **k)

    monkeypatch.setattr(DataFetcher, "_get_kline_from_tdx", classmethod(fake_tdx))
    DataFetcher.get_kline(["002008.SZ"], "20240101", "20240110", use_cache=True)
    n1 = calls["n"]
    DataFetcher.get_kline(["002008.SZ"], "20240101", "20240110", use_cache=True)
    assert calls["n"] == n1, "二次 use_cache=True 应命中缓存, TDX 零调用"
    assert n1 >= 1, "首次应 miss-fetch"


def test_get_kline_signature_backward_compat(monkeypatch):
    """原位置参数调用 (无 use_cache) 仍工作, 走 TDX 直拉。"""
    from core.data_fetcher import DataFetcher
    monkeypatch.setattr(DataFetcher, "_ensure_ready", classmethod(lambda cls: None))
    monkeypatch.setattr(DataFetcher, "_get_kline_from_tdx", classmethod(
        lambda cls, *a, **k: _make_fake_kline(*a, **k)))
    # 旧式调用: 全位置参数 (与 engine.py:778 调用方式一致)
    res = DataFetcher.get_kline(["002008.SZ"], "20240101", "20240110", "1d", "front")
    assert "Close" in res

