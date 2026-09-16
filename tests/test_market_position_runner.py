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

import numpy as np
import pandas as pd
import pytest

from core import market_position_runner as mpr


def _write_cache(n_days=400, n_stocks=6, stub_last=False):
    """造一个最小日线缓存: n_stocks 只票 + 三大指数。

    写到 mpr.KLINE_1D_DIR (= conftest 隔离出来的 per-test tmp), 不写 tmp_path ——
    隔离的意义就是"runner 认哪个目录, 测试就往哪个目录造数据"。
    """
    d = mpr.KLINE_1D_DIR
    d.mkdir(parents=True, exist_ok=True)
    dates = pd.date_range("2024-01-01", periods=n_days, freq="B")
    for i in range(n_stocks):
        code = f"60000{i}.SH"
        base = 10.0 + i
        close = np.linspace(base, base * 1.3, n_days)
        vol = np.full(n_days, 1000.0)
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

    def test_markdown_carries_all_three_caveats(self, tmp_path):
        """三条已知偏差必须原样出现在报告里 (不靠各处自觉)。"""
        _write_cache()
        mpr.collect(bars=300, write=True)
        md = mpr.thermometer_md()
        assert "ST 股 (±5%) 会漏计" in md
        assert "生存者偏差" in md
        assert "不足十年" in md
        assert "不联入任何仓位调度" in md

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
        assert "ok" in md
        sd = mpr.shadow_replay()
        assert "ok" in sd
        if sd["ok"]:
            assert {r["rule"] for r in sd["rows"]} == set(mpr.SHADOW_RULES)
            # 年化分母必须按真实日历跨度, 不能拿录像条数当分母
            assert sd["years"] > 0 and sd["days"] > 0


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
