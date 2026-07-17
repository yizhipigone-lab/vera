"""5m 数据层降级测试 (计划书 2026-07-18, Phase A)。

被测: backtest/degrade_5m.py — 从交易日历合成完整 5m 网格 (每天恰好 48 根) +
缺 5m 的股-天用 1d OHLC 填充 + degraded_np 标记 + 降级交易事后扫描。
engine 集成: degrade_5m 开关 / tradable 合并 / 1d 涨停拒绝 / 降级笔数统计。
mock DataFetcher, 不触 TDX。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

import backtest.engine as engine_module
from backtest.degrade_5m import (
    BARS_5M_PER_DAY,
    apply_5m_degradation,
    recompute_last_tradable_idx,
    scan_degraded_positions,
    synthesize_5m_grid,
)
from backtest.engine import BacktestEngine


# ─────────────────────────────────────────────────────────────
# 公共构造
# ─────────────────────────────────────────────────────────────

DAYS = ["2026-06-22", "2026-06-23", "2026-06-24"]  # 6.23 模拟全市场缺口日


def _times_48(d: str) -> list:
    """一天的 48 根 5m bar 时间: 9:35..11:30 (24) + 13:05..15:00 (24)。"""
    out = []
    t = pd.Timestamp(f"{d} 09:35")
    for _ in range(24):
        out.append(t)
        t += pd.Timedelta(minutes=5)
    t = pd.Timestamp(f"{d} 13:05")
    for _ in range(24):
        out.append(t)
        t += pd.Timedelta(minutes=5)
    return out


def _grid_index(days=DAYS) -> pd.DatetimeIndex:
    return pd.DatetimeIndex([t for d in days for t in _times_48(d)])


def _mk_5m(close_map: dict, days=DAYS) -> pd.DataFrame:
    """close_map: {code: {date_str: 收盘价 or None(全天缺失)}}。全天值=当天 48 根同价。"""
    idx = _grid_index(days)
    data = {}
    for code, per_day in close_map.items():
        col = []
        for d in days:
            v = per_day.get(d)
            col.extend([np.nan] * BARS_5M_PER_DAY if v is None else [v] * BARS_5M_PER_DAY)
        data[code] = col
    return pd.DataFrame(data, index=idx)


def _mk_1d(close_map: dict, days=DAYS) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in days])
    return pd.DataFrame(
        {c: [per_day.get(d, np.nan) for d in days] for c, per_day in close_map.items()},
        index=idx)


def _ohlc_from_close(close_5m: pd.DataFrame):
    """从 close 造 high/low/open (简单偏移)。"""
    return close_5m * 1.01, close_5m * 0.99, close_5m * 0.995


def _bounds(codes, days=DAYS):
    """全部股窗口 = 全区间。"""
    return {c: (pd.Timestamp(days[0]), pd.Timestamp(days[-1])) for c in codes}


# ─────────────────────────────────────────────────────────────
# 网格构建 (CRITICAL-1)
# ─────────────────────────────────────────────────────────────

def test_grid_exactly_48_bars_per_day():
    """每天恰好 48 根 (T+1 i//bpday / 首日 bar_index%bpday 不变量)。"""
    grid = synthesize_5m_grid([pd.Timestamp(d) for d in DAYS])
    assert len(grid) == 3 * 48
    counts = grid.normalize().value_counts()
    assert (counts.values == 48).all()


def test_grid_bar_times_and_lunch_break():
    """首根 9:35, 末根 15:00, 午间 11:30→13:05 断档。"""
    grid = synthesize_5m_grid([pd.Timestamp("2026-06-23")])
    assert grid[0] == pd.Timestamp("2026-06-23 09:35")
    assert grid[-1] == pd.Timestamp("2026-06-23 15:00")
    assert pd.Timestamp("2026-06-23 11:30") in grid
    assert pd.Timestamp("2026-06-23 13:05") in grid
    assert pd.Timestamp("2026-06-23 12:00") not in grid


def test_grid_market_wide_gap_day_rows_exist():
    """全市场缺口日 (任何股都没有 bar) 也能从日历合成 48 行 — 否则无从填充。"""
    days = [pd.Timestamp("2026-06-22"), pd.Timestamp("2026-06-23")]
    grid = synthesize_5m_grid(days)
    gap_rows = grid[grid.normalize() == pd.Timestamp("2026-06-23")]
    assert len(gap_rows) == 48


def test_grid_empty_input():
    assert len(synthesize_5m_grid([])) == 0


# ─────────────────────────────────────────────────────────────
# 填充机制
# ─────────────────────────────────────────────────────────────

def test_missing_5m_day_filled_with_1d():
    """某股某天 5m 全缺 + 1d 有 → 48 根填满 1d OHLC, degraded_np 对应 True。"""
    close = _mk_5m({"600001": {"2026-06-22": 10.0, "2026-06-23": None, "2026-06-24": 10.2}})
    high, low, opn = _ohlc_from_close(close)
    c1 = _mk_1d({"600001": {"2026-06-22": 10.0, "2026-06-23": 10.1, "2026-06-24": 10.2}})
    h1, l1, o1 = c1 + 0.1, c1 - 0.1, c1 - 0.05

    res = apply_5m_degradation(close, high, low, opn, c1, h1, l1, o1,
                               window_bounds=_bounds(["600001"]))
    day2 = close.index.normalize() == pd.Timestamp("2026-06-23")
    assert (res.close.loc[day2, "600001"] == 10.1).all()
    assert (res.high.loc[day2, "600001"] == 10.2).all()
    assert (res.low.loc[day2, "600001"] == 10.0).all()
    assert np.allclose(res.open.loc[day2, "600001"].values, 10.05)
    assert res.degraded_np[day2, 0].all()
    assert not res.degraded_np[~day2, 0].any()
    assert res.n_stock_days == 1
    assert res.degraded_days == {"2026-06-23": 1}


def test_partial_missing_day_treated_as_full_missing():
    """部分 bar 缺失的当天按"整日缺"处理 (明示假设 3): 全天 48 根填 1d。"""
    close = _mk_5m({"600001": {"2026-06-22": 10.0, "2026-06-23": 10.1, "2026-06-24": 10.2}})
    day2 = close.index.normalize() == pd.Timestamp("2026-06-23")
    close.iloc[BARS_5M_PER_DAY:BARS_5M_PER_DAY + 18, 0] = np.nan  # 只剩 30 根有效
    high, low, opn = _ohlc_from_close(close)
    c1 = _mk_1d({"600001": {"2026-06-22": 10.0, "2026-06-23": 11.1, "2026-06-24": 10.2}})
    h1, l1, o1 = c1 + 0.1, c1 - 0.1, c1 - 0.05

    res = apply_5m_degradation(close, high, low, opn, c1, h1, l1, o1,
                               window_bounds=_bounds(["600001"]))
    # 全天 (含原本有效的 30 根) 被 1d 覆盖
    assert (res.close.loc[day2, "600001"] == 11.1).all()
    assert res.degraded_np[day2, 0].all()


def test_suspended_day_not_filled():
    """1d 也缺 (真停牌) → 不填充, 保持 NaN 不可交易 (G2)。"""
    close = _mk_5m({"600001": {"2026-06-22": 10.0, "2026-06-23": None, "2026-06-24": 10.2}})
    high, low, opn = _ohlc_from_close(close)
    c1 = _mk_1d({"600001": {"2026-06-22": 10.0, "2026-06-24": 10.2}})  # 6.23 停牌
    h1, l1, o1 = c1 + 0.1, c1 - 0.1, c1 - 0.05

    res = apply_5m_degradation(close, high, low, opn, c1, h1, l1, o1,
                               window_bounds=_bounds(["600001"]))
    day2 = close.index.normalize() == pd.Timestamp("2026-06-23")
    assert res.close.loc[day2, "600001"].isna().all()
    assert not res.degraded_np[day2, 0].any()
    assert res.n_stock_days == 0


def test_other_stock_unaffected():
    """别股 5m 完整 → 不降级, 数据一字不动。"""
    close = _mk_5m({
        "600001": {"2026-06-22": 10.0, "2026-06-23": None, "2026-06-24": 10.2},
        "600002": {"2026-06-22": 20.0, "2026-06-23": 20.1, "2026-06-24": 20.2},
    })
    high, low, opn = _ohlc_from_close(close)
    c1 = _mk_1d({
        "600001": {"2026-06-22": 10.0, "2026-06-23": 10.1, "2026-06-24": 10.2},
        "600002": {"2026-06-22": 20.0, "2026-06-23": 20.1, "2026-06-24": 20.2},
    })
    h1, l1, o1 = c1 + 0.1, c1 - 0.1, c1 - 0.05

    res = apply_5m_degradation(close, high, low, opn, c1, h1, l1, o1,
                               window_bounds=_bounds(["600001", "600002"]))
    pd.testing.assert_frame_equal(res.close[["600002"]], close[["600002"]])
    assert not res.degraded_np[:, 1].any()
    assert res.n_stock_days == 1


def test_out_of_window_not_filled():
    """窗口外的缺 5m 股-天 (有 1d) 不填充 — 稀疏窗口"窗口内才可交易"语义不破。"""
    close = _mk_5m({"600001": {"2026-06-22": 10.0, "2026-06-23": None, "2026-06-24": 10.2}})
    high, low, opn = _ohlc_from_close(close)
    c1 = _mk_1d({"600001": {"2026-06-22": 10.0, "2026-06-23": 10.1, "2026-06-24": 10.2}})
    h1, l1, o1 = c1 + 0.1, c1 - 0.1, c1 - 0.05
    # 窗口只覆盖 6.22 和 6.24, 6.23 在窗口外
    bounds = {"600001": (pd.Timestamp("2026-06-24"), pd.Timestamp("2026-06-24"))}

    res = apply_5m_degradation(close, high, low, opn, c1, h1, l1, o1,
                               window_bounds=bounds)
    day2 = close.index.normalize() == pd.Timestamp("2026-06-23")
    assert res.close.loc[day2, "600001"].isna().all()
    assert not res.degraded_np.any()


def test_limit_up_degraded_day_rejected():
    """降级日 1d 涨停 → 不填充 (信号丢弃), rejected_limit_up 计数 (MEDIUM-3)。"""
    close = _mk_5m({"600001": {"2026-06-22": 10.0, "2026-06-23": None, "2026-06-24": 10.2}})
    high, low, opn = _ohlc_from_close(close)
    # 6.23 1d 收盘 11.0 vs 前收 10.0 → +10% 涨停 (主板 0.10)
    c1 = _mk_1d({"600001": {"2026-06-22": 10.0, "2026-06-23": 11.0, "2026-06-24": 10.2}})
    h1, l1, o1 = c1 + 0.1, c1 - 0.1, c1 - 0.05

    res = apply_5m_degradation(close, high, low, opn, c1, h1, l1, o1,
                               window_bounds=_bounds(["600001"]),
                               limit_ratio_vec=np.array([0.10]))
    day2 = close.index.normalize() == pd.Timestamp("2026-06-23")
    assert res.close.loc[day2, "600001"].isna().all()
    assert not res.degraded_np.any()
    assert res.rejected_limit_up == 1


def test_adjust_mismatch_detected():
    """复权一致性 (LOW-1): 5m 完整日的聚合 OHLC vs 1d 不一致 → 计数。"""
    close = _mk_5m({"600001": {"2026-06-22": 10.0, "2026-06-23": 10.1, "2026-06-24": 10.2}})
    high, low, opn = _ohlc_from_close(close)
    # 6.23 的 1d 与 5m 不一致 (复权口径漂移)
    c1 = _mk_1d({"600001": {"2026-06-22": 10.0, "2026-06-23": 99.9, "2026-06-24": 10.2}})
    h1, l1, o1 = c1 + 0.1, c1 - 0.1, c1 - 0.05

    res = apply_5m_degradation(close, high, low, opn, c1, h1, l1, o1,
                               window_bounds=_bounds(["600001"]))
    assert res.adjust_mismatches >= 1


def test_adjust_consistent_no_mismatch():
    """5m 日聚合 == 1d → 0 违例。"""
    close = _mk_5m({"600001": {"2026-06-22": 10.0, "2026-06-23": 10.1, "2026-06-24": 10.2}})
    high, low, opn = _ohlc_from_close(close)
    c1 = _mk_1d({"600001": {"2026-06-22": 10.0, "2026-06-23": 10.1, "2026-06-24": 10.2}})
    # 与 _ohlc_from_close 的偏移一致
    h1, l1, o1 = c1 * 1.01, c1 * 0.99, c1 * 0.995

    res = apply_5m_degradation(close, high, low, opn, c1, h1, l1, o1,
                               window_bounds=_bounds(["600001"]))
    assert res.adjust_mismatches == 0


# ─────────────────────────────────────────────────────────────
# 降级交易事后扫描
# ─────────────────────────────────────────────────────────────

def test_limit_up_ratio_aligned_by_column_name_not_position():
    """审计 HIGH-1: 涨停比率必须按列名对齐, 不是按位置。

    1d 拉取的列序/列数与 5m 无保证 (KlineCache 跳过无数据股)。688 是 20%,
    若按位置错拿 600 的 10% → +15% 被误判涨停拒绝 (该填的没填)。
    """
    codes = ["600001", "688001"]
    close = _mk_5m({
        "600001": {"2026-06-22": 10.0, "2026-06-23": None, "2026-06-24": 10.2},
        "688001": {"2026-06-22": 20.0, "2026-06-23": None, "2026-06-24": 20.2},
    })
    high, low, opn = _ohlc_from_close(close)
    # 1d 列序故意颠倒 + 600001 涨停 (+10%), 688001 +15% (20% 板不算涨停)
    c1 = _mk_1d({
        "688001": {"2026-06-22": 20.0, "2026-06-23": 23.0, "2026-06-24": 20.2},
        "600001": {"2026-06-22": 10.0, "2026-06-23": 11.0, "2026-06-24": 10.2},
    })
    h1, l1, o1 = c1 + 0.1, c1 - 0.1, c1 - 0.05
    # ratio_vec 按 5m 列序 (600001→10%, 688001→20%)
    res = apply_5m_degradation(close, high, low, opn, c1, h1, l1, o1,
                               window_bounds=_bounds(codes),
                               limit_ratio_vec=np.array([0.10, 0.20]))
    day2 = close.index.normalize() == pd.Timestamp("2026-06-23")
    ci_600, ci_688 = list(close.columns).index("600001"), list(close.columns).index("688001")
    # 600001 涨停 → 拒绝; 688001 +15% < 20% → 正常填充 (错对齐会把它也拒了)
    assert res.close.loc[day2, "600001"].isna().all()
    assert np.allclose(res.close.loc[day2, "688001"].values, 23.0)
    assert res.rejected_limit_up == 1


def test_limit_up_1d_missing_column_safe():
    """审计 HIGH-1b: 1d 缺某股列 (整段无数据) → 该股不降级, 且不 broadcast 报错。"""
    close = _mk_5m({
        "600001": {"2026-06-22": 10.0, "2026-06-23": None, "2026-06-24": 10.2},
        "600002": {"2026-06-22": 20.0, "2026-06-23": None, "2026-06-24": 20.2},
    })
    high, low, opn = _ohlc_from_close(close)
    # 1d 只有 600001 一列 (600002 整段缺)
    c1 = _mk_1d({"600001": {"2026-06-22": 10.0, "2026-06-23": 10.1, "2026-06-24": 10.2}})
    h1, l1, o1 = c1 + 0.1, c1 - 0.1, c1 - 0.05
    res = apply_5m_degradation(close, high, low, opn, c1, h1, l1, o1,
                               window_bounds=_bounds(["600001", "600002"]),
                               limit_ratio_vec=np.array([0.10, 0.10]))
    assert res.n_stock_days == 1  # 只 600001 降级, 不崩


def test_scan_degraded_positions():
    """持仓区间 [entry_idx, exit_idx] 含降级 bar → 标记; 部分出场按 (ci, entry_idx) 聚合。"""
    n_bars = 3 * BARS_5M_PER_DAY
    degraded = np.zeros((n_bars, 1), dtype=bool)
    degraded[BARS_5M_PER_DAY:2 * BARS_5M_PER_DAY, 0] = True  # 第 2 天降级
    # 持仓 1: entry 第1天 → exit 第3天 (跨降级日); 阶梯拆两行 (部分出场)
    # 持仓 2: entry/exit 都在第3天 (不碰降级日)
    raw = np.array([
        [0, 10, 60, 10.0, 10.5, 100, 50.0, 0.05, 5.0],    # 持仓1 部分卖
        [0, 10, 100, 10.0, 10.8, 100, 80.0, 0.08, 4.0],   # 持仓1 剩余
        [0, 105, 140, 10.2, 10.3, 100, 10.0, 0.01, 6.0],  # 持仓2
    ])
    positions = scan_degraded_positions(raw, degraded)
    assert len(positions) == 2
    p1 = [p for p in positions if p["entry_idx"] == 10][0]
    p2 = [p for p in positions if p["entry_idx"] == 105][0]
    assert p1["is_degraded"] and p1["exit_idx"] == 100 and p1["n_trade_rows"] == 2
    assert not p2["is_degraded"]


def test_recompute_last_tradable_idx():
    tradable = np.array([[True, False], [True, True], [False, True]])
    lti = recompute_last_tradable_idx(tradable)
    assert list(lti) == [1, 2]
    assert recompute_last_tradable_idx(np.zeros((3, 1), dtype=bool))[0] == -1


# ─────────────────────────────────────────────────────────────
# engine 集成 (mock DataFetcher, 不触 TDX)
# ─────────────────────────────────────────────────────────────

def _run_engine_5m(monkeypatch, close_5m, mask, kline_1d, trading_days,
                   selections, config=None, stop_config=None, capture=None,
                   core_raw_trades=None):
    """公共驱动: mock 窗口拉取 + 交易日历 + 1d 拉取, 捕获 _simulate_core_v3 入参。"""
    eng = BacktestEngine(config or {'period': '5m', 'degrade_5m': True})
    kline = {'Close': close_5m, 'High': close_5m * 1.01,
             'Low': close_5m * 0.99, 'Open': close_5m.copy()}

    monkeypatch.setattr(
        engine_module.DataFetcher, 'get_kline_windowed',
        staticmethod(lambda selections, period, window_trading_days, dividend_type,
                     fill_data: (kline, mask)))
    monkeypatch.setattr(
        engine_module.DataFetcher, 'get_trading_days',
        classmethod(lambda cls, start, end, market="SH": list(trading_days)))
    monkeypatch.setattr(
        engine_module.DataFetcher, 'get_kline',
        classmethod(lambda cls, stock_list, start_time="", end_time="", period="1d",
                    dividend_type="front", count=-1, fill_data=True, field_list=None,
                    **kw: kline_1d))
    monkeypatch.setattr(BacktestEngine, '_filter_limit_up',
                        lambda self, entries, prices: entries)
    # _limit_ratio_vector 会查 get_cached_info → 触 TDX 初始化, 留下全局
    # "已初始化但未连接" 状态, 污染后续真实管线测试的 skip 判定。mock 掉。
    monkeypatch.setattr(BacktestEngine, '_limit_ratio_vector',
                        lambda self, columns: np.full(len(columns), 0.10))

    def fake_core(price_np, entry_np, *args, **kwargs):
        if capture is not None:
            capture['price_np'] = price_np
            capture['entry_np'] = entry_np
            capture['high_np'] = kwargs.get('high_np')
            capture['tradable_np'] = kwargs.get('tradable_np')
            capture['last_tradable_idx'] = kwargs.get('last_tradable_idx')
            capture['shape'] = price_np.shape
        return np.full(price_np.shape[0], 100000.0), (
            core_raw_trades if core_raw_trades is not None else np.empty((0, 9)))

    monkeypatch.setattr(engine_module, '_simulate_core_v3', fake_core)
    result = eng.run(selections=selections, start_time='20260622', end_time='20260624',
                     stop_config=stop_config or {'time_stop': {'enabled': True, 'max_hold_days': 20}})
    return result


def _engine_fixture():
    """3 天网格, 600001.SH 第 2 天 (6.23) 5m 全缺, 1d 三天都有。

    代码用规范化格式 .SH — compute_window_bounds 会 normalize_list,
    生产环境 kline 列同为规范化格式, 测试须同口径否则窗口边界对不上。
    """
    code = "600001.SH"
    days_ts = [pd.Timestamp(d) for d in DAYS]
    close_5m = _mk_5m({code: {"2026-06-22": 10.0, "2026-06-23": None, "2026-06-24": 10.2}})
    # 窗口拉取只返回有 bar 的行 (缺口日无行, 真实场景)
    close_5m = close_5m.dropna()
    mask = pd.DataFrame(True, index=close_5m.index, columns=close_5m.columns)
    c1 = _mk_1d({code: {"2026-06-22": 10.0, "2026-06-23": 10.1, "2026-06-24": 10.2}})
    kline_1d = {'Close': c1, 'High': c1 + 0.1, 'Low': c1 - 0.1, 'Open': c1 - 0.05}
    selections = pd.DataFrame([{'select_date': pd.Timestamp('2026-06-23'), 'stock_code': code}])
    return close_5m, mask, kline_1d, days_ts, selections


def test_degrade_disabled_by_default(monkeypatch):
    """degrade_5m 未开 (默认 False) → 不插行不填充, 行为与现状一致 (G4)。"""
    close_5m, mask, kline_1d, days_ts, selections = _engine_fixture()
    capture = {}
    _run_engine_5m(monkeypatch, close_5m, mask, kline_1d, days_ts, selections,
                   config={'period': '5m'}, capture=capture)
    # 不插行: 行数 = 2 天 × 48 (缺口日无行)
    assert capture['shape'][0] == 2 * BARS_5M_PER_DAY


def test_degraded_day_rows_inserted_and_tradable(monkeypatch):
    """开启降级: 缺口日插入 48 行 + 填充 + tradable_np 在降级 bar 为 True (CRITICAL-1/2)。"""
    close_5m, mask, kline_1d, days_ts, selections = _engine_fixture()
    capture = {}
    _run_engine_5m(monkeypatch, close_5m, mask, kline_1d, days_ts, selections,
                   capture=capture)
    assert capture['shape'][0] == 3 * BARS_5M_PER_DAY
    tradable = capture['tradable_np']
    # 降级日 (第 2 天, bar 48..95) 可交易
    assert tradable[BARS_5M_PER_DAY:2 * BARS_5M_PER_DAY, 0].all()


def test_last_tradable_idx_covers_degraded_bars(monkeypatch):
    """last_tradable_idx 重算后覆盖降级 bar (不误判退市)。"""
    close_5m, mask, kline_1d, days_ts, selections = _engine_fixture()
    # 窗口在第 2 天结束 (6.24 无 bar 且窗口外) → 无降级时 lti=47, 降级后 lti=95
    close_5m = close_5m[close_5m.index.normalize() <= pd.Timestamp('2026-06-23')]
    mask = mask.loc[close_5m.index]
    days_ts = [pd.Timestamp('2026-06-22'), pd.Timestamp('2026-06-23')]
    capture = {}
    _run_engine_5m(monkeypatch, close_5m, mask, kline_1d, days_ts, selections,
                   capture=capture)
    assert capture['last_tradable_idx'][0] == 2 * BARS_5M_PER_DAY - 1


def test_degraded_entry_price_is_1d_close(monkeypatch):
    """降级日入场 = 信号日最后一根 bar (15:00), 价格 = 1d 收盘 (铁律)。"""
    close_5m, mask, kline_1d, days_ts, selections = _engine_fixture()
    capture = {}
    _run_engine_5m(monkeypatch, close_5m, mask, kline_1d, days_ts, selections,
                   capture=capture)
    entry_bar = 2 * BARS_5M_PER_DAY - 1  # 6.23 15:00
    assert capture['entry_np'][entry_bar, 0]
    assert capture['price_np'][entry_bar, 0] == pytest.approx(10.1)
    # 填充在 ffill 之前: 降级日 high = 1d high (10.2), 不是前一日 ffill
    assert capture['high_np'][BARS_5M_PER_DAY, 0] == pytest.approx(10.2)


def test_degraded_trade_count_in_result(monkeypatch):
    """result.degradation: 含降级 bar 的持仓被计数 (事后扫描, 不进 loop)。"""
    close_5m, mask, kline_1d, days_ts, selections = _engine_fixture()
    # 一笔交易: entry 6.23 (降级日, bar 95) → exit 6.24 (bar 100)
    raw = np.array([[0, 95, 100, 10.1, 10.2, 100, 10.0, 0.01, 6.0]])
    result = _run_engine_5m(monkeypatch, close_5m, mask, kline_1d, days_ts,
                            selections, capture={}, core_raw_trades=raw)
    degr = result.get("degradation")
    assert degr is not None and degr["enabled"] is True
    assert degr["degraded_trades"] == 1
    assert degr["total_trades"] == 1
    assert degr["degraded_pct"] == pytest.approx(1.0)
    assert degr["degraded_days"] == {"2026-06-23": 1}


def test_limit_up_degraded_day_rejected_engine(monkeypatch, caplog):
    """engine 集成: 降级日 1d 涨停 → 不填充, 该 bar 不可交易, 告警 (MEDIUM-3)。"""
    close_5m, mask, _, days_ts, selections = _engine_fixture()
    # 6.23 1d 收盘 11.0 vs 前收 10.0 → 涨停
    c1 = _mk_1d({"600001.SH": {"2026-06-22": 10.0, "2026-06-23": 11.0, "2026-06-24": 10.2}})
    kline_1d = {'Close': c1, 'High': c1 + 0.1, 'Low': c1 - 0.1, 'Open': c1 - 0.05}
    capture = {}
    with caplog.at_level(logging.WARNING):
        _run_engine_5m(monkeypatch, close_5m, mask, kline_1d, days_ts, selections,
                       capture=capture)
    # 缺口日仍插行 (48 根/天不变量) 但不填充 → 不可交易
    assert capture['shape'][0] == 3 * BARS_5M_PER_DAY
    assert not capture['tradable_np'][BARS_5M_PER_DAY:2 * BARS_5M_PER_DAY, 0].any()
    assert any('涨停' in r.message for r in caplog.records)
