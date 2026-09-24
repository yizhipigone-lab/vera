# -*- coding: utf-8 -*-
"""tests/test_prepare_matrices_parity.py — prepare_matrices 公开接缝 parity
(2026-09-19 架构修订批次 3.1)。

背景: tools/ 的 4 个 sweep 脚本曾手工复刻 engine.run() 准备段, 引擎一改即
静默漂移。收编后用本测试锁死: **同一份取数输出下, 公开接缝的产物与旧复刻
配方逐字段一致** (旧配方作为参考实现内联在本测试里 —— 这是它唯一合法副本)。

口径说明: 接缝会把 end_time 传给 get_kline_windowed (2026-07-21 窗口截断口径),
旧复刻段有的传有的不传 —— 那是取数层差异, 不在本测试范围 (fake fetcher 忽略
end_time); 本测试锁的是"取数输出 → 矩阵"的变换层。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest._constants import STD_BAR_TIMES
from backtest.engine import (
    BacktestEngine,
    _build_tradable_from_raw,
    recompute_last_tradable_idx,
)

CODES = ["000001.SZ", "600519.SH"]
N_DAYS = 8
WIN_TD = 5


def _synth_kline():
    """合成 5m K 线: N_DAYS 天 × 48 根标准 bar × 2 只股, 价格确定性伪随机。"""
    bars = STD_BAR_TIMES["5m"]
    days = pd.bdate_range("2026-08-03", periods=N_DAYS)
    idx = pd.DatetimeIndex(
        [pd.Timestamp(f"{d.date()} {t}") for d in days for t in bars])
    rng = np.random.RandomState(42)
    data = {}
    base = {"000001.SZ": 10.0, "600519.SH": 100.0}
    for f in ("Open", "High", "Low", "Close"):
        data[f] = pd.DataFrame(index=idx, columns=CODES, dtype=float)
    for c in CODES:
        walk = base[c] * np.cumprod(1 + rng.randn(len(idx)) * 0.001)
        data["Close"][c] = walk
        data["Open"][c] = walk * (1 - 0.0005)
        data["High"][c] = walk * 1.002
        data["Low"][c] = walk * 0.998
    data["Volume"] = pd.DataFrame(10000.0, index=idx, columns=CODES)
    # window_mask: 全 True, 只把最后一天的尾盘两只股设 False (验证 mask 生效)
    mask = pd.DataFrame(True, index=idx, columns=CODES)
    mask.iloc[-10:, :] = False
    return data, mask


def _selections():
    # 每只股在第 2 天一个信号
    return pd.DataFrame({
        "stock_code": CODES,
        "select_date": [pd.bdate_range("2026-08-03", periods=N_DAYS)[1].strftime("%Y%m%d")] * 2,
    })


@pytest.fixture()
def fake_fetch(monkeypatch):
    """DataFetcher.get_kline_windowed → 合成数据; ST 信息 → 全非 ST。"""
    kline, mask = _synth_kline()
    from core.data_fetcher import DataFetcher

    def _fake(cls, selections, period, window_trading_days=45,
              dividend_type="front", fill_data=False, *,
              use_cache=False, end_time=None):
        return kline, mask
    monkeypatch.setattr(DataFetcher, "get_kline_windowed",
                        classmethod(_fake))
    monkeypatch.setattr("backtest.engine.get_cached_info",
                        lambda code: {"IsSTGP": "0"})
    return kline, mask


def _reference_recipe(engine, selections, kline, window_mask, apply_limit_filter=True):
    """旧 sweep 复刻配方 (gs_5m_sweep/quantqq_5m_sweep 版, 改造前逐行照抄)。"""
    close = engine._ensure_index(kline["Close"])
    high_df = engine._ensure_index(kline["High"])
    low_df = engine._ensure_index(kline["Low"])
    open_df = engine._ensure_index(kline["Open"])

    close, high_df, low_df, open_df = BacktestEngine._drop_nonstandard_5m_bars(
        close, high_df, low_df, open_df)

    entries = engine._build_entry_signals(selections, close)
    cols = sorted(close.columns.intersection(entries.columns))
    cols = sorted(set(cols) & set(high_df.columns) & set(low_df.columns))

    close_raw = close.reindex(index=close.index, columns=cols)
    close = close_raw.ffill()
    entries = entries.reindex(index=close.index, columns=cols, fill_value=False)
    if apply_limit_filter:
        entries = engine._filter_limit_up(entries, close)
    idx = close.index

    high_np = high_df.reindex(index=idx, columns=cols).ffill().values.astype(np.float64)
    low_np = low_df.reindex(index=idx, columns=cols).ffill().values.astype(np.float64)
    open_np = open_df.reindex(index=idx, columns=cols).values.astype(np.float64)

    tradable_np, _ = _build_tradable_from_raw(close_raw, close)
    wm = window_mask.reindex(index=idx, columns=cols, fill_value=False).values.astype(bool)
    tradable_np = tradable_np & wm
    last_tradable_idx = recompute_last_tradable_idx(tradable_np)
    return dict(close=close, entries=entries, high=high_np, low=low_np,
                open=open_np, tradable=tradable_np,
                last_tradable_idx=last_tradable_idx, idx=idx, cols=cols)


def test_prepare_matrices_matches_reference_recipe(fake_fetch):
    """接缝产物 == 旧复刻配方, 逐字段 (值/索引/列/形状) 一致。"""
    kline, mask = fake_fetch
    engine = BacktestEngine({"period": "5m", "use_kline_cache": True})
    sel = _selections()
    prep = engine.prepare_matrices(sel, "20260803", "20260814", WIN_TD)
    assert prep is not None
    ref = _reference_recipe(engine, sel, kline, mask)

    pd.testing.assert_frame_equal(
        prep["close"], ref["close"].reindex(columns=prep["cols"]))
    # entries: 接缝内不做涨停过滤 (那是买入口径的事, sweep 拿到接缝产物后
    # 自己再滤) —— 接缝产物 == 参考配方的未过滤版; 再过滤后 == 过滤版
    ref_unfiltered = _reference_recipe(engine, sel, kline, mask,
                                       apply_limit_filter=False)
    pd.testing.assert_frame_equal(prep["entries"], ref_unfiltered["entries"])
    pd.testing.assert_frame_equal(
        engine._filter_limit_up(prep["entries"], prep["close"]), ref["entries"])
    np.testing.assert_allclose(prep["high"], ref["high"], equal_nan=True)
    np.testing.assert_allclose(prep["low"], ref["low"], equal_nan=True)
    np.testing.assert_allclose(prep["open"], ref["open"], equal_nan=True)
    np.testing.assert_array_equal(prep["tradable"], ref["tradable"])
    np.testing.assert_array_equal(prep["last_tradable_idx"],
                                  ref["last_tradable_idx"])
    assert list(prep["cols"]) == list(ref["cols"])
    assert prep["idx"].equals(ref["idx"])


def test_prepare_matrices_empty_returns_none(monkeypatch):
    """取数为空 → None (sweep 的 no_kline 分支靠它)。"""
    from core.data_fetcher import DataFetcher

    def _fake(cls, *a, **kw):
        return {}, pd.DataFrame()
    monkeypatch.setattr(DataFetcher, "get_kline_windowed",
                        classmethod(_fake))
    engine = BacktestEngine({"period": "5m"})
    assert engine.prepare_matrices(_selections(), "20260803",
                                   "20260814", WIN_TD) is None


def test_filter_limit_up_config_off_is_identity(monkeypatch):
    """filter_limit_up=False (批次 3.1 新配置): close_t 口径下 entries 原样返回,
    取代 attr_gp1014 的 monkeypatch 变体。"""
    kline, mask = _synth_kline()
    from core.data_fetcher import DataFetcher

    def _fake(cls, *a, **kw):
        return kline, mask
    monkeypatch.setattr(DataFetcher, "get_kline_windowed",
                        classmethod(_fake))
    monkeypatch.setattr("backtest.engine.get_cached_info",
                        lambda code: {"IsSTGP": "0"})
    engine = BacktestEngine({"period": "5m", "filter_limit_up": False})
    sel = _selections()
    prep = engine.prepare_matrices(sel, "20260803", "20260814", WIN_TD)
    out_entries, bp, info = engine._apply_entry_price_mode(
        prep["entries"], prep["close"], prep["high"], prep["low"],
        prep["open"], prep["tradable"])
    assert bp is None and info is None
    pd.testing.assert_frame_equal(out_entries, prep["entries"])


# ---------------------------------------------------------------------------
# 2026-09-20 审计 P2-4: 上面的 parity 测试对四个改动**全绿** (fake 数据太干净)
#   (a) 接缝把 end_time 传成 None      → fake fetcher 忽略 end_time
#   (b) 去掉 close 的 ffill            → 合成数据无停牌 NaN
#   (c) 去掉 low 的列交集              → 三只股都在 low 里
#   (d) 跳过非标准 bar 过滤            → 合成数据全是标准时刻
# 下面这条用"脏数据"把四点逐条锁死 (每条都有对应的突变验证记录)。
# ---------------------------------------------------------------------------

DIRTY_CODES = ["000001.SZ", "600519.SH", "300750.SZ"]   # 300750 不在 Low 里


def _dirty_kline():
    """脏 5m 数据: 停牌 NaN + 非标准时刻 bar + 缺 Low 列的股。"""
    bars = list(STD_BAR_TIMES["5m"])
    days = pd.bdate_range("2026-08-03", periods=N_DAYS)
    idx = pd.DatetimeIndex(
        [pd.Timestamp(f"{d.date()} {t}") for d in days for t in bars]
        + [pd.Timestamp(f"{days[2].date()} 13:00:00")])   # 非标准 bar
    idx = idx.sort_values()
    data = {}
    for f in ("Open", "High", "Low", "Close"):
        data[f] = pd.DataFrame(index=idx, columns=DIRTY_CODES, dtype=float)
    for c in DIRTY_CODES:
        data["Close"][c] = np.linspace(10.0, 12.0, len(idx))
        data["Open"][c] = data["Close"][c] * 0.999
        data["High"][c] = data["Close"][c] * 1.002
        data["Low"][c] = data["Close"][c] * 0.998
    # 停牌: 000001.SZ 第 4 天整日无成交 (Close/High/Low/Open 全 NaN)
    day4 = [t for t in idx if t.date() == days[3].date()]
    for f in ("Open", "High", "Low", "Close"):
        data[f].loc[day4, "000001.SZ"] = np.nan
    # 缺 Low 列: 300750.SZ 从 Low 里整个消失 (真实场景 = 该股无 low 字段)
    data["Low"] = data["Low"].drop(columns=["300750.SZ"])
    data["Volume"] = pd.DataFrame(10000.0, index=idx, columns=DIRTY_CODES)
    mask = pd.DataFrame(True, index=idx, columns=DIRTY_CODES)
    return data, mask


def test_seam_end_time_intraday_filter_ffill_and_col_intersection(monkeypatch):
    """脏数据逐条锁死审计 P2-4 的四个突变点。"""
    kline, mask = _dirty_kline()
    seen = {}
    from core.data_fetcher import DataFetcher

    def _fake(cls, selections, period, window_trading_days=45,
              dividend_type="front", fill_data=False, *,
              use_cache=False, end_time=None):
        seen.update(period=period, window_trading_days=window_trading_days,
                    use_cache=use_cache, end_time=end_time)
        return kline, mask
    monkeypatch.setattr(DataFetcher, "get_kline_windowed", classmethod(_fake))
    monkeypatch.setattr("backtest.engine.get_cached_info",
                        lambda code: {"IsSTGP": "0"})

    engine = BacktestEngine({"period": "5m", "use_kline_cache": True,
                             "degrade_5m": False})
    sel = pd.DataFrame({
        "stock_code": DIRTY_CODES,
        "select_date": [pd.bdate_range("2026-08-03", periods=N_DAYS)[1]
                        .strftime("%Y%m%d")] * len(DIRTY_CODES),
    })
    prep = engine.prepare_matrices(sel, "20260803", "20260817", WIN_TD)
    assert prep is not None

    # (a) 窗口终点截断口径: end_time 必须原样透传 (传 None 曾让测试全绿)
    assert seen["end_time"] == "20260817", "接缝必须把 end_time 透传给取数层"
    assert seen["period"] == "5m" and seen["use_cache"] is True

    # (d) 非标准时刻 bar (13:00 临停复牌竞价) 必须被过滤掉: 每天恰好 48 根
    per_day = pd.Series(1, index=prep["idx"]).groupby(prep["idx"].date).sum()
    assert set(per_day.unique()) == {48}, f"非标准 bar 未过滤: {per_day.unique()}"

    # (c) 列交集必须含 low: 只在 Close 里有的 300750.SZ 不许进矩阵
    assert "300750.SZ" not in prep["cols"], "列交集漏了 low_df (缺 low 的股混入)"
    assert set(prep["cols"]) == {"000001.SZ", "600519.SH"}

    # (b) close 必须 ffill: 停牌日 (第 4 天) 的值 = 前一根 bar 的值, 不许是 NaN
    day4 = [t for t in prep["idx"] if t.date() == pd.bdate_range(
        "2026-08-03", periods=N_DAYS)[3].date()]
    prev_bar = prep["close"].loc[:day4[0]].iloc[-2]["000001.SZ"]
    assert not np.isnan(prep["close"].loc[day4, "000001.SZ"]).any(), \
        "停牌日 close 出现 NaN (ffill 被去掉)"
    assert (prep["close"].loc[day4, "000001.SZ"] == prev_bar).all(), \
        "停牌日 close 未沿用前值 (ffill 口径不符)"
    # 原始价 (含停牌 NaN) 必须在 tradable 里体现: 停牌日该股不可交易
    day4_pos = [prep["idx"].get_loc(t) for t in day4]
    col_pos = prep["cols"].index("000001.SZ")
    assert not prep["tradable"][day4_pos, col_pos].any(), \
        "停牌日应判不可交易 (close_raw 的 NaN 被 ffill 吃掉了)"

