"""2026-07-23 修复的回归测试:
1. formula_runner 自适应扫描深度 (_adaptive_scan_count) — 原写死 3000 扫 ~12 年全历史
2. selector 北交所口径过滤 — 板块成份股可能含 .BJ (实测 881008 含 920088.BJ),
   仅 全部A股(5)/北交所(53) 口径保留, 沪深A股(50) 等一律剔除
"""
import pandas as pd
import pytest

from core.formula_runner import _adaptive_scan_count
from selection.selector import StockSelector
from core.data_fetcher import DataFetcher


# === _adaptive_scan_count ===

class TestAdaptiveScanCount:
    def test_short_range_much_less_than_3000(self):
        # 2024-01-01 ~ 2025-06-30 ≈ 546 天 ≈ 365 交易日 + 300 预热 ≈ 665
        count = _adaptive_scan_count("20240101", "20250630", "1d")
        assert 400 <= count <= 1000
        assert count < 3000

    def test_long_range_capped_at_3000(self):
        assert _adaptive_scan_count("20100101", "20250630", "1d") == 3000

    def test_floor_400(self):
        # 区间很短也要留足指标预热
        assert _adaptive_scan_count("20250101", "20250131", "1d") == 400

    def test_unknown_period_fallback_3000(self):
        assert _adaptive_scan_count("20240101", "20250630", "1m") == 3000

    def test_missing_dates_fallback_3000(self):
        assert _adaptive_scan_count("", "20250630", "1d") == 3000
        assert _adaptive_scan_count("20240101", "", "1d") == 3000

    def test_bad_dates_fallback_3000(self):
        assert _adaptive_scan_count("not-a-date", "20250630", "1d") == 3000

    def test_reversed_range_fallback_3000(self):
        assert _adaptive_scan_count("20250630", "20240101", "1d") == 3000

    def test_5m_scales_by_bars_per_day(self):
        # 5m: 48 bar/天, 30 天 ≈ 954 + 300 预热
        count = _adaptive_scan_count("20250101", "20250131", "5m")
        assert 1000 <= count <= 1500


# === selector 北交所过滤 ===

def _make_selector(utype, sectors=None):
    cfg = {
        "formula_name": "UPN",
        "universe": {"type": utype, "exclude_st": False, "sectors": sectors or []},
    }
    return StockSelector(cfg)


class TestUniverseBjFilter:
    def test_sectors_hs_a_drops_bj(self, monkeypatch):
        """沪深A股(50) + 板块: 板块里的北交所被剔除"""
        monkeypatch.setattr(
            DataFetcher, "get_sector_stocks",
            lambda code: ["600519.SH", "920088.BJ", "000001.SZ"],
        )
        stocks = _make_selector("50", sectors=["881008.SH"]).resolve_universe()
        assert "920088.BJ" not in stocks
        assert set(stocks) == {"600519.SH", "000001.SZ"}

    def test_sectors_all_a_keeps_bj(self, monkeypatch):
        """全部A股(5) + 板块: 北交所保留"""
        monkeypatch.setattr(
            DataFetcher, "get_sector_stocks",
            lambda code: ["600519.SH", "920088.BJ"],
        )
        stocks = _make_selector("5", sectors=["881008.SH"]).resolve_universe()
        assert "920088.BJ" in stocks

    def test_hs_a_pool_defensive_drop(self, monkeypatch):
        """沪深A股(50) 池本身被污染时 (TDX 客户端差异) 也兜底剔除"""
        monkeypatch.setattr(
            DataFetcher, "get_stock_universe",
            lambda lt: ["600519.SH", "830799.BJ"],
        )
        stocks = _make_selector("50").resolve_universe()
        assert "830799.BJ" not in stocks

    def test_all_a_pool_keeps_bj(self, monkeypatch):
        monkeypatch.setattr(
            DataFetcher, "get_stock_universe",
            lambda lt: ["600519.SH", "830799.BJ"],
        )
        stocks = _make_selector("5").resolve_universe()
        assert "830799.BJ" in stocks

    def test_beijingsuo_pool_keeps_bj(self, monkeypatch):
        """北交所(53) 池自身当然保留 .BJ"""
        monkeypatch.setattr(
            DataFetcher, "get_stock_universe",
            lambda lt: ["920088.BJ", "830799.BJ"],
        )
        stocks = _make_selector("53").resolve_universe()
        assert len(stocks) == 2
