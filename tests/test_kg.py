"""test_kg - kg 知识图谱查询测试 (政策研究平台 P0, 计划书 §7)。

覆盖: 命中/miss/code 归一化/穿透/松耦合(空 db)。
固化 P0 闸门: 持仓票查到上下游产品/公司。
"""
import pytest

from kg.query import (
    get_company,
    get_company_industry,
    get_company_products,
    get_holdings_chain,
    get_product_upstream,
)
from kg.schema import get_db_path


# 用真实 graph.db (P0 已导入 17 万边) 测命中; 临时空 db 测松耦合
@pytest.fixture(scope="module")
def real_db():
    p = get_db_path()
    # 2026-09-20 CI 修红: graph.db 被 gitignore (由 chainkg_loader 重建),
    # CI checkout 永远没有 → 命中类用例在无库环境 skip (空 db 松耦合用例照常跑)
    from pathlib import Path
    if not Path(p).exists():
        pytest.skip(f"知识图谱库不存在 ({p}), 命中类用例跳过 —— CI/新机器上没有")
    return p


# ── get_company: 命中 / 归一化 / miss ──

def test_get_company_hit(real_db):
    """已知票 → 公司节点 (华特气体 688268 已在图谱)."""
    c = get_company("688268.SH", real_db)
    assert c is not None
    assert c["code"] == "688268"
    assert "气体" in c["name"]


def test_get_company_code_normalization(real_db):
    """带后缀/不带后缀都能查 (688268.SH == 688268)."""
    a = get_company("688268.SH", real_db)
    b = get_company("688268", real_db)
    assert a is not None and b is not None
    assert a["node_id"] == b["node_id"]  # 归一化到同一节点


def test_get_company_miss(real_db):
    """不存在票 → None (松耦合)."""
    assert get_company("999999.SZ", real_db) is None
    assert get_company("", real_db) is None


# ── 行业 / 主营产品 ──

def test_get_company_industry(real_db):
    """票 → 行业列表."""
    ind = get_company_industry("688268.SH", real_db)
    assert isinstance(ind, list)
    assert len(ind) >= 1
    assert all("code" in i and "name" in i for i in ind)


def test_get_company_products_ordered(real_db):
    """票 → 主营产品按 rel_weight 降序."""
    prods = get_company_products("688268.SH", real_db, top_n=3)
    assert len(prods) >= 1
    weights = [p["weight"] for p in prods]
    assert weights == sorted(weights, reverse=True)


# ── 产品上游 (递归 CTE) ──

def test_get_product_upstream_hit(real_db):
    """产品 → 上游材料 (红豆薏米仁汤 首行确定有上游 冰糖/红豆/薏米仁)."""
    ups = get_product_upstream("红豆薏米仁汤", max_depth=1, db_path=real_db)
    assert len(ups) >= 1
    names = [u["upstream"] for u in ups]
    assert any(n in names for n in ["冰糖", "红豆", "薏米仁"])


def test_get_product_upstream_miss(real_db):
    """不存在的产品 → 空列表 (不抛)."""
    assert get_product_upstream("不存在的产品XYZ", db_path=real_db) == []


# ── ★ P0 闸门: 完整穿透 ──

def test_get_holdings_chain_hit(real_db):
    """★P0 闸门: 已知票 → 完整穿透 dict (广晟有色 600259 有完整产业链)."""
    r = get_holdings_chain("600259.SH", real_db)
    assert r is not None
    assert r["stock"] == "600259"
    assert r["company"]["name"] == "广晟有色"
    assert "industry" in r
    assert isinstance(r["products"], list)
    assert len(r["products"]) >= 1
    # 至少一个主营产品有上游材料 (广晟有色的涤纶长丝/稀土有上游)
    has_upstream = any(p["upstream"] for p in r["products"])
    assert has_upstream, "广晟有色应有产品带上游材料"


def test_get_holdings_chain_miss(real_db):
    """不存在票 → None (松耦合)."""
    assert get_holdings_chain("999999.SZ", real_db) is None


# ── 松耦合: 空 db 不崩 (铁律: 图谱崩了不影响选股交易) ──

def test_松耦合_空db_不抛(tmp_path):
    """空 db (表都不存在) → 查询返 None/[] 不抛 (松耦合铁律: 图谱崩了不影响选股交易)."""
    empty_db = tmp_path / "empty.db"  # 不 init_db, 纯空文件
    assert get_company("688268", empty_db) is None      # 表不存在 → except → None
    assert get_company_industry("688268", empty_db) == []
    assert get_company_products("688268", empty_db) == []
    assert get_holdings_chain("688268", empty_db) is None
