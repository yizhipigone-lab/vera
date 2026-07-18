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
        # 5m: 生成到 end 当天收盘 (含末日 9:35-15:00 bar, 与真实 TDX 一致)
        bars = pd.date_range(start, pd.Timestamp(end) + pd.Timedelta(days=1), freq="5min")
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
    """last_date 后有新日期 → 增量拉取 append, 不重拉旧段。
    F6 [H2]: 增量起点含重叠 bar (last_d), 用于 staleness 比对。"""
    calls = {"codes": []}

    def tracking_fetcher(stock_list, start, end, **k):
        calls["codes"].append((tuple(stock_list), str(start), str(end)))
        return _make_fake_kline(stock_list, start, end, **k)

    cache = _make_cache(tmp_path, fetcher=tracking_fetcher)
    cache.get(["002008.SZ"], "2024-01-01", "2024-01-10", period="1d")
    # 后续取更长区间 → 增量拉 01-10 (重叠 bar) 起
    cache.get(["002008.SZ"], "2024-01-01", "2024-01-20", period="1d")
    # 至少有一次增量拉取 (start 在 01-10 含重叠, 01-01 首次之后)
    has_incremental = any(s == "20240110" for (_, s, _) in calls["codes"])
    assert has_incremental, f"应有增量拉取 (含重叠 bar last_d), calls={calls['codes']}"


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


# ───────────────────────── 审计修复 F1-F6 ─────────────────────────


def test_f1_mixed_dividend_type_raises(tmp_path):
    """F1 [C1] dividend_type 非 front → 抛 ValueError, 杜绝混合口径静默错数。"""
    cache = _make_cache(tmp_path)
    with pytest.raises(ValueError):
        cache.get(["002008.SZ"], "2024-01-01", "2024-01-10", period="1d", dividend_type="none")
    # front / 1 正常
    cache.get(["002008.SZ"], "2024-01-01", "2024-01-10", period="1d", dividend_type="front")
    cache.get(["002008.SZ"], "2024-01-01", "2024-01-10", period="1d", dividend_type=1)


def test_f2_5m_end_day_not_dropped(tmp_path):
    """F2 [H3] 5m 区间末日整段不被切片丢弃 (end_ts=00:00 时仍含当天 9:35-15:00 bar)。"""
    cache = _make_cache(tmp_path)
    res = cache.get(["002008.SZ"], "2024-01-02", "2024-01-05", period="5m")
    close = res["Close"]["002008.SZ"].dropna()
    assert len(close) > 0
    last_day = close.index.max().normalize()
    assert last_day == pd.Timestamp("2024-01-05"), (
        f"5m 末日 2024-01-05 被丢弃, 最后 bar 日期={last_day}"
    )


def test_f2_1d_end_day_unchanged(tmp_path):
    """F2 [H3] 1d 末日行为不变 (00:00 本就等于 normalize)。"""
    cache = _make_cache(tmp_path)
    res = cache.get(["002008.SZ"], "2024-01-02", "2024-01-05", period="1d")
    close = res["Close"]["002008.SZ"].dropna()
    assert close.index.max() == pd.Timestamp("2024-01-05")


def test_f3_non_normalized_code_works(tmp_path):
    """F3 [M2] 非归一化代码 ("002008") 与归一化 ("002008.SZ") 等价, 共用一份缓存。"""
    cache = _make_cache(tmp_path)
    r1 = cache.get(["002008.SZ"], "2024-01-01", "2024-01-10", period="1d")
    r2 = cache.get(["002008"], "2024-01-01", "2024-01-10", period="1d")
    # 非归一化代码应能取到 (静默消失 bug)
    assert "002008.SZ" in r2["Close"].columns or "002008" in r2["Close"].columns
    c2 = r2["Close"].iloc[:, 0].dropna()
    assert len(c2) > 0, "非归一化代码 get(['002008']) 不应返回空"
    # 共用一份 parquet (不产生碎片)
    import glob
    files = glob.glob(str(tmp_path / "kline_cache" / "1d" / "*.parquet"))
    assert len(files) == 1, f"应只有一份 parquet, 实际 {files}"


def test_f4_backward_extension_warns(tmp_path, caplog):
    """F4 [M1] 请求起点早于缓存起点 → 打 WARNING, 不静默截断。"""
    cache = _make_cache(tmp_path)
    cache.get(["002008.SZ"], "2024-02-01", "2024-02-29", period="1d")  # 先缓存 2 月
    with caplog.at_level("WARNING", logger="core.kline_cache"):
        cache.get(["002008.SZ"], "2024-01-01", "2024-02-29", period="1d")  # 请求更早起点
    assert any("002008" in r.message and ("截断" in r.message or "早" in r.message or "缺失" in r.message)
               for r in caplog.records), "向后扩展静默截断必须告警"


def test_f5_intact_false_cooldown(tmp_path):
    """F5 [H1] intact=false 股连续两次 get() 只全量重拉一次 (24h 冷却, 不 thrash)。"""
    def always_gap_fetcher(stock_list, start, end, **k):
        res = _make_fake_kline(stock_list, start, end, **k)
        for f in ["Open", "High", "Low", "Close", "Volume", "Amount"]:
            res[f] = res[f].drop(pd.Timestamp("2024-01-05"), errors="ignore")
        return res

    calls = {"n": 0}

    def counting(sl, s, e, **k):
        calls["n"] += 1
        return always_gap_fetcher(sl, s, e, **k)

    cache = _make_cache(tmp_path, fetcher=counting)
    cache.get(["002008.SZ"], "2024-01-01", "2024-01-10", period="1d")
    # 确认 intact=false
    rec = [r for r in cache._manifest_all() if r[0] == "002008.SZ"][0]
    intact_idx = cache.MANIFEST_COLUMNS.index("intact")
    assert not rec[intact_idx]
    n1 = calls["n"]
    # 第二次 get (同一进程内, 应冷却不再全量重拉)
    cache.get(["002008.SZ"], "2024-01-01", "2024-01-10", period="1d")
    assert calls["n"] == n1, f"intact=false 冷却期不应重复全量重拉 (n1={n1}, n2={calls['n']})"


def test_f6_staleness_overlap_triggers_full_refetch(tmp_path):
    """F6 [H2] 增量含重叠 bar, close 不一致 → 全量重拉 (前复权分红 shift)。"""
    calls = []

    def shifting_fetcher(stock_list, start, end, **k):
        calls.append(str(start))
        res = _make_fake_kline(stock_list, start, end, **k)
        # 若 start 是增量起点 (非全量), 把首 bar close 改大模拟分红 shift
        if str(start) > "20240101":
            res["Close"] = res["Close"] * 1.5
        return res

    cache = _make_cache(tmp_path, fetcher=shifting_fetcher)
    cache.get(["002008.SZ"], "2024-01-01", "2024-01-10", period="1d")
    calls.clear()
    # 增量取 [last+? , end] → 重叠 bar close 变了 → 应触发全量重拉 (含更早 start)
    cache.get(["002008.SZ"], "2024-01-01", "2024-01-20", period="1d")
    assert any(s <= "20240101" for s in calls), (
        f"重叠 bar close 不一致应触发全量重拉, 但 calls={calls}"
    )


# ───────────────────────── Phase 2: 5m 缺 bar 检测 ─────────────────────────


def test_5m_whole_day_gap_detected(tmp_path, caplog):
    """Phase2: 5m 整天缺 (0 根, 如 002008 6.23-6.29) → 检出 + 告警。"""
    def gap5m_fetcher(stock_list, start, end, **k):
        res = _make_fake_kline(stock_list, start, end, **k)
        for f in ["Open", "High", "Low", "Close", "Volume", "Amount"]:
            df = res[f]
            # 删 01-05 当天所有 bar (整天缺)
            mask = df.index.normalize() != pd.Timestamp("2024-01-05")
            res[f] = df[mask]
        return res

    cache = _make_cache(tmp_path, fetcher=gap5m_fetcher)
    with caplog.at_level("WARNING", logger="core.kline_cache"):
        cache.get(["002008.SZ"], "2024-01-02", "2024-01-08", period="5m")
    assert any("kline_gap" in r.message and "002008" in r.message for r in caplog.records), (
        "5m 整天缺必须检出并告警"
    )


def test_5m_partial_bars_warns(tmp_path, caplog):
    """Phase2: 5m 某天 bar 数 < 48 (部分缺口) → 打 kline_gap_5m 告警。"""
    def partial5m_fetcher(stock_list, start, end, **k):
        res = _make_fake_kline(stock_list, start, end, **k)
        for f in ["Open", "High", "Low", "Close", "Volume", "Amount"]:
            df = res[f]
            # 01-05 只留 10 根 (部分缺)
            day = df.index.normalize() == pd.Timestamp("2024-01-05")
            keep = (~day) | (df.groupby(df.index.normalize()).cumcount() < 10)
            res[f] = df[keep]
        return res

    cache = _make_cache(tmp_path, fetcher=partial5m_fetcher)
    with caplog.at_level("WARNING", logger="core.kline_cache"):
        cache.get(["002008.SZ"], "2024-01-02", "2024-01-08", period="5m")
    assert any("kline_gap_5m" in r.message for r in caplog.records), (
        "5m 部分缺 bar (某天 <48 根) 必须告警"
    )


def test_get_close_price_passes_use_cache(monkeypatch, tmp_path):
    """③b: get_close_price(use_cache=True) 透传到 get_kline (run_cached 路径接缓存)。"""
    from core.data_fetcher import DataFetcher
    seen = {}

    def fake_get_kline(cls, *a, **k):
        seen["use_cache"] = k.get("use_cache")
        return {"Close": pd.DataFrame({"002008.SZ": [1.0, 2.0]},
                                      index=pd.to_datetime(["2024-01-02", "2024-01-03"]))}

    monkeypatch.setattr(DataFetcher, "get_kline", classmethod(fake_get_kline))
    DataFetcher.get_close_price(["002008.SZ"], "20240101", "20240110", use_cache=True)
    assert seen["use_cache"] is True, "get_close_price 应把 use_cache 透传给 get_kline"




def test_get_kline_windowed_passes_use_cache(monkeypatch):
    """use_cache=True 时, get_kline_windowed 应把 use_cache 透传给 get_kline。"""
    import core.data_fetcher as df_mod

    seen = {}

    def fake_get_kline(stock_list, start_time="", end_time="", period="1d",
                       dividend_type="front", count=-1, fill_data=True,
                       field_list=None, *, use_cache=False, force_refresh=False):
        seen["use_cache"] = use_cache
        idx = pd.DatetimeIndex(["2026-06-30 09:35", "2026-06-30 09:40"])
        close = pd.DataFrame({"000001": [10.0, 10.1]}, index=idx)
        return {f: close.copy() for f in ["Open", "High", "Low", "Close", "Volume", "Amount"]}

    monkeypatch.setattr(df_mod.DataFetcher, "get_kline", fake_get_kline)
    # get_trading_days 底层 _ensure_ready() 会触 TDX, 必须 mock (铁律: 测试不触 TDX)
    monkeypatch.setattr(
        df_mod.DataFetcher, "get_trading_days",
        classmethod(lambda cls, s, e, market="SH":
                    list(pd.bdate_range("2026-06-30", "2026-08-31"))))
    sel = pd.DataFrame({"stock_code": ["000001"], "select_date": ["2026-06-30"]})
    df_mod.DataFetcher.get_kline_windowed(
        sel, period="5m", dividend_type="front", fill_data=False, use_cache=True)
    assert seen.get("use_cache") is True
