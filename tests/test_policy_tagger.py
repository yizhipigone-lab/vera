"""test_policy_tagger - 票→优先级标签测试(计划书 §7)。

覆盖:各档正确(P1/P2/P3/AVOID)、UNTAGGED(不在索引/ETF)、批量、
失败返 None(松耦合)、selections、空/无 code 列、JSON 加载(128 全)。

build_stock_sector_index 全 mock,不实际连 TDX;tongdaxin_priority.json 读真实文件。
"""
import pandas as pd

from policy_kb import policy_tagger


def _patch_index(monkeypatch, index_dict):
    """mock policy_tagger.build_stock_sector_index 返固定反向索引。"""
    monkeypatch.setattr(policy_tagger, "build_stock_sector_index", lambda: dict(index_dict))


def test_tag_stock_p1(monkeypatch):
    _patch_index(monkeypatch, {"600519.SH": "881319.SH"})  # 半导体=P1
    policy_tagger.clear_cache()
    assert policy_tagger.tag_stock("600519.SH") == "P1"


def test_tag_stock_avoid(monkeypatch):
    _patch_index(monkeypatch, {"000001.SZ": "881065.SH"})  # 普钢=AVOID
    policy_tagger.clear_cache()
    assert policy_tagger.tag_stock("000001.SZ") == "AVOID"


def test_tag_stock_p2(monkeypatch):
    _patch_index(monkeypatch, {"300750.SZ": "881262.SH"})  # 电池=P2
    policy_tagger.clear_cache()
    assert policy_tagger.tag_stock("300750.SZ") == "P2"


def test_tag_stock_untagged_not_in_index(monkeypatch):
    """票不在反向索引 → UNTAGGED。"""
    _patch_index(monkeypatch, {})
    policy_tagger.clear_cache()
    assert policy_tagger.tag_stock("999999.SH") == policy_tagger.UNTAGGED


def test_tag_stock_etf_untagged(monkeypatch):
    """ETF(510300)不属于 881 行业,不在反向索引 → UNTAGGED。"""
    _patch_index(monkeypatch, {})
    policy_tagger.clear_cache()
    assert policy_tagger.tag_stock("510300.SH") == policy_tagger.UNTAGGED


def test_tag_stocks_batch(monkeypatch):
    _patch_index(monkeypatch, {
        "600519.SH": "881319.SH",  # P1 半导体
        "000001.SZ": "881065.SH",  # AVOID 普钢
        "300750.SZ": "881262.SH",  # P2 电池
    })
    policy_tagger.clear_cache()
    r = policy_tagger.tag_stocks(["600519.SH", "000001.SZ", "300750.SZ", "999999.SH"])
    assert r == {
        "600519.SH": "P1",
        "000001.SZ": "AVOID",
        "300750.SZ": "P2",
        "999999.SH": "UNTAGGED",
    }


def test_tag_stocks_failure_returns_none(monkeypatch):
    """索引构建崩 → tag_stocks 返 None(松耦合)。"""
    def boom():
        raise RuntimeError("索引崩")
    monkeypatch.setattr(policy_tagger, "build_stock_sector_index", boom)
    assert policy_tagger.tag_stocks(["600519.SH"]) is None


def test_tag_selections(monkeypatch):
    _patch_index(monkeypatch, {"600519.SH": "881319.SH"})
    policy_tagger.clear_cache()
    df = pd.DataFrame({"stock_code": ["600519.SH", "999999.SH"]})
    r = policy_tagger.tag_selections(df)
    assert r == {"600519.SH": "P1", "999999.SH": "UNTAGGED"}


def test_tag_selections_empty():
    assert policy_tagger.tag_selections(pd.DataFrame()) is None
    assert policy_tagger.tag_selections(None) is None


def test_tag_selections_no_code_col():
    """selections 无 stock_code 列 → None(不抛)。"""
    df = pd.DataFrame({"code": ["600519.SH"]})
    assert policy_tagger.tag_selections(df) is None


def test_load_priority_map_real_json():
    """读真实 tongdaxin_priority.json:128 行业,半导体 P1,普钢 AVOID。"""
    policy_tagger.clear_cache()
    pm = policy_tagger._load_priority_map(force_refresh=True)
    assert len(pm) == 128
    assert pm["881319.SH"]["priority"] == "P1"   # 半导体
    assert pm["881319.SH"]["name"] == "半导体"
    assert pm["881065.SH"]["priority"] == "AVOID"  # 普钢
    assert pm["881262.SH"]["priority"] == "P2"   # 电池
