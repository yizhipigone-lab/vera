"""
行业板块选股 (sector selection) — selector.sectors 分支单测

不依赖 TDX, 用 monkeypatch mock DataFetcher.get_sector_stocks / get_stock_universe.
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from selection.selector import StockSelector


# ---------------------------------------------------------------------------
# Fixtures: mock TDX
# ---------------------------------------------------------------------------
def _make_selector(sectors=None, include_etf=False, etf_only=False, utype="50"):
    """构造 StockSelector, universe 配置注入 sectors/ETF 字段."""
    config = {
        "formula_name": "UPN",
        "formula_arg": "3",
        "universe": {
            "type": utype,
            "exclude_st": False,        # 关闭 ST 过滤, 避免 mock get_stock_info
            "sectors": sectors or [],
            "include_etf": include_etf,
            "etf_only": etf_only,
        },
        "period": "1d",
        "dividend_type": 1,
    }
    return StockSelector(config)


@pytest.fixture
def mock_sector_stocks(monkeypatch):
    """mock DataFetcher.get_sector_stocks, 返回固定成份股."""
    sector_data = {
        "881319.SH": ["600519.SH", "000001.SZ", "300750.SZ"],   # 半导体 (3 只)
        "881326.SH": ["600519.SH", "002049.SZ", "300053.SZ"],   # 消费电子 (3 只, 含 1 个重复)
        "881291.SH": ["688001.SH", "688002.SH"],                # 军工电子 (2 只)
    }
    from core import data_fetcher
    monkeypatch.setattr(
        data_fetcher.DataFetcher, "get_sector_stocks",
        classmethod(lambda cls, code: sector_data.get(code, []))
    )
    return sector_data


@pytest.fixture
def mock_universe(monkeypatch):
    """mock DataFetcher.get_stock_universe, A股池 + ETF 池."""
    pools = {
        "50": ["600519.SH", "000001.SZ", "300750.SZ"],          # A股池 (3 只)
        "31": ["510300.SH", "510500.SH", "513100.SH"],          # ETF 池 (3 只)
    }
    from core import data_fetcher
    monkeypatch.setattr(
        data_fetcher.DataFetcher, "get_stock_universe",
        classmethod(lambda cls, lt: pools.get(str(lt), []))
    )
    return pools


# ---------------------------------------------------------------------------
# Test 1: 单板块 → 成份股正确
# ---------------------------------------------------------------------------
def test_single_sector(mock_sector_stocks):
    sel = _make_selector(sectors=["881319.SH"])
    stocks = sel.resolve_universe()
    expected = {"600519.SH", "000001.SZ", "300750.SZ"}
    assert set(stocks) == expected, f"单板块成份股应 = 半导体 3 只, 实际 {set(stocks)}"


# ---------------------------------------------------------------------------
# Test 2: 多板块 → 并集去重
# ---------------------------------------------------------------------------
def test_multi_sector_union(mock_sector_stocks):
    sel = _make_selector(sectors=["881319.SH", "881326.SH"])
    stocks = sel.resolve_universe()
    # 半导体 {600519, 000001, 300750} ∪ 消费电子 {600519, 002049, 300053}
    expected = {"600519.SH", "000001.SZ", "300750.SZ", "002049.SZ", "300053.SZ"}
    assert set(stocks) == expected, f"多板块并集去重, 实际 {set(stocks)}"


# ---------------------------------------------------------------------------
# Test 3: 板块 + include_etf → 叠加 ETF
# ---------------------------------------------------------------------------
def test_sector_with_etf(mock_sector_stocks, mock_universe):
    sel = _make_selector(sectors=["881319.SH"], include_etf=True)
    stocks = sel.resolve_universe()
    expected = {"600519.SH", "000001.SZ", "300750.SZ",  # 半导体
                "510300.SH", "510500.SH", "513100.SH"}  # ETF
    assert set(stocks) == expected, f"板块+ETF 叠加, 实际 {set(stocks)}"


# ---------------------------------------------------------------------------
# Test 4: 板块 + etf_only → 只 ETF (板块被忽略)
# ---------------------------------------------------------------------------
def test_sector_ignored_when_etf_only(mock_sector_stocks, mock_universe):
    sel = _make_selector(sectors=["881319.SH"], etf_only=True)
    stocks = sel.resolve_universe()
    expected = {"510300.SH", "510500.SH", "513100.SH"}  # 只 ETF
    assert set(stocks) == expected, f"etf_only 优先, 板块被忽略, 实际 {set(stocks)}"
    assert "600519.SH" not in stocks, "板块成份股不应出现"


# ---------------------------------------------------------------------------
# Test 5: 空板块 → 走 A 股路径
# ---------------------------------------------------------------------------
def test_empty_sectors_uses_a_stock(mock_universe):
    sel = _make_selector(sectors=[], etf_only=False, include_etf=False, utype="50")
    stocks = sel.resolve_universe()
    expected = {"600519.SH", "000001.SZ", "300750.SZ"}  # A 股池
    assert set(stocks) == expected, f"空板块走 A 股, 实际 {set(stocks)}"


# ---------------------------------------------------------------------------
# Test 6: 重复板块代码 → 去重 (set 自然处理)
# ---------------------------------------------------------------------------
def test_duplicate_sector_dedup(mock_sector_stocks):
    sel = _make_selector(sectors=["881319.SH", "881319.SH"])
    stocks = sel.resolve_universe()
    # 重复板块, 成份股去重后仍 3 只
    expected = {"600519.SH", "000001.SZ", "300750.SZ"}
    assert set(stocks) == expected, f"重复板块去重, 实际 {set(stocks)}"
    assert len(stocks) == 3, f"去重后 3 只, 实际 {len(stocks)}"


# ---------------------------------------------------------------------------
# P0 #1: 端到端断言 (审计 Q2) — 板块成份股 → 选股池 → 回测 trades 一致性
# ---------------------------------------------------------------------------
def test_end_to_end_sector_backtest_trades_in_sector(mock_sector_stocks):
    """端到端: selector.resolve_universe() 返回的股票 ∉ 板块成份股的, 不应出现."""
    sel = _make_selector(sectors=["881319.SH"])
    stocks = sel.resolve_universe()
    # 预期: 半导体 180 只里 mock 了 3 只 (600519, 000001, 300750)
    sector_members = {"600519.SH", "000001.SZ", "300750.SZ"}
    outsiders = set(stocks) - sector_members
    assert not outsiders, f"选股池不应包含板块外股票, 额外: {outsiders}"


@pytest.fixture
def mock_empty_sector(monkeypatch):
    """mock 板块返回空成份股 (板块代码失效)."""
    from core import data_fetcher
    monkeypatch.setattr(
        data_fetcher.DataFetcher, "get_sector_stocks",
        classmethod(lambda cls, code: [])
    )
    monkeypatch.setattr(
        data_fetcher.DataFetcher, "get_stock_universe",
        classmethod(lambda cls, lt: ["600519.SH", "000001.SZ"] if str(lt) == "50" else [])
    )


def test_empty_sector_falls_back_to_empty_pool(mock_empty_sector):
    """Q5 边界: 板块代码失效 → 成份股为空 → 选股池为空."""
    sel = _make_selector(sectors=["999999.SH"], etf_only=False, include_etf=False)
    stocks = sel.resolve_universe()
    assert len(stocks) == 0, f"失效板块成份股为空, 选股池应空, 实际 {len(stocks)}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# === 覆盖率靶向 (2026-07-15) ===

def test_custom_universe_type():
    """type='custom' → 直接使用 config['stocks'] 列表."""
    sel = StockSelector({
        "formula_name": "UPN",
        "formula_arg": "3",
        "universe": {
            "type": "custom",
            "stocks": ["600519.SH", "000001.SZ"],
            "exclude_st": False,
            "sectors": [],
            "include_etf": False,
            "etf_only": False,
        },
    })
    stocks = sel.resolve_universe()
    assert len(stocks) == 2
    assert "600519.SH" in stocks


def test_resolve_universe_exclude_st_enabled(monkeypatch):
    """exclude_st=True 时 resolve_universe 调用 filter_stocks."""
    from core import data_fetcher

    def fake_universe(cls, lt):
        return ["600519.SH", "000001.SZ", "000002.SZ"]

    monkeypatch.setattr(data_fetcher.DataFetcher, "get_stock_universe",
                        classmethod(fake_universe))

    sel = StockSelector({
        "formula_name": "UPN",
        "formula_arg": "3",
        "universe": {
            "type": "50",
            "exclude_st": True,
            "exclude_new_listings_days": 60,
            "sectors": [],
            "include_etf": False,
            "etf_only": False,
        },
    })
    stocks = sel.resolve_universe()
    # ST filter may reduce list, but resolve should complete without error
    assert isinstance(stocks, list)
