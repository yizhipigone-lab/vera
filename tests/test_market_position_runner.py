"""tests/test_market_position_runner.py — 大盘位置 IO 层 (2026-09-17)。

全程用 tmp 日线缓存 (conftest 已把 KLINE_1D_DIR / DAILY_PATH 指到 per-test tmp),
绝不碰生产 data/kline_cache 与 data/market_position (2026-07-27 缓存投毒事件的
同款隔离纪律)。

覆盖:
  - 落盘幂等 (同一天重跑不追加重复行)
  - 空壳 bar 回退 (末日 volume=0 → 快照日期回退到前一有效日)
  - 数据滞后打 stale 标记
  - 缓存布局契约 (断言与 KlineCache._parquet_path 生成同一路径 —— 直读 parquet
    是性能取舍, 靠这条测试防缓存布局改了之后静默读空)
  - 体温表 Markdown 必带三条已知偏差警告
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from core import market_position_runner as mpr


def _write_cache(n_days=400, n_stocks=6, stub_last=False, wavy_amount=False):
    """造一个最小日线缓存: n_stocks 只票 + 三大指数。

    写到 mpr.KLINE_1D_DIR (= conftest 隔离出来的 per-test tmp), 不写 tmp_path ——
    隔离的意义就是"runner 认哪个目录, 测试就往哪个目录造数据"。

    `wavy_amount`: 成交额改成**起伏**的（默认是单调上行）。默认那条序列的
    `rolling(...).rank(pct=True)` 恒等于 100，测不出"预热不足导致排名算错"（F-07）。
    """
    d = mpr.KLINE_1D_DIR
    d.mkdir(parents=True, exist_ok=True)
    dates = pd.date_range("2024-01-01", periods=n_days, freq="B")
    for i in range(n_stocks):
        code = f"60000{i}.SH"
        base = 10.0 + i
        close = np.linspace(base, base * 1.3, n_days)
        vol = np.full(n_days, 1000.0)
        if wavy_amount:
            vol = 1000.0 + 300.0 * np.sin(np.arange(n_days) / 7.0)
        if stub_last:
            close[-1] = close[-2]          # 空壳 bar: OHLC 全等
            vol[-1] = 0.0                  # 且无成交
        pd.DataFrame({"date": dates, "open": close, "high": close,
                      "low": close, "close": close, "volume": vol,
                      "amount": close * vol / 1e4}).to_parquet(
            d / f"{code}.parquet", index=False)
    idx_dates = dates
    m = len(idx_dates)
    for code, base in (("000001.SH", 3000.0), ("000300.SH", 3500.0),
                       ("399006.SZ", 2000.0)):
        close = np.linspace(base, base * 1.1, m)
        pd.DataFrame({"date": idx_dates, "close": close,
                      "volume": np.full(m, 1e6),
                      "amount": np.full(m, 1e5)}).to_parquet(
            d / f"{code}.parquet", index=False)
    return dates


class TestCollect:
    def test_writes_records_and_is_idempotent(self, tmp_path):
        _write_cache()
        r1 = mpr.collect(bars=300, write=True)
        assert r1["ok"] is True
        assert r1["records"] > 200
        first = mpr.DAILY_PATH.read_text(encoding="utf-8")
        assert len(first.strip().splitlines()) == r1["lines"]
        # 连跑第二遍: 行数与内容都不变 (upsert 不追加重复行)
        r2 = mpr.collect(bars=300, write=True)
        assert r2["lines"] == r1["lines"]
        assert mpr.DAILY_PATH.read_text(encoding="utf-8") == first

    def test_stub_last_bar_rolls_back_asof(self, tmp_path):
        """实测场景: 盘前抓数造出末日空壳 bar → 快照日期必须回退。"""
        dates = _write_cache(stub_last=True)
        r = mpr.collect(bars=300, write=True)
        assert r["ok"] is True
        assert r["asof"] == dates[-2].date().isoformat()
        assert r["snapshot"]["date"] == dates[-2].date().isoformat()
        assert r["snapshot"]["breadth"]["traded"] == 6   # 全票都有成交

    def test_stale_flag_when_cache_lags(self, tmp_path):
        _write_cache()
        r = mpr.collect(bars=300, write=True, expected=pd.Timestamp("2030-01-01").date())
        assert r["stale"] is True
        assert r["snapshot"]["stale"] is True
        assert r["snapshot"]["expected_date"] == "2030-01-01"

    def test_fresh_flag_when_matches_expected(self, tmp_path):
        dates = _write_cache()
        r = mpr.collect(bars=300, write=True, expected=dates[-1].date())
        assert r["stale"] is False
        assert r["asof"] == dates[-1].date().isoformat()

    def test_empty_cache_fails_soft(self, tmp_path):
        (tmp_path / "1d").mkdir(parents=True, exist_ok=True)
        r = mpr.collect(bars=300, write=True)
        assert r["ok"] is False
        assert "缓存为空" in r["reason"]

    def test_dry_run_does_not_write(self, tmp_path):
        _write_cache()
        r = mpr.collect(bars=300, write=False)
        assert r["ok"] is True and r["records"] > 0
        assert r["lines"] == 0
        assert not mpr.DAILY_PATH.exists()

    def test_record_shape(self, tmp_path):
        _write_cache()
        rec = mpr.collect(bars=300, write=True)["snapshot"]
        for key in ("date", "expected_date", "stale", "indices", "breadth",
                    "turnover", "limit", "shadow"):
            assert key in rec, key
        for key in ("shanghai", "hs300", "chuangyeban"):
            assert rec["indices"][key]["name"]
        assert set(rec["shadow"]) == set(mpr.SHADOW_RULES)
        # 三条影子规则一律只有 on/off 两态 (2026-09-17 修复: regime 曾落 'range')
        assert all(v in ("on", "off") for v in rec["shadow"].values())

    def test_text_columns_survive_the_record_writer(self, tmp_path):
        """M7 自查抓到的真 bug 的回归锁：文本列不许被 `_f()` 打回 None。

        原写法用 `col != "regime"` 判断文本列，于是新加的 `regime_20` 走了
        `float("bull")` → TypeError 被 `_f` 吞掉 → 返回 None →
        **5501 条记录里 regime_20 全空**（实测 0/5501），静默丢了这一维。
        """
        _write_cache(n_days=400)
        recs = [r for r in mpr.history(limit=0)]
        if not recs:
            recs = [mpr.collect(bars=0, write=True)["snapshot"]]
        rec = mpr.collect(bars=0, write=True)["snapshot"]
        for key in ("shanghai", "hs300", "chuangyeban"):
            it = rec["indices"][key]
            assert it.get("regime") in ("bull", "bear", "range", None)
            # regime_20 不依赖均线 → 400 根样本上必须有值
            assert it.get("regime_20") in ("bull", "bear", "range"), \
                f"{key}.regime_20 = {it.get('regime_20')!r}（文本列被数值转换吃掉了）"
        assert set(mpr.TEXT_COLS) >= {"regime", "regime_20"}

    def test_regime_shadow_rule_is_on_off_not_label(self, tmp_path):
        """回归: regime 规则必须落 on/off, 否则回放按 =='on' 判定永远 0 持仓。"""
        _write_cache()
        recs = mpr.history(limit=0)
        mpr.collect(bars=300, write=True)
        recs = mpr.history(limit=0)
        assert {r["shadow"]["regime"] for r in recs} <= {"on", "off"}


class TestHistoryAndLatest:
    def test_empty_when_no_file(self, tmp_path):
        assert mpr.history() == []
        assert mpr.latest() is None

    def test_bad_lines_skipped(self, tmp_path):
        mpr.DAILY_PATH.parent.mkdir(parents=True, exist_ok=True)
        mpr.DAILY_PATH.write_text(
            '{"date":"2024-01-02","a":1}\nnot json\n\n{"date":"2024-01-03"}\n',
            encoding="utf-8")
        h = mpr.history(limit=0)
        assert [r["date"] for r in h] == ["2024-01-02", "2024-01-03"]
        assert mpr.latest()["date"] == "2024-01-03"

    def test_limit_takes_tail(self, tmp_path):
        mpr.DAILY_PATH.parent.mkdir(parents=True, exist_ok=True)
        rows = "\n".join(json.dumps({"date": f"2024-01-{i:02d}"}) for i in range(1, 11))
        mpr.DAILY_PATH.write_text(rows + "\n", encoding="utf-8")
        assert len(mpr.history(limit=3)) == 3
        assert mpr.history(limit=3)[-1]["date"] == "2024-01-10"


class TestThermometer:
    def test_missing_records_explains_how_to_bootstrap(self, tmp_path):
        md = mpr.thermometer_md()
        assert "还没有连续录像" in md
        assert "market_position_collect.py" in md

    def test_markdown_carries_all_three_caveats_in_plain_chinese(self):
        """§16.9 第 10 条【大白话·硬条款】: 三条已知偏差必须**用大白话**出现。

        原版写的是「生存者偏差」「ST 股 (±5%) 会漏计」—— 用户明说看不懂。
        这条测试同时是**正向锁**（白话解释必须在）与**反向锁**（黑话不许再出现）。
        """
        _write_cache()
        mpr.collect(bars=300, write=True)
        md = mpr.thermometer_md()
        # ① 涨跌停口径
        assert "ST 股" in md and "±5%" in md and "偏低" in md
        # ② 退市股不在数据里 —— 必须说清"方向是高估"和"为什么"
        assert "退市" in md and "高估" in md and "活到今天的公司" in md
        # ③ 十年百分位早年凑不满十年
        assert "凑不满十年" in md and "2016~2019" in md
        # 铁律提示
        assert "不联入任何仓位调度" in md
        # **反向锁**: 这些黑话必须已经被翻译掉
        # **审计 F2 修正**: 原来锁的是 "Newey-West (HAC) t 值" 这个**整串** ——
        # 它在我们改写口径后已经不存在了, 于是这条锁**永远不会失败、也拦不住任何回归**
        # (实测锁的串恒 False, 而 "Newey-West" 与 "t 值" 两个黑话仍在正文)。
        # 改成锁**词根**: 只要出现这些词就是没翻译干净。
        for jargon in ("生存者偏差", "Newey-West", "HAC", "T+1 生效", "秩相关",
                       "MA250", "n_eff", "DSR", "PBO", "akshare", "stock_ebs_lg",
                       "PE-TTM"):
            assert jargon not in md, f"大白话条款要求翻译黑话，但报告里还有：{jargon}"

    def test_markdown_explains_every_number_in_the_headline(self):
        """§16.9 第 10 条: 每个数字后面必须跟一句白话说明它意味着什么。"""
        _write_cache()
        mpr.collect(bars=300, write=True)
        md = mpr.thermometer_md()
        head = md.split("## 位置")[0]
        assert "先说人话" in head
        # 宽度、新高低、量能、涨跌停 四个数字都要有解释
        assert "均线下方" in head and "换句话说" in head
        assert "倍" in head and ("偏弱" in head or "向下" in head or "更多" in head)
        assert ("冷到" in head or "热闹" in head or "活跃" in head)
        assert "情绪" in head or "方向" in head

    def test_plain_language_helpers_are_generated_not_hardcoded(self):
        """人话必须由数字生成 —— 写死"八成"会在宽度变了之后变成假话。"""
        assert "八成" not in mpr._width_plain(85)
        assert "绝大多数" in mpr._width_plain(85)
        assert "不到六分之一" in mpr._width_plain(5)
        assert "算不出" in mpr._width_plain(None)
        assert "一边倒地向下" in mpr._hl_plain(0, 30)
        assert "0.0 倍" not in mpr._hl_plain(0, 30)
        assert "冷到了地板上" in mpr._amount_plain(1.6)
        assert "非常活跃" in mpr._amount_plain(90)
        assert "下跌" not in mpr._amount_plain(None) or "算不出" in mpr._amount_plain(None)
        assert "情绪偏多" in mpr._zdt_plain({"up": 60, "down": 5})
        assert "情绪偏空" in mpr._zdt_plain({"up": 3, "down": 40})
        assert "没有极端情绪" in mpr._zdt_plain({"up": 0, "down": 0})
        assert mpr._position_plain(90.9) == "（偏贵区）"
        assert mpr._position_plain(5) == "（便宜区）"

    def test_markdown_reports_stale_date(self, tmp_path):
        _write_cache()
        mpr.collect(bars=300, write=True, expected=pd.Timestamp("2030-01-01").date())
        assert "数据滞后" in mpr.thermometer_md()


class TestMirrorAndShadow:
    def test_mirror_needs_enough_records(self, tmp_path):
        mpr.DAILY_PATH.parent.mkdir(parents=True, exist_ok=True)
        mpr.DAILY_PATH.write_text(
            "\n".join(json.dumps({"date": f"2024-01-{i:02d}"}) for i in range(1, 11))
            + "\n", encoding="utf-8")
        r = mpr.mirror()
        assert r["ok"] is False
        assert "--backfill" in r["reason"]

    def test_shadow_replay_needs_enough_records(self, tmp_path):
        assert mpr.shadow_replay()["ok"] is False

    def test_mirror_and_shadow_on_synthetic_cache(self, tmp_path):
        _write_cache(n_days=400)
        mpr.collect(bars=0, write=True)
        recs = mpr.history(limit=0)
        assert len(recs) > 300
        md = mpr.mirror(top_n=3)
        # 合成数据里指数只有 400 根 (不足 750), 十年分位为 None → 照镜子明确报不可用
        # **审计 F2 修正**: 原来写 `assert "ok" in md` —— 那判的是字典**键**存在性,
        # 而 mirror() 所有返回路径都带 ok 键 → **恒真, 一个字都没验证**。
        assert md["ok"] is False, "400 根指数样本给不出十年分位, 照镜子应当明确报不可用"
        assert md["reason"], "不可用必须给出原因, 不能只有个 False"
        sd = mpr.shadow_replay()
        assert sd["ok"] is True, sd.get("reason")
        assert {r["rule"] for r in sd["rows"]} == set(mpr.SHADOW_RULES)
        # 年化分母必须按真实日历跨度, 不能拿录像条数当分母
        assert sd["years"] > 0 and sd["days"] > 0
        assert sd["days"] > sd["years"] * 200, "交易日数与年数应当量级自洽"
        # 每条规则都要有毛/净两组数 + 显著性字段(否则就是半个功能)
        for r in sd["rows"] + [sd["buy_hold"]]:
            assert r["net"]["annualized_pct"] is not None
            assert r["windows"]["in"], "双窗口结果不许为空"


class TestMirrorCaliber:
    """§14.2 / §14.3 / §14.9: 结论基于分位带 + 按年份拆解 + 单一年份主导必须点名。"""

    def _patch_band(self, monkeypatch, band):
        fake = {"picks": band[:1], "band": band, "n_band": len(band),
                "eligible": 900, "exclude_recent": 252, "min_gap": 20,
                "quantile": 0.05}
        # 2026-09-19 批次 5.1 第四刀: mirror 已搬到 core/market_mirror ——
        # **patch 实现所在模块** (mpr.similar_days 是转发出来的, patch 它不会
        # 影响实现: 实测两例红, 症状是"六特征齐全 0 条")。
        from core import market_mirror as _mm
        monkeypatch.setattr(_mm, "similar_days", lambda *a, **k: fake)

    def test_mirror_names_dominating_year(self, tmp_path, monkeypatch):
        _write_cache(n_days=400)
        mpr.collect(bars=0, write=True)
        self._patch_band(monkeypatch, [
            {"date": "2020-03-02", "distance": 0.10},
            {"date": "2020-03-05", "distance": 0.11},
            {"date": "2020-03-09", "distance": 0.12},
            {"date": "2021-07-05", "distance": 0.30}])
        md = mpr.mirror()
        assert md["ok"], md.get("reason")
        assert [y["year"] for y in md["years"]] == ["2020", "2021"]
        assert md["years"][0]["n"] == 3
        assert md["dominance"] == "2020"
        assert "本结论由 2020 年主导" in md["warning"]

    def test_mirror_no_dominance_when_spread_evenly(self, tmp_path, monkeypatch):
        _write_cache(n_days=400)
        mpr.collect(bars=0, write=True)
        self._patch_band(monkeypatch, [
            {"date": "2019-03-02", "distance": 0.10},
            {"date": "2019-03-05", "distance": 0.11},
            {"date": "2021-07-05", "distance": 0.30},
            {"date": "2021-07-09", "distance": 0.31}])
        md = mpr.mirror()
        assert md["dominance"] is None
        assert "主导" not in md["warning"]

    def test_mirror_reports_when_band_is_unusable(self, tmp_path):
        """合成缓存只有 400 天且十年分位为 None → 明确说"不可用 + 怎么办"。"""
        _write_cache(n_days=400)
        mpr.collect(bars=0, write=True)
        md = mpr.mirror(top_n=3)
        assert md["ok"] is False
        assert "252" in md["reason"] and "回填" in md["reason"]


class TestMomentumBuckets:
    """前期12月涨跌 → 未来12月收益 分桶 (2026-09-17, 926 号回测复核产物)。"""

    def test_three_indices_side_by_side(self, tmp_path):
        _write_cache(n_days=800)   # ~38 个月, 够 12+12 窗口出样本
        out = mpr.momentum_buckets()
        assert out["ok"] is True
        assert out["warning"] == mpr.MOMENTUM_BUCKET_WARNING
        assert len(out["indices"]) == 3
        for idx in out["indices"]:
            assert idx["ok"] is True
            assert len(idx["buckets"]) == 6
            assert idx["n_samples"] > 0
            assert idx["current"]["bucket"] is not None
            # 逐月线性上涨 → 前期12月恒正 → 样本全在「涨」侧三个桶
            assert sum(b["n"] for b in idx["buckets"][:3]) == 0

    def test_empty_cache_fails_soft(self, tmp_path):
        out = mpr.momentum_buckets()
        assert out["ok"] is False
        assert "缓存" in out["reason"]
        assert out["warning"]          # 警告文案即便失败也带上 (页面展示用)

    def test_warning_is_plain_chinese(self):
        """大白话硬条款: 警告文案不许出现统计黑话。"""
        w = mpr.MOMENTUM_BUCKET_WARNING
        for jargon in ("N_eff", "Spearman", "显著", "p 值", "p值"):
            assert jargon not in w


class TestStatsHelpers:
    """§15.1 E1/E2 + §16.7 MED-4: HAC t 值 / 持有段 / 段收益口径。"""

    def test_hac_matches_manual_newey_west(self):
        rng = np.random.default_rng(11)
        x = pd.Series(rng.normal(0.001, 0.01, 400))
        got = mpr._hac_tstat(x, lags=5)
        d = x - x.mean()
        nw = float((d * d).mean())
        for k in range(1, 6):
            # 手写循环按**位置**配对 (pandas 的 iloc 乘法会按索引对齐, 不能用)
            nw += 2.0 * (1.0 - k / 6.0) * float((d.iloc[k:].to_numpy()
                                                 * d.iloc[:-k].to_numpy()).mean())
        expect_se = (nw / len(x)) ** 0.5
        assert got["se"] == pytest.approx(expect_se, rel=1e-9)
        assert got["t"] == pytest.approx(x.mean() / expect_se, rel=1e-9)
        assert got["ci_low"] < x.mean() < got["ci_high"]
        assert got["n_eff"] == pytest.approx(len(x) / got["vif"])

    def test_hac_inflates_variance_under_autocorrelation(self):
        """正自相关序列必须被膨胀 —— 否则普通 t 检验会把显著性吹大。"""
        rng = np.random.default_rng(3)
        e = rng.normal(0.0, 0.01, 2000)
        ar = np.zeros(2000)
        for i in range(1, 2000):
            ar[i] = 0.8 * ar[i - 1] + e[i]
        got = mpr._hac_tstat(pd.Series(ar))
        assert got["vif"] > 2.0, f"AR(1) 方差膨胀因子应远大于 1, 实际 {got['vif']}"
        assert got["n_eff"] < 1000
        assert mpr._hac_tstat(pd.Series(e))["vif"] < 1.2, "白噪声不该被膨胀"

    def test_hac_returns_none_on_thin_or_constant(self):
        assert mpr._hac_tstat(pd.Series([0.01] * 30)) is None   # 常量 → 方差 0
        assert mpr._hac_tstat(pd.Series([0.01] * 5)) is None    # 样本不足
        assert mpr._hac_tstat(pd.Series([], dtype=float)) is None

    def test_holding_segments_counts_runs(self):
        pos = pd.Series([0, 0, 1, 1, 1, 0, 1, 0, 0, 1, 1],
                        index=pd.date_range("2024-01-01", periods=11, freq="B"))
        assert mpr._holding_segments(pos) == [(2, 4, 3), (6, 6, 1), (9, 10, 2)]
        assert mpr._holding_segments(pd.Series([1.0, 1.0])) == [(0, 1, 2)]
        assert mpr._holding_segments(pd.Series([0.0, 0.0])) == []

    def test_segment_stats_does_not_normalise_by_length(self):
        """§16.7 MED-4 实测修正: 段收益**不做长度归一化**。

        除以段长会给长段(赢家)打折、却不给短段(输家)打折 —— 实测能把结论的符号
        弄反。这里两段都是 +5% (1 天 / 99 天): 不归一化 → 平均就是 5%;
        若按日均归一化 → 会掉到约 2.5%, 两者可区分。
        """
        idx = pd.date_range("2024-01-01", periods=101, freq="B")
        r = pd.Series(0.0, index=idx)
        r.iloc[1] = 0.05                                   # 1 天段: +5%
        r.iloc[2:] = (1.05 ** (1.0 / 99)) - 1.0            # 99 天段: 合计 +5%
        st = mpr._segment_stats(r, [(1, 1, 1), (2, 100, 99)], 0.0)
        assert st["mean_return_pct"] == pytest.approx(5.0, abs=0.01)
        assert st["median_return_pct"] == pytest.approx(5.0, abs=0.01)
        assert st["n"] == 2 and st["min_days"] == 1 and st["max_days"] == 99
        assert st["t"] is None and "样本不足" in st["note"]
        assert st["t_gross"] is None

    def test_segment_stats_subtracts_cost_per_round_trip(self):
        idx = pd.date_range("2024-01-01", periods=40, freq="B")
        r = pd.Series(0.0, index=idx)
        for a in range(1, 40, 4):
            r.iloc[a:a + 2] = 0.01
        segs = mpr._holding_segments((r != 0).astype(float))
        assert len(segs) == 10
        st0 = mpr._segment_stats(r, segs, 0.0)
        st1 = mpr._segment_stats(r, segs, 0.0011)
        assert st1["mean_return_pct"] < st0["mean_return_pct"]
        assert st1["mean_return_pct"] == pytest.approx(
            st0["mean_return_pct"] - 0.11, abs=0.02)

    def test_cost_params_read_from_engine_not_hardcoded(self):
        """成本口径不许写第二份 —— 必须等于 backtest/engine.py 的默认值。"""
        from backtest.engine import BacktestEngine
        e = BacktestEngine({})
        c = mpr._cost_params()
        assert c["commission"] == e.commission
        assert c["stamp_tax"] == e.stamp_tax
        assert c["slippage"] == e.slippage
        assert c["round_trip"] == pytest.approx(e.commission * 2 + e.stamp_tax)
        assert c["round_trip_with_slippage"] > c["round_trip"]


class TestShadowReplayCaliber:
    """§16.2 / §15.1 / §15.4: 毛净并列 / 显著性 / 双窗口 / 措辞口径。"""

    def _run(self):
        _write_cache(n_days=400)
        mpr.collect(bars=0, write=True)
        return mpr.shadow_replay()

    def test_gross_and_net_side_by_side(self):
        sd = self._run()
        assert sd["ok"], sd.get("reason")
        assert sd["cost"]["round_trip_pct"] == pytest.approx(0.11, abs=0.005)
        assert sd["cost"]["round_trip_with_slippage_pct"] > sd["cost"]["round_trip_pct"]
        for r in sd["rows"] + [sd["buy_hold"]]:
            assert r["net"]["annualized_pct"] <= r["annualized_pct"] + 1e-9
            assert r["round_trips"] >= 1
            assert "没扣滑点" in sd["caliber"] and "偏乐观" in sd["caliber"]

    def test_every_row_has_significance_and_windows(self):
        sd = self._run()
        for r in sd["rows"]:
            assert {"t", "ci_low_pct", "ci_high_pct", "n_eff"} <= set(r["net"])
            assert r["net"]["ci_low_pct"] < r["net"]["ci_high_pct"]
            assert 0 < r["net"]["n_eff"] <= r["days"]
            w = r["windows"]
            assert set(w) >= {"in", "out", "consistent"}
            assert w["in"]["annualized_pct"] is not None
            assert w["out"]["annualized_pct"] is not None
            assert w["consistent"] in (True, False)
            seg = r["segments"]
            assert seg["n"] == r["round_trips"]
            if seg["n"] < mpr.MIN_SEGMENTS_FOR_T:
                assert seg["t"] is None and "样本不足" in seg["note"]

    def test_net_deducts_exactly_one_round_trip_per_segment(self):
        """净口径总成本 = 建仓次数 × 单次往返 (构造一个必然开过仓的场景)。"""
        sd = self._run()
        r = sd["rows"][0]
        gross_total = r["total_pct"]
        net_total = r["net"]["total_pct"]
        expect = r["round_trips"] * sd["cost"]["round_trip_pct"]
        assert (gross_total - net_total) == pytest.approx(expect, abs=1.0)


def _write_erp(n=900, start="2020-01-01"):
    """造一个最小 ERP (股债性价比) 缓存 —— 纯本地, 不联网。"""
    mpr.ERP_PATH.parent.mkdir(parents=True, exist_ok=True)
    idx = pd.date_range(start, periods=n, freq="B")
    rows = [json.dumps({"date": d.date().isoformat(),
                        "erp": round(0.05 + 0.001 * (i % 10), 6)})
            for i, d in enumerate(idx)]
    mpr.ERP_PATH.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return idx


def _amount_pct(rec):
    return (rec.get("turnover") or {}).get("amount_pct_1y")


class TestWarmup:
    """预热窗口 —— 2026-09-17 实测抓到的真 bug 的回归锁。

    原实现: 日常采集 `collect(bars=300)` 把日线截断成 300 根, 而
    `amount_pct_1y` 的 `rolling(250, min_periods=120)` 在窗口头部 120 天算不出
    → 那些记录被 upsert **覆盖成缺值** (实测 118 条历史记录的 `amount_pct_1y`
    变 null, 只坏不好)。修法 = 多读 `WARMUP_BARS` 做预热, 只输出窗口内最后 bars 天。
    """

    def test_daily_collect_does_not_degrade_history(self):
        _write_cache(n_days=800)
        mpr.collect(bars=0, write=True)
        before = {r["date"]: r for r in mpr.history(limit=0)}
        assert len(before) > mpr.DEFAULT_BARS + mpr.WARMUP_BARS
        mpr.collect(bars=mpr.DEFAULT_BARS, write=True)
        after = {r["date"]: r for r in mpr.history(limit=0)}
        assert set(after) == set(before), "日常采集不许新增或丢掉录像行"
        bad = [d for d in before
               if _amount_pct(before[d]) is not None
               and _amount_pct(after[d]) is None]
        assert not bad, \
            f"日常采集把 {len(bad)} 条历史的成交额百分位改成了空: {bad[:5]} …"

    def test_daily_collect_emits_only_the_window(self):
        """预热段不算正式记录 —— 输出的记录数不许超过 bars。"""
        _write_cache(n_days=800)
        res = mpr.collect(bars=200, write=True)
        assert res["ok"] and res["records"] <= 200

    def test_warmup_covers_the_longest_lookback(self):
        """预热必须覆盖最长的回看窗口 (成交额一年百分位 250 根)。"""
        assert mpr.WARMUP_BARS >= mpr.AMOUNT_RANK_WINDOW
        assert mpr.AMOUNT_RANK_WINDOW > mpr.AMOUNT_RANK_MIN_PERIODS, \
            "min_periods 比窗口小, 正是这条预热的由来"

    def test_warmup_values_are_identical_not_merely_non_null(self):
        """审计 F-07：预热不足时数值会**静默漂移**（不是变 null），所以要比数值。

        `amount_pct_1y` = `rolling(250, min_periods=120).rank(pct=True)`：满 120 根
        就有数，但那是"在 120 个值里排名"，**是错的**，要满 250 根才对。
        原来那条测试只断言"不许变 null"，抓不到这种漂移。

        两段：
          ① 预热 300（实际值）与预热 900（远超任何回看窗）→ 逐日**完全相同**；
          ② 预热 120（不足）与预热 900 → **必须有差异**（反锁：证明这条测试有牙，
             将来有人把 WARMUP_BARS 调小到 min_periods 附近就会红）。
        """
        _write_cache(n_days=900, wavy_amount=True)
        got = {}
        from core import market_position_io as mpio

        def run(w):
            # 2026-09-20 审计 P2-6: 落盘实现在基座 mpio —— patch 点必须跟着实现走
            # (patch mpr._upsert 从"唯一实现"改成"静默 no-op"了)
            orig = mpio._upsert
            mpr.WARMUP_BARS = w
            mpio._upsert = lambda records, path=None: got.update(
                {r["date"]: _amount_pct(r) for r in records}) or len(records)
            try:
                mpr.collect(bars=mpr.DEFAULT_BARS, write=True)
            finally:
                mpio._upsert = orig
            return dict(got)

        base = run(900)
        enough = run(mpr.WARMUP_BARS)
        short = run(mpr.AMOUNT_RANK_MIN_PERIODS)
        common = [d for d in base if d in enough and base[d] is not None]
        assert len(common) > 50, f"可比对的交易日太少: {len(common)}"
        bad = [d for d in common if enough[d] != base[d]]
        assert not bad, f"预热 {mpr.WARMUP_BARS} 根仍与长预热不一致: {bad[:5]} …"
        # 反锁：预热只到 min_periods 时必然对不上（否则这条测试是空转的）
        short_common = [d for d in base if d in short and base[d] is not None]
        off = [d for d in short_common if short[d] != base[d]]
        assert off, "预热降到 min_periods 竟然还对得上 —— 这条测试没牙，得重写"


class TestErpValuation:
    """估值维度 (计划书 §14.4): ERP 股债性价比。全程用本地造的缓存, 不联网。"""

    def test_snapshot_percentile_and_range(self):
        idx = _write_erp(n=900)
        tbl = mpr._erp_table()
        snap = mpr._erp_snapshot(tbl, idx[-1])
        assert snap, "历史够 750 条就必须给数"
        assert snap["n_obs"] >= mpr.ERP_MIN_OBS
        assert 0 <= snap["erp_pct_10y"] <= 100
        assert snap["erp_min_10y_pct"] <= snap["erp_pct"] <= snap["erp_max_10y_pct"]
        assert "沪深300" in snap["caliber"], "口径必须写清是沪深300, 不是全市场"
        assert snap["source"] and snap["asof"]

    def test_history_too_short_gives_no_number(self):
        idx = _write_erp(n=300)
        tbl = mpr._erp_table()
        assert mpr._erp_snapshot(tbl, idx[-1]) is None, "不足 3 年不给数 (不编)"

    def test_missing_erp_file_is_not_faked(self):
        assert len(mpr._read_erp()) == 0
        assert mpr._erp_snapshot(mpr._erp_table(), "2026-09-15") is None

    def test_fetch_can_be_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv(mpr.ERP_FETCH_ENV, "1")
        assert mpr._erp_fetch_enabled() is False
        r = mpr._refresh_erp()
        assert r["ok"] is False and mpr.ERP_FETCH_ENV in r["reason"]

    def test_bad_lines_skipped_not_fatal(self):
        mpr.ERP_PATH.parent.mkdir(parents=True, exist_ok=True)
        mpr.ERP_PATH.write_text(
            "not json\n{\"date\":\"2020-01-02\",\"erp\":0.05}\n{\"date\":\"2020-01-03\"}\n",
            encoding="utf-8")
        s = mpr._read_erp()
        assert len(s) == 1 and float(s.iloc[0]) == pytest.approx(0.05)

    def test_collect_attaches_valuation_and_markdown_shows_it(self):
        _write_cache(n_days=400)
        _write_erp(n=900, start="2015-01-01")
        res = mpr.collect(bars=300, write=True)
        rec = res["snapshot"]
        assert rec.get("valuation"), "有 ERP 缓存时记录必须带上估值"
        md = mpr.thermometer_md()
        assert "估值（贵不贵" in md
        assert "股债性价比" in md and "十年" in md
        assert "沪深300 口径" in md

    def test_markdown_says_missing_when_no_erp(self):
        _write_cache(n_days=400)
        mpr.collect(bars=300, write=True)
        md = mpr.thermometer_md()
        assert "【缺】没有本地 ERP" in md


class TestRegimeEpisodes:
    """牛熊区间与时长 (计划书 §14.5): 单点状态答不了"这轮走了多久"。"""

    def test_episodes_split_runs(self):
        idx = pd.date_range("2024-01-01", periods=8, freq="B")
        lab = pd.Series(["range", "range", "bull", "bull", "bull", "bear", "bear", "bear"],
                        index=idx)
        eps = mpr._regime_episodes(lab)
        assert [(e["state"], e["start_i"], e["end_i"]) for e in eps] == [
            ("range", 0, 1), ("bull", 2, 4), ("bear", 5, 7)]

    def test_episodes_skip_none_labels(self):
        idx = pd.date_range("2024-01-01", periods=4, freq="B")
        lab = pd.Series([None, None, "bull", "bull"], index=idx)
        eps = mpr._regime_episodes(lab)
        assert len(eps) == 1 and eps[0]["state"] == "bull" and eps[0]["start_i"] == 2

    def test_summary_counts_months_and_marks_ongoing(self):
        idx = pd.date_range("2024-01-01", periods=6, freq="B")
        lab = pd.Series(["bull", "bull", "bull", "bear", "bear", "bear"], index=idx)
        px = pd.Series([100.0, 110.0, 120.0, 110.0, 100.0, 90.0], index=idx)
        s = mpr._regime_summary(lab, px, caliber="测试口径")
        assert s["state"] == "bear" and s["since"] == "2024-01-04"
        assert s["n_episodes"] == 2 and s["n_same_state"] == 0
        assert s["months"] > 0 and s["ret_pct"] is not None
        assert s["median_months"] is None, "没有历史同状态段 → 中位数不给数 (不编)"

    def test_flicker_flagged_when_episodes_are_tiny(self):
        """日频抖动太勤的口径必须被标记 (实测年线口径中位只有 0.2 个月)。"""
        idx = pd.date_range("2024-01-01", periods=600, freq="B")
        states = ["bull" if i % 2 == 0 else "bear" for i in range(600)]
        lab = pd.Series(states, index=idx)
        px = pd.Series(range(100, 700), index=idx, dtype=float)
        s = mpr._regime_summary(lab, px, caliber="抖动口径")
        assert s["too_flickery"] is True and s["flicker_note"]
        assert s["n_episodes"] > 100

    def test_longest_rows_capped(self):
        idx = pd.date_range("2024-01-01", periods=200, freq="B")
        lab = pd.Series(["bull" if (i // 5) % 2 == 0 else "bear" for i in range(200)],
                        index=idx)
        px = pd.Series(range(100, 300), index=idx, dtype=float)
        s = mpr._regime_summary(lab, px, caliber="测试")
        assert len(s["longest_rows"]) <= 8


class TestDimensionValidity:
    """维度体检 (计划书 §14.7 + §16.1/§16.3/§16.5)。"""

    def test_needs_enough_history(self):
        _write_cache(n_days=400)
        mpr.collect(bars=0, write=True)
        r = mpr._dimension_validity()
        assert r["ok"] is False
        assert "500" in r["reason"]

    def test_runs_and_carries_the_discipline_notes(self):
        _write_cache(n_days=1200)
        mpr.collect(bars=0, write=True)
        r = mpr._dimension_validity()
        assert r["ok"], r.get("reason")
        assert r["n_months"] >= mpr.VALIDITY_MIN_MONTHS
        assert r["rows"], "至少要产出一行体检结果"
        for row in r["rows"]:
            assert set(row) >= {"field", "family", "horizon", "n", "n_eff",
                                "rho", "rho_in", "rho_out", "consistent", "verdict"}
            assert row["n_eff"] is not None and row["n_eff"] > 0
            assert row["verdict"]
        # 纪律 3「数族不数因子」: 结果里必须有族, 且披露共检验多少组合
        assert r["n_families"] >= 2 and r["n_tests"] == len(r["rows"])
        notes = " ".join(r["limitations"])
        assert "幸存者偏差" in notes, "§16.5 要求把幸存者偏差写成显式限制"
        # 审计 M7：原来断言字面 "DSR" —— 但那三个字母已按"用户可见文案不许出现
        # 技术名词"的要求翻成白话「多重检验校正」，所以改成锁**语义**。
        assert "多重检验校正" in notes, "必须说明为何不做多重检验校正"
        assert "不作预测依据" in notes and "不合成总分" in notes, "§16.4 措辞"

    def test_erp_absent_when_no_erp_cache(self):
        """没有 ERP 缓存 → ERP 那一行必须缺席, 不能拿别的列冒充。"""
        _write_cache(n_days=1200)
        mpr.collect(bars=0, write=True)
        r = mpr._dimension_validity()
        assert r["ok"]
        assert all(row["field"] != "erp" for row in r["rows"])

    def test_erp_included_when_cache_present(self):
        _write_cache(n_days=1200)
        _write_erp(n=1200, start="2024-01-01")
        mpr.collect(bars=0, write=True)
        r = mpr._dimension_validity()
        assert r["ok"], r.get("reason")
        assert any(row["field"] == "erp" for row in r["rows"])

    def test_markdown_has_the_validity_section(self):
        _write_cache(n_days=1200)
        mpr.collect(bars=0, write=True)
        md = mpr.thermometer_md()
        assert "## 指标体检" in md
        assert "仅描述现状，不作预测依据" in md or "可用（样本内" in md

    def test_markdown_has_the_regime_section(self):
        _write_cache(n_days=400)
        mpr.collect(bars=300, write=True)
        md = mpr.thermometer_md()
        assert "## 牛熊区间" in md and "20% 法则" in md and "年线" in md

    def test_validity_numbers_follow_the_data_not_the_source_code(self):
        """审计 F-06/F-08 回归锁：正文里的统计结论**必须跟着数据走**。

        两段断言，各治一种"写死"：

        ① **「几件事」那段**（在 `thermometer_md` 里现算）—— 用"偷梁换柱"（与
           `test_reuses_trade_analysis_month_view` 同一手法）：真跑一遍拿到体检结果，
           把成绩改成一组**真实数据里绝不可能出现**的数（rho `+0.99` / `-0.98`、
           五分位差 `88.8` / `-77.7`）再注入渲染，正文必须跟着变。
           原来正文写死的是「rho +0.55 / −0.57」这类数字，在这条断言下必然露馅。
        ② **「诚实限制」那段**（在 `_dimension_validity` 里就拼好了）—— 把它的数字
           与同一份结果里的 `rows` 对账：有效独立样本的区间必须等于最长持有期那一批的
           最小/最大值。原来写死的是「各指标 7.6~12.3」，而实测是 9.6~21.6。
        """
        _write_cache(n_days=1200)
        _write_erp(n=1200, start="2024-01-01")
        mpr.collect(bars=0, write=True)
        vd = mpr._dimension_validity()
        assert vd["ok"], vd.get("reason")
        assert len(vd["summary"]) >= 2, "这条测试要至少两个指标才能摆出正负两端"
        notes = " ".join(vd["limitations"])

        # ② 先对账"诚实限制"（用真数据，不注入）—— 区间必须与 rows 算出来的一致
        longest = max(r["horizon_days"] for r in vd["rows"])
        eff = [r["n_eff"] for r in vd["rows"]
               if r["horizon_days"] == longest and r["n_eff"] is not None]
        assert f"{min(eff)} ~ {max(eff)} 份" in notes, \
            f"有效独立样本区间必须由 rows 现算（最长持有期 {longest}）：{notes}"
        assert "7.6~12.3" not in notes and "7.6 ~ 12.3" not in notes, \
            "写死的那对数字必须彻底消失"

        # ① 再验「几件事」那段跟着注入的数据走
        fake = json.loads(json.dumps(vd))            # 深拷贝（全是纯类型）
        by_field = {s["field"]: s for s in fake["summary"]}
        # 挑两个"在最长的那个持有期上真的有结果"的指标来当正负两端 ——
        # 短历史的指标（十年百分位要 3 年预热）在最长的档上可能一行都没有。
        have = [f for f in by_field
                if any(r["field"] == f and r["horizon_days"] == longest
                       for r in fake["rows"])]
        assert len(have) >= 2, "这条测试要至少两个指标在最长持有期上有结果"
        s1, s2 = by_field[have[0]], by_field[have[1]]
        for s, rho, spread in ((s1, 0.99, 88.8), (s2, -0.98, -77.7)):
            s.update(rho_12m=rho, p_12m=0.0001, any_significant_consistent=True,
                     spread=spread, best_horizon="12 个月")
        for s in fake["summary"]:                     # 其余压成不显著，别抢"最强"的位置
            if s["field"] not in (s1["field"], s2["field"]):
                s["p_12m"], s["any_significant_consistent"] = 0.9, False

        md = mpr.thermometer_md(validity_data=fake)
        # 只截「几件事」那一段来断言：表格里的 rho 格子也会印 +0.99，混在一起会掩盖
        # 「正文是不是写死的」这个问题。
        i = md.find("**这次实测出来的几件事**")
        assert i >= 0, "正文必须保留这一段"
        items = md[i:md.find("**诚实限制", i)]
        assert "+0.99" in items and "-0.98" in items, \
            f"正文没跟着注入的数据变 —— 这几个数字是写死在源码里的：{items}"
        assert "88.8" in items and "77.7" in items, f"五分位差也必须现算：{items}"


class TestCacheLayoutContract:
    def test_parquet_path_matches_kline_cache_layout(self, tmp_path):
        """直读 parquet 是性能取舍 —— 靠这条契约测试防缓存布局改了静默读空。

        断言: 本项目拼出的 <缓存根>/1d/<code>.parquet 与 KlineCache._parquet_path
        生成的是**同一个路径**。
        """
        from core.kline_cache import KlineCache
        kc = KlineCache(mpr.KLINE_1D_DIR.parent, tdx_fetcher=lambda *a, **k: {},
                        calendar_fetcher=lambda: [])
        for code in ("600000.SH", "000001.SZ", "399006.SZ"):
            assert mpr.KLINE_1D_DIR / f"{code}.parquet" == kc._parquet_path(code, "1d")


class TestPushFailsSoft:
    def test_push_without_webhook_does_not_raise(self, tmp_path, monkeypatch):
        _write_cache()
        mpr.collect(bars=300, write=True)
        import tools.send_report_feishu as srf

        def _boom():
            raise SystemExit("找不到 FEISHU_WEBHOOK_URL")
        monkeypatch.setattr(srf, "load_webhook", _boom)
        r = mpr.push_thermometer()
        assert r["ok"] is False
        assert "FEISHU_WEBHOOK_URL" in r["reason"]

    def test_push_with_no_record(self, tmp_path):
        r = mpr.push_thermometer()
        assert r["ok"] is False
        assert "还没有连续录像" in r["reason"]


class TestCrossProcessWriteLock:
    """跨进程写锁 (2026-09-19 批次 4.4)。

    背景: daily.jsonl 的写者跨两个进程 (scheduler 三个 job + server 手动 collect),
    `_COLLECT_LOCK` 只是 threading.Lock 跨进程无效, 原来靠原子写 last-write-wins
    兜底 —— 两个进程同时读-改-写会丢记录。现在 _upsert 拿文件锁, 超时明确报错。
    """

    def test_upsert_blocks_when_lock_held(self, tmp_path, monkeypatch):
        p = tmp_path / "daily.jsonl"
        p.write_text("", encoding="utf-8")
        # 2026-09-19 批次 5.1: _upsert/_UPSERT_LOCK_TIMEOUT 已搬到基座
        # core/market_position_io —— **patch 实现所在模块**, 不是 runner 转发名
        from core import market_position_io as mpio
        monkeypatch.setattr(mpio, "_UPSERT_LOCK_TIMEOUT", 0.3)
        # 同进程另开一个句柄持锁 —— Windows 文件锁按句柄生效, 会真冲突
        with mpr._cross_process_lock(p):
            with pytest.raises(TimeoutError, match="跨进程写锁超时"):
                mpr._upsert([{"date": "2026-09-18", "x": 1}], path=p)

    def test_upsert_works_after_lock_released(self, tmp_path):
        p = tmp_path / "daily.jsonl"
        n = mpr._upsert([{"date": "2026-09-18", "x": 1}], path=p)
        assert n == 1 and p.with_suffix(".jsonl.lock").exists()
        # 再来一次: 锁已释放, 同日覆盖不追加
        n2 = mpr._upsert([{"date": "2026-09-18", "x": 2}], path=p)
        assert n2 == 1
        assert json.loads(p.read_text(encoding="utf-8").strip())["x"] == 2

    def test_collect_reports_lock_conflict_as_reason(self, tmp_path, monkeypatch):
        """collect 拿到锁冲突 → ok False + 人话 reason (不是抛栈)。"""
        _write_cache()
        from core import market_position_io as mpio
        monkeypatch.setattr(mpio, "_UPSERT_LOCK_TIMEOUT", 0.3)

        def _conflicted(records, path=None):
            raise TimeoutError("跨进程写锁超时 (0s): daily.jsonl.lock 被另一个进程持有")
        # 2026-09-20 审计 P2-6: 落盘实现在基座, patch 点跟着实现走
        monkeypatch.setattr(mpio, "_upsert", _conflicted)
        r = mpr.collect(bars=60, write=True)
        assert r["ok"] is False
        assert "另一个进程正在采集" in r["reason"]


class TestIsolationGuard:
    """隔离守卫 (2026-09-19 批次 5.1 抽底座时新增)。

    底座把路径常量集中到 `core/market_position_io` 后, 风险从"忘了隔离"变成
    "隔离打在了旧位置" —— 只 patch runner 而实现读基座, 测试就会写**生产**
    data/market_position/daily.jsonl (2026-07-27 缓存投毒、2026-09-17 向量索引
    投毒同一病类)。本测试直接断言: 测试期间三个路径**都**在 tmp 下。
    """

    def test_paths_are_isolated_into_tmp(self):
        """2026-09-20 审计 P2-8: 五条落盘路径全都要在 tmp 里。

        原先只查三条 (daily/kline/erp), 漏了**同目录**的 dashboard.jsonl 与
        events.jsonl —— 仪表盘刷新与事件录入在测试里真跑就会写生产文件
        (latent 投毒面: 当时用例恰好不写, 所以一直没爆)。新增落盘路径必须
        一并进全局隔离。
        """
        from core import market_dashboard_runner as mdbr
        from core import market_events as mev
        from core import market_position_io as mpio
        prod = Path(__file__).resolve().parents[1] / "data"
        for name, val in (("mpio.DAILY_PATH", mpio.DAILY_PATH),
                          ("mpio.KLINE_1D_DIR", mpio.KLINE_1D_DIR),
                          ("mpr.ERP_PATH", mpr.ERP_PATH),
                          ("mev.EVENTS_PATH", mev.EVENTS_PATH),
                          ("mdbr.DASHBOARD_PATH", mdbr.DASHBOARD_PATH)):
            p = Path(val).resolve()
            assert prod not in p.parents and p != prod, (
                f"{name} 指向生产目录 ({p}) —— conftest 的隔离没生效, "
                "跑 collect 会把假数据写进真实录像")

    def test_no_forwarded_name_is_materialized_on_runner(self):
        """防 shadow 守卫: runner 模块**不许真的有**这些转发名。

        坑 (2026-09-19 实测): `monkeypatch.setattr(mpr, "DAILY_PATH", …)` 在撤销时
        会把转发出来的值**实体化成真实属性**, 从此永久 shadow 模块级 __getattr__
        —— 之后"写 tmp、读生产"或反之, 正是投毒型事故的病根。要 patch 就打基座。
        本断言把这类误用变成会响的铃。
        """
        # 只查**可变状态**名 (路径/阈值): 函数对象 (history/_upsert/…) 显式 import
        # 是合法且必要的, 且它们不承载可变状态 (见 runner 的 _IO_STATE_NAMES 注释)
        materialized = set(mpr._IO_STATE_NAMES) & set(vars(mpr))
        assert not materialized, (
            f"runner 模块上出现了转发名的实体属性 {sorted(materialized)} —— "
            "有人 patch 了 mpr.X 而不是 core.market_position_io.X, "
            "会永久 shadow __getattr__ 转发 (隔离失效)")

    def test_runner_reads_base_paths_at_call_time(self):
        """runner 内部必须**调用期**读基座路径 (不能是 import 时快照)。

        手法: 临时把基座路径指到 tmp, 断言 runner 的 history() 跟着走 ——
        若 runner 里存了快照副本, 这里读的还是旧路径。
        """
        from core import market_position_io as mpio
        orig = mpio.DAILY_PATH
        try:
            mpio.DAILY_PATH = Path(orig).parent / "____guard_probe.jsonl"
            Path(mpio.DAILY_PATH).parent.mkdir(parents=True, exist_ok=True)
            Path(mpio.DAILY_PATH).write_text(
                '{"date": "2026-09-19", "probe": true}\n', encoding="utf-8")
            assert mpr.history(limit=1)[-1].get("probe") is True
            assert mpr.latest().get("probe") is True
        finally:
            Path(mpio.DAILY_PATH).unlink(missing_ok=True)
            mpio.DAILY_PATH = orig


class TestOwnershipRoster:
    """归属名册守卫 (2026-09-20 审计 P2-5/P2-6/P2-7)。

    病类: 拆包后 6 个块模块 + runner 各自 `from core.market_position_io import
    _f, history, _upsert` —— 拿到的是 import 时刻的**值副本**。于是 patch 基座
    (conftest 隔离 / 参数扫描) 对这些模块是**静默 no-op**, 而 patch 块模块又只
    影响它自己: 两种都静默 (审计变异实验 E4a/E4d 绿、E4b 红)。

    现在的契约:**跨模块一律走属主模块的调用期属性** (`mpio._f` / `market_erp.X`),
    本文件四条断言把它变成会响的铃。
    """

    SPLIT_MODULES = ("market_erp", "market_regime", "market_validity",
                     "market_shadow_replay", "market_thermometer",
                     "market_mirror")

    def test_roster_names_all_resolve_on_their_owner(self):
        """名册里的每个名字都必须在属主模块上真的存在 (防名册陈旧/写错归属)。"""
        missing = []
        for owner, names in mpr._FORWARD_TABLE:
            for n in names:
                if not hasattr(owner, n):
                    missing.append(f"{owner.__name__}.{n}")
        assert not missing, f"归属名册里有解析不到的名字: {missing}"

    def test_no_forwarded_name_is_materialized_for_real(self):
        """名册里的名字一个都不许在 runner 上留实体 (含函数对象/常量)。

        P2-7 原口径只守 5 个路径常量, 于是 `INDEX_SPECS` / `ERP_MIN_OBS` /
        `SHADOW_RULES` 这些按值 import 的副本不在守卫视野里 —— rebind `mpr.X`
        静默分流且守卫不响。现在全部纳入。
        """
        roster = {n for _owner, names in mpr._FORWARD_TABLE for n in names}
        materialized = sorted(roster & set(vars(mpr)))
        assert not materialized, (
            f"runner 上出现了转发名的实体副本 {materialized} —— "
            "patch 属主模块时这副本不会跟着变 (静默分流)")
        # 高风险可变状态清单必须是名册子集 (防手写漂移)
        assert set(mpr._IO_STATE_NAMES) <= roster

    def test_no_module_binds_base_primitives_by_value(self):
        """六个块模块 + runner 都不许 `from core.market_position_io import X`。

        AST 断言 (不是子串): 这类 import 一旦回来, patch 基座就是静默 no-op。
        """
        import ast
        root = Path(__file__).resolve().parents[1] / "core"
        files = [root / f"{m}.py" for m in self.SPLIT_MODULES]
        files.append(root / "market_position_runner.py")
        offenders = []
        for f in files:
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.ImportFrom)
                        and node.module == "core.market_position_io"):
                    offenders.append(f"{f.name}:{node.lineno} "
                                     f"{[a.name for a in node.names]}")
        assert not offenders, (
            "以下位置按值绑定了基座原语 (patch 基座对它无效): " + "; ".join(offenders))

    def test_no_split_module_binds_sibling_constants_by_value(self):
        """块模块之间也不许按值 import 常量/原语 (同名病类)。

        允许: 纯数学层 `core.market_position` 的公开函数 (无状态、且它是规则
        单一来源, 不参与 patch 面); 禁止: 任何 `core.market_*` 拆分包成员。
        """
        import ast
        root = Path(__file__).resolve().parents[1] / "core"
        allow = {"core.market_position", "core.market_position_io"}
        offenders = []
        for m in self.SPLIT_MODULES:
            f = root / f"{m}.py"
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                mod = node.module
                if (mod.startswith("core.market_") and mod not in allow):
                    offenders.append(f"{f.name}:{node.lineno} ← {mod} "
                                     f"{[a.name for a in node.names]}")
        assert not offenders, (
            "块模块间按值 import (patch 属主模块会静默分流): " + "; ".join(offenders))
