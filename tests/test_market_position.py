"""tests/test_market_position.py — 大盘位置纯数学 + 业务铁律 AST 守护 (2026-09-17)。

纯函数测试不碰磁盘、不碰网络 (core/market_position.py 零 IO)。
最后一块是铁律守护: 三个新模块源代码里不得出现 import trade
(业务铁律 1: 大盘研判只报告, 绝不联入仓位调度)。照 tests/brain/
test_sentiment_acceptance.py 的 AST 写法。
"""
from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from core import market_position as mp


def _series(vals, start="2020-01-01", freq="B"):
    return pd.Series([float(v) for v in vals],
                     index=pd.date_range(start, periods=len(vals), freq=freq))


def _rising(n, start=100.0, step=0.5):
    return _series(np.arange(n, dtype=float) * step + start)


# ── index_position / index_position_series ────────────────────────────


class TestIndexPosition:
    def test_highest_point_is_100th_percentile(self):
        p = mp.index_position(_rising(3000))
        assert p["pct_10y"] == 100.0
        assert p["close"] == pytest.approx(100.0 + 2999 * 0.5, abs=0.01)

    def test_lowest_point_is_low_percentile(self):
        p = mp.index_position(_rising(3000)[::-1].reset_index(drop=True))
        assert p["pct_10y"] is not None and p["pct_10y"] < 5.0

    def test_two_interfaces_agree(self):
        """单一实现: index_position 必须就是 series 的末行 (不可能漂移)。"""
        s = _rising(900)      # 需 ≥750 根才给十年百分位, ≥220 根才给牛熊
        df = mp.index_position_series(s)
        last = df.iloc[-1]
        single = mp.index_position(s)
        assert single["pct_10y"] is not None
        for col in mp.POSITION_COLUMNS:
            if col == "regime":
                assert single[col] == last[col]
            else:
                assert single[col] == pytest.approx(float(last[col]), abs=1e-6), col

    def test_short_sample_does_not_fake(self):
        """样本不足 → None, 绝不拿 0 或半截样本冒充。"""
        p = mp.index_position(_series([1.0, 2.0, 3.0]))
        assert p["close"] == 3.0
        assert p["pct_10y"] is None      # 不满 250 根不给分位
        assert p["regime"] is None       # MA250 未成形
        assert p["ma250_dev_pct"] is None

    def test_declining_series_is_bear_when_ma_formed(self):
        p = mp.index_position(_series(np.linspace(400, 100, 400)))
        assert p["regime"] == "bear"
        assert p["ma250_dev_pct"] < 0


# ── breadth_frame ─────────────────────────────────────────────────────


class TestBreadthFrame:
    def test_stub_bar_excluded_from_traded(self):
        """空壳 bar (有日期无成交) 不计入分母, 也不算"站上均线"。"""
        dates = pd.date_range("2024-01-01", periods=5, freq="B")
        close = pd.DataFrame({"A": [10.0, 10.1, 10.2, 10.3, 10.4],
                              "B": [20.0, 20.1, 20.2, 20.3, 20.4],
                              "C": [30.0, 30.1, 30.2, 30.3, 30.4]}, index=dates)
        vol = pd.DataFrame({"A": [100.0] * 5, "B": [100.0] * 5,
                            "C": [100.0, 100.0, 100.0, 100.0, 0.0]}, index=dates)
        bf = mp.breadth_frame(close, vol)
        assert bf.loc[dates[-1], "traded"] == 2
        assert bf.loc[dates[-1], "traded_ratio"] == pytest.approx(2 / 3)
        assert bf.loc[dates[-2], "traded"] == 3

    def test_rising_market_all_above_ma20(self):
        dates = pd.date_range("2024-01-01", periods=80, freq="B")
        vals = np.arange(80, dtype=float) + 100.0
        close = pd.DataFrame({"A": vals, "B": vals * 2}, index=dates)
        vol = pd.DataFrame({"A": [1.0] * 80, "B": [1.0] * 80}, index=dates)
        bf = mp.breadth_frame(close, vol)
        assert bf.loc[dates[-1], "above_ma20_pct"] == pytest.approx(100.0)

    def test_new_high_needs_full_window(self):
        """不满 60 根不算"创 60 日新高" (防新股上市头几天被记成新高)。"""
        dates = pd.date_range("2024-01-01", periods=80, freq="B")
        vals = np.arange(80, dtype=float) + 100.0
        close = pd.DataFrame({"A": vals}, index=dates)
        vol = pd.DataFrame({"A": [1.0] * 80}, index=dates)
        bf = mp.breadth_frame(close, vol)
        assert bf["new_high"].iloc[:59].sum() == 0
        assert bf["new_high"].iloc[59] == 1        # 第 60 根起满窗
        assert bf["new_low"].sum() == 0            # 单调上涨没有新低

    def test_falling_market_new_low_dominates(self):
        dates = pd.date_range("2024-01-01", periods=80, freq="B")
        vals = 200.0 - np.arange(80, dtype=float)
        close = pd.DataFrame({"A": vals}, index=dates)
        vol = pd.DataFrame({"A": [1.0] * 80}, index=dates)
        bf = mp.breadth_frame(close, vol)
        assert bf.loc[dates[-1], "new_low"] == 1
        assert bf.loc[dates[-1], "hl_spread"] == -1

    def test_empty_input_returns_empty_frame(self):
        bf = mp.breadth_frame(pd.DataFrame(), pd.DataFrame())
        assert len(bf) == 0
        assert "traded_ratio" in bf.columns


# ── last_valid_date ───────────────────────────────────────────────────


class TestLastValidDate:
    def test_skips_stub_last_day(self):
        """实测场景: 盘前抓数造出"有日期、volume=0"的末日 bar, 必须回退。"""
        dates = pd.date_range("2024-01-01", periods=4, freq="B")
        vol = pd.DataFrame({"A": [1.0, 1.0, 1.0, 0.0],
                            "B": [1.0, 1.0, 1.0, 0.0],
                            "C": [1.0, 1.0, 1.0, 0.0]}, index=dates)
        assert mp.last_valid_date(vol) == dates[2]

    def test_all_stub_returns_none(self):
        dates = pd.date_range("2024-01-01", periods=3, freq="B")
        vol = pd.DataFrame({"A": [0.0, 0.0, 0.0]}, index=dates)
        assert mp.last_valid_date(vol) is None

    def test_empty_returns_none(self):
        assert mp.last_valid_date(pd.DataFrame()) is None


# ── limit_counts / limit_counts_series ────────────────────────────────


class TestLimitCounts:
    @staticmethod
    def _frame():
        dates = pd.date_range("2024-01-01", periods=2, freq="B")
        close = pd.DataFrame({"600000.SH": [10.0, 11.0],
                              "300001.SZ": [10.0, 11.0]}, index=dates)
        vol = pd.DataFrame({"600000.SH": [100.0, 100.0],
                            "300001.SZ": [100.0, 100.0]}, index=dates)
        return dates, close, vol

    def test_main_board_10pct_is_limit_up_but_creation_board_is_not(self):
        dates, close, vol = self._frame()
        r = mp.limit_counts(close, vol, {"600000.SH": 0.10, "300001.SZ": 0.20},
                            dates[1])
        assert r["up"] == 1        # 主板 +10% 封板
        assert r["traded"] == 2

    def test_creation_board_20pct_is_limit_up(self):
        dates = pd.date_range("2024-01-01", periods=2, freq="B")
        close = pd.DataFrame({"300001.SZ": [10.0, 12.0]}, index=dates)
        vol = pd.DataFrame({"300001.SZ": [100.0, 100.0]}, index=dates)
        r = mp.limit_counts(close, vol, {"300001.SZ": 0.20}, dates[1])
        assert r["up"] == 1

    def test_limit_down(self):
        dates = pd.date_range("2024-01-01", periods=2, freq="B")
        close = pd.DataFrame({"600000.SH": [10.0, 9.0]}, index=dates)
        vol = pd.DataFrame({"600000.SH": [100.0, 100.0]}, index=dates)
        r = mp.limit_counts(close, vol, {"600000.SH": 0.10}, dates[1])
        assert r["down"] == 1 and r["up"] == 0

    def test_series_matches_single(self):
        """单一实现: 批量版与单日版必须同数 (回填走批量、页面走单日)。"""
        dates, close, vol = self._frame()
        ratios = pd.Series({"600000.SH": 0.10, "300001.SZ": 0.20})
        bulk = mp.limit_counts_series(close, vol, ratios, list(dates))
        for d in dates:
            assert bulk[d] == mp.limit_counts(close, vol, ratios, d)

    def test_stub_day_is_not_counted(self):
        dates = pd.date_range("2024-01-01", periods=3, freq="B")
        close = pd.DataFrame({"600000.SH": [10.0, 10.0, 11.0]}, index=dates)
        vol = pd.DataFrame({"600000.SH": [100.0, 100.0, 0.0]}, index=dates)
        r = mp.limit_counts(close, vol, {"600000.SH": 0.10}, dates[2])
        assert r["traded"] == 0 and r["up"] == 0


# ── similar_days ──────────────────────────────────────────────────────


def _fake_history(n=300, seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    return pd.DataFrame({f: rng.normal(50, 15, n) for f in mp.SIMILAR_FEATURES},
                        index=idx)


class TestSimilarDays:
    def test_identical_vector_is_matched_first(self):
        h = _fake_history()
        target = {f: float(h[f].iloc[-1]) for f in mp.SIMILAR_FEATURES}
        # 把第 100 行改成与 target 完全一样 —— 它应当是距离最小的一天
        for f in mp.SIMILAR_FEATURES:
            h.iloc[100, h.columns.get_loc(f)] = target[f]
        picks = mp.similar_days(target, h, top_n=3, gap=20)["picks"]
        assert picks, "应当能照出镜子"
        assert picks[0]["date"] == h.index[100].date().isoformat()
        assert picks[0]["distance"] == pytest.approx(0.0, abs=1e-9)

    def test_recent_days_excluded(self):
        """纪律①: 最近 gap 个交易日不能作为相似日 (否则永远是昨天)。"""
        h = _fake_history()
        gap = 20
        target = {f: float(h[f].iloc[-1]) for f in mp.SIMILAR_FEATURES}
        picks = mp.similar_days(target, h, top_n=5, gap=gap)["picks"]
        cutoff = h.index[-gap].date().isoformat()
        assert picks and all(p["date"] <= cutoff for p in picks)

    def test_neighbours_deduped(self):
        """纪律②: 已选中的日子前后 gap 个交易日内不再选 (否则照出一串连续日)。"""
        h = _fake_history()
        gap = 20
        target = {f: float(h[f].iloc[-1]) for f in mp.SIMILAR_FEATURES}
        picks = mp.similar_days(target, h, top_n=5, gap=gap)["picks"]
        pos = [h.index.get_loc(pd.Timestamp(p["date"])) for p in picks]
        for i in range(len(pos)):
            for j in range(i + 1, len(pos)):
                assert abs(pos[i] - pos[j]) > gap

    def test_default_excludes_a_whole_year(self):
        """§14.1 回归锁: 默认排除**整整一年** (252 个交易日), 不是 20 天。

        喂一个"最近 30 天里的完美匹配", 断言它**不会**被选中 —— 原默认 20 天时
        它会当选, 那就是拿上个月当"历史参照", 属于数据窥探。
        """
        assert mp.RECENT_EXCLUDE_BARS == 252
        assert mp.MIN_MATCH_GAP_BARS == 20
        h = _fake_history(n=400)
        target = {f: float(h[f].iloc[-1]) for f in mp.SIMILAR_FEATURES}
        for f in mp.SIMILAR_FEATURES:          # 30 天前那天改成与今天一模一样
            h.iloc[-30, h.columns.get_loc(f)] = target[f]
        res = mp.similar_days(target, h, top_n=5)
        got = [p["date"] for p in res["picks"]]
        assert h.index[-30].date().isoformat() not in got, \
            "最近一年内的日子被选中 = 数据窥探复发"
        cutoff = h.index[-mp.RECENT_EXCLUDE_BARS].date().isoformat()
        assert got and all(d <= cutoff for d in got)
        # 可选池 = 截止日(含)之前的全部历史日
        assert res["eligible"] == len(h) - mp.RECENT_EXCLUDE_BARS + 1
        assert res["exclude_recent"] == 252 and res["min_gap"] == 20

    def test_band_mode_returns_quantile_slice(self):
        """§14.2: 结论依据是"距离最近的一档"(前 5%), 不是 top-5 的中位数。"""
        h = _fake_history(n=400)
        target = {f: float(h[f].iloc[-1]) for f in mp.SIMILAR_FEATURES}
        res = mp.similar_days(target, h, top_n=5, quantile=0.05, max_band=300)
        assert res["band"], "开了 quantile 就必须给出一档样本"
        assert res["n_band"] == len(res["band"])
        assert res["n_band"] >= 5, "样本太少时至少给 5 个"
        assert res["n_band"] <= res["eligible"]
        dates = [b["date"] for b in res["band"]]
        assert len(set(dates)) == len(dates), "档内不许有重复日"
        # 档内距离单调不减 = 确实是"距离最近的那一档"
        dists = [b["distance"] for b in res["band"]]
        assert dists == sorted(dists), "分位带必须按距离从小到大取"
        assert res["quantile"] == 0.05

    def test_band_off_by_default(self):
        """没传 quantile 时不悄悄塞一档样本 (默认行为零变化)。"""
        h = _fake_history(n=400)
        target = {f: float(h[f].iloc[-1]) for f in mp.SIMILAR_FEATURES}
        res = mp.similar_days(target, h)
        assert res["band"] == [] and res["n_band"] == 0

    def test_short_history_returns_empty(self):
        res = mp.similar_days({f: 1.0 for f in mp.SIMILAR_FEATURES},
                              _fake_history(n=30))
        assert res["picks"] == [] and res["band"] == [] and res["eligible"] == 0

    def test_missing_feature_returns_empty(self):
        h = _fake_history().drop(columns=["vol_ann_20"])
        assert mp.similar_days({f: 1.0 for f in mp.SIMILAR_FEATURES}, h)["picks"] == []

    def test_target_with_none_returns_empty(self):
        h = _fake_history()
        target = {f: float(h[f].iloc[-1]) for f in mp.SIMILAR_FEATURES}
        target["amount_pct_1y"] = None
        assert mp.similar_days(target, h)["picks"] == []

    def test_zero_variance_feature_returns_empty(self):
        """某特征在历史上是常数 → 没法标准化 → 整题放弃, 不硬算。"""
        h = _fake_history(n=400)
        h["vol_ann_20"] = 12.0
        target = {f: float(h[f].iloc[-1]) for f in mp.SIMILAR_FEATURES}
        assert mp.similar_days(target, h)["picks"] == []


# ── forward_return ────────────────────────────────────────────────────


class TestForwardReturn:
    def test_basic_horizons(self):
        s = _series([100.0, 110.0, 121.0])
        assert mp.forward_return(s, s.index[0], 1) == pytest.approx(10.0)
        assert mp.forward_return(s, s.index[0], 2) == pytest.approx(21.0)

    def test_insufficient_sample_returns_none(self):
        s = _series([100.0, 110.0, 121.0])
        assert mp.forward_return(s, s.index[0], 5) is None

    def test_non_trading_start_rolls_forward(self):
        s = _series([100.0, 110.0, 121.0])   # 2020-01-01/02/03
        r = mp.forward_return(s, "2019-12-31", 1)   # 早于首日 → 从首日起算
        assert r == pytest.approx(10.0)

    def test_zero_horizon_returns_none(self):
        s = _series([100.0, 110.0])
        assert mp.forward_return(s, s.index[0], 0) is None


# ── 铁律 AST 守护 ─────────────────────────────────────────────────────

_POSITION_MODULES = ("core/market_position.py",
                     "core/market_position_runner.py",
                     "market_position_api.py")


def test_ast_no_trade_import():
    """业务铁律 1: 大盘位置三模块绝不 import trade。

    大盘研判只做提示与报告, 绝不联入仓位调度 —— 物理隔离靠 AST 静态断言,
    不靠自觉 (照 tests/brain/test_sentiment_acceptance.py)。
    """
    for rel in _POSITION_MODULES:
        p = Path(rel)
        assert p.exists(), f"模块不存在: {rel}"
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert not a.name.startswith("trade"), \
                        f"{rel} 物理隔离破坏: import {a.name}"
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert not mod.startswith("trade"), \
                    f"{rel} 物理隔离破坏: from {mod}"


def test_public_interface_within_limit():
    """铁律 8: 公开函数 ≤8 个 (深模块检查单第 2 问)。"""
    pub = [n for n in mp.__all__ if callable(getattr(mp, n, None))]
    assert len(pub) <= 8, f"core/market_position 公开函数 {len(pub)} 个 > 8: {pub}"
