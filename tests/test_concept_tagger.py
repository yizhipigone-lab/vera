"""test_concept_tagger - B 层概念标签测试 (P1.5, 计划书 §7)。

mock xtdata/缓存, 测 tag_selections / 缓存命中 / 松耦合 (不依赖真实通达信环境)。
"""
from unittest.mock import patch

import pandas as pd

from concept_kb import concept_tagger as ct

# ── tag_stocks ──

def test_tag_stocks_hit():
    """已知票 → 概念 list; 不在索引的票 → 空 list."""
    fake_idx = {"600519.SH": ["白酒概念"], "300750.SZ": ["储能", "固态电池"]}
    with patch.object(ct, "_load_cache", return_value={"index": fake_idx, "concept_list": ["白酒概念"]}):
        tags = ct.tag_stocks(["600519.SH", "300750.SZ", "999999.SZ"])
    assert tags["600519.SH"] == ["白酒概念"]
    assert "储能" in tags["300750.SZ"]
    assert tags["999999.SZ"] == []  # 不在索引 → 空 (不抛)


def test_tag_stocks_empty():
    """空输入 → {} (不调缓存)."""
    assert ct.tag_stocks([]) == {}


# ── tag_selections (Pipeline 接缝) ──

def test_tag_selections_hit():
    """DataFrame → {票: [概念]}."""
    fake_idx = {"600519.SH": ["白酒概念"]}
    sel = pd.DataFrame({"stock_code": ["600519.SH", "999999.SZ"]})
    with patch.object(ct, "_load_cache", return_value={"index": fake_idx, "concept_list": []}):
        r = ct.tag_selections(sel)
    assert r["600519.SH"] == ["白酒概念"]
    assert r["999999.SZ"] == []


def test_tag_selections_empty_df():
    """空 DataFrame → None (serialize 不加 key)."""
    assert ct.tag_selections(pd.DataFrame()) is None


def test_tag_selections_none():
    assert ct.tag_selections(None) is None


def test_tag_selections_no_code_col():
    """无 stock_code 列 → None (不抛)."""
    df = pd.DataFrame({"code": ["600519"]})
    assert ct.tag_selections(df) is None


# ── build_concept_index: 缓存命中 / 强刷 / 松耦合 ──

def test_build_index_cache_hit():
    """缓存命中 → 不重拉 xtdata."""
    fake = {"index": {"600519.SH": ["白酒概念"]}, "concept_list": ["白酒概念"]}
    with patch.object(ct, "_load_cache", return_value=fake), \
         patch.object(ct, "_fetch_from_tdx") as m_fetch:
        idx = ct.build_concept_index()
        assert idx == {"600519.SH": ["白酒概念"]}
        m_fetch.assert_not_called()  # 缓存命中, 不拉


def test_build_index_force_refresh():
    """force_refresh=True → 忽略缓存重拉."""
    with patch.object(ct, "_fetch_from_tdx",
                      return_value=(["TDGN白酒概念"], {"600519.SH": ["白酒概念"]})), \
         patch.object(ct, "_save_cache"), \
         patch.object(ct, "_load_cache") as m_load:
        idx = ct.build_concept_index(force_refresh=True)
        assert idx == {"600519.SH": ["白酒概念"]}
        m_load.assert_not_called()  # 强刷不读缓存


def test_build_index_松耦合_失败用旧缓存():
    """xtdata 失败 → 用旧缓存(allow_expired), 不抛 (松耦合铁律)."""
    # 第一次 _load_cache (TTL 检查) 返 None → 触发拉; 拉失败 → 第二次 _load_cache(allow_expired=True) 返旧缓存
    old = {"index": {"600519.SH": ["白酒概念"]}, "concept_list": ["白酒概念"]}
    with patch.object(ct, "_load_cache", side_effect=[None, old]), \
         patch.object(ct, "_fetch_from_tdx", side_effect=Exception("xtdata 挂了")):
        idx = ct.build_concept_index()
        assert idx == {"600519.SH": ["白酒概念"]}  # 用旧缓存, 不抛


def test_build_index_松耦合_失败无旧缓存返空():
    """xtdata 失败 + 无旧缓存 → 返 {} (不抛, serialize 不加 key)."""
    with patch.object(ct, "_load_cache", return_value=None), \
         patch.object(ct, "_fetch_from_tdx", side_effect=Exception("xtdata 挂了")):
        idx = ct.build_concept_index()
        assert idx == {}
