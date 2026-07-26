"""test_sector_index - 票→行业反向索引测试(计划书 §7)。

覆盖:反向索引构建、多归属取首、缓存命中、force_refresh、get_sector_of、
空输入、失败行业跳过(松耦合)。

所有 DataFetcher 调用 monkeypatch mock,不实际连 TDX。
"""
from policy_kb import build_sector_index


def _patch(monkeypatch, sector_list, stocks_map):
    """monkeypatch DataFetcher.get_sector_list / get_sector_stocks 两个 classmethod。"""
    monkeypatch.setattr(
        build_sector_index.DataFetcher,
        "get_sector_list",
        classmethod(lambda cls: list(sector_list)),
    )
    monkeypatch.setattr(
        build_sector_index.DataFetcher,
        "get_sector_stocks",
        classmethod(lambda cls, c: stocks_map.get(c, [])),
    )
    build_sector_index.clear_cache()


def test_build_returns_reverse_index(monkeypatch):
    """正向(行业→票)能反查成 {票: 行业}。"""
    _patch(
        monkeypatch,
        [{"code": "881319.SH", "name": "半导体"}, {"code": "881326.SH", "name": "消费电子"}],
        {"881319.SH": ["600519.SH", "000001.SZ"], "881326.SH": ["600519.SH", "300750.SZ"]},
    )
    idx = build_sector_index.build_stock_sector_index()
    assert idx["600519.SH"] == "881319.SH"  # 多归属取首个
    assert idx["000001.SZ"] == "881319.SH"
    assert idx["300750.SZ"] == "881326.SH"


def test_cache_hit_no_rebuild(monkeypatch):
    """第二次调用命中缓存,不重新拉(只调 get_sector_list 一次)。"""
    count = {"n": 0}

    def mock_list(cls):
        count["n"] += 1
        return [{"code": "881319.SH", "name": "半导体"}]

    monkeypatch.setattr(build_sector_index.DataFetcher, "get_sector_list", classmethod(mock_list))
    monkeypatch.setattr(
        build_sector_index.DataFetcher,
        "get_sector_stocks",
        classmethod(lambda cls, c: []),
    )
    build_sector_index.clear_cache()
    build_sector_index.build_stock_sector_index()
    build_sector_index.build_stock_sector_index()
    assert count["n"] == 1


def test_force_refresh_rebuilds(monkeypatch):
    """force_refresh=True 强制重建。"""
    count = {"n": 0}

    def mock_list(cls):
        count["n"] += 1
        return [{"code": "881319.SH", "name": "半导体"}]

    monkeypatch.setattr(build_sector_index.DataFetcher, "get_sector_list", classmethod(mock_list))
    monkeypatch.setattr(
        build_sector_index.DataFetcher,
        "get_sector_stocks",
        classmethod(lambda cls, c: []),
    )
    build_sector_index.clear_cache()
    build_sector_index.build_stock_sector_index()
    build_sector_index.build_stock_sector_index(force_refresh=True)
    assert count["n"] == 2


def test_get_sector_of(monkeypatch):
    """单票查询;不在索引返回 None。"""
    _patch(
        monkeypatch,
        [{"code": "881319.SH", "name": "半导体"}],
        {"881319.SH": ["600519.SH"]},
    )
    assert build_sector_index.get_sector_of("600519.SH") == "881319.SH"
    assert build_sector_index.get_sector_of("999999.SH") is None


def test_empty_when_no_sectors(monkeypatch):
    """无行业 → 空 dict(不抛)。"""
    _patch(monkeypatch, [], {})
    assert build_sector_index.build_stock_sector_index() == {}


def test_failed_sector_skipped(monkeypatch):
    """某行业拉取抛异常 → 跳过,不中断(松耦合,返部分结果)。"""
    def mock_stocks(cls, c):
        if c == "881326.SH":
            raise RuntimeError("TDX 断")
        return ["600519.SH"] if c == "881319.SH" else []

    monkeypatch.setattr(
        build_sector_index.DataFetcher,
        "get_sector_list",
        classmethod(lambda cls: [
            {"code": "881319.SH", "name": "半导体"},
            {"code": "881326.SH", "name": "消费电子"},
        ]),
    )
    monkeypatch.setattr(build_sector_index.DataFetcher, "get_sector_stocks", classmethod(mock_stocks))
    build_sector_index.clear_cache()
    idx = build_sector_index.build_stock_sector_index()
    assert idx == {"600519.SH": "881319.SH"}  # 881326 失败跳过


def test_idempotent(monkeypatch):
    """多次构建结果一致(幂等)。"""
    _patch(
        monkeypatch,
        [{"code": "881319.SH", "name": "半导体"}],
        {"881319.SH": ["600519.SH", "000001.SZ"]},
    )
    build_sector_index.clear_cache()
    r1 = build_sector_index.build_stock_sector_index()
    r2 = build_sector_index.build_stock_sector_index(force_refresh=True)
    assert r1 == r2
