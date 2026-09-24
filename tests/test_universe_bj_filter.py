"""回归测试:
1. formula_runner 扫描深度 (_adaptive_scan_count) — 2026-07-31 回退为写死 3000
   (2026-07-23 的自适应算法按 end_time 估算 count, 但 TDX 实际从当前日期往前,
   导致 end_time 早于今天的历史回测丢前段信号; 见函数 docstring)
2. selector 北交所口径过滤 — 板块成份股可能含 .BJ (实测 881008 含 920088.BJ),
   仅 全部A股(5)/北交所(53) 口径保留, 沪深A股(50) 等一律剔除
"""

from core.data_fetcher import DataFetcher
from core.formula_runner import _adaptive_scan_count
from selection.selector import StockSelector

# === _adaptive_scan_count (2026-07-31: 恒返回 _MAX_SCAN_COUNT=3000) ===

class TestAdaptiveScanCount:
    # 2026-07-31 回退后, 函数对所有输入恒返回 3000。下列用例保留多输入覆盖
    # (短/长区间、各周期、缺值、坏值、反转), 确保任何分支都不会重新触发旧自适应
    # 逻辑 (那会让 end_time 早于今天的历史回测丢前段信号)。

    def test_1d_short_range(self):
        assert _adaptive_scan_count("20240101", "20250630", "1d") == 3000

    def test_1d_long_range(self):
        # 2026-08-02 起 _adaptive_scan_count 按"start 距今天"估算, floor=3000 cap=7000
        # (formula_runner.py:44-50)。超长区间(2010→今天)突破 floor 返回 ~4318,
        # 写死 ==3000 已过期; 改 >=3000 表达"始终覆盖到 floor 以上"的语义。
        assert _adaptive_scan_count("20100101", "20250630", "1d") >= 3000

    def test_1d_tiny_range(self):
        assert _adaptive_scan_count("20250101", "20250131", "1d") == 3000

    def test_1m(self):
        assert _adaptive_scan_count("20240101", "20250630", "1m") == 3000

    def test_5m(self):
        assert _adaptive_scan_count("20250101", "20250131", "5m") == 3000

    def test_missing_dates(self):
        assert _adaptive_scan_count("", "20250630", "1d") == 3000
        assert _adaptive_scan_count("20240101", "", "1d") == 3000

    def test_bad_dates(self):
        assert _adaptive_scan_count("not-a-date", "20250630", "1d") == 3000

    def test_reversed_range(self):
        assert _adaptive_scan_count("20250630", "20240101", "1d") == 3000


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
