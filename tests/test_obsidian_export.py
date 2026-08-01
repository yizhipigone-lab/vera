"""obsidian.export 测试 — kg/schema.init_db 造合成小图, tmp_path 隔离, 不碰真实 kg/graph.db。"""
from __future__ import annotations

import json

import pytest

from kg.schema import init_db
from obsidian.export import export_vault


@pytest.fixture()
def kg_db(tmp_path):
    """合成小图: 5 节点 4 边 (+1 悬空边, 端点不在节点表, 应被忽略)。"""
    db = tmp_path / "graph.db"
    conn = init_db(db)
    conn.executemany(
        "INSERT INTO kg_nodes (node_id, node_type, code, name, payload_json) VALUES (?,?,?,?,?)",
        [
            ("policy:p1", "policy", "p1", "新能源规划",
             json.dumps({"policy_id": "p1", "title": "新能源规划",
                         "nested": {"a": 1}, "lst": [1, 2]}, ensure_ascii=False)),
            ("industry_tdx:881001.SH", "industry_tdx", "881001.SH", "煤炭开采",
             json.dumps({"priority": "P3"}, ensure_ascii=False)),
            ("company:600000", "company", "600000", "测试公司",
             json.dumps({"fullname": "测试公司股份有限公司", "time": "2020-01-01"},
                        ensure_ascii=False)),
            ("product:特种气体", "product", None, "特种气体",
             json.dumps({"name": "特种气体"}, ensure_ascii=False)),
            ("product:硅烷", "product", None, "硅烷", None),
        ],
    )
    conn.executemany(
        "INSERT INTO kg_edges (src_id, dst_id, edge_type, weight, payload_json) VALUES (?,?,?,?,?)",
        [
            ("policy:p1", "industry_tdx:881001.SH", "affects", 0.8, None),
            ("company:600000", "industry_tdx:881001.SH", "belongs_to", 1.0, None),
            ("company:600000", "product:特种气体", "main_product", 0.51, None),
            ("product:特种气体", "product:硅烷", "product_upstream", 1.0, None),
            # 悬空边: dst 不在 kg_nodes, 不应进 vault
            ("company:600000", "product:不存在", "main_product", 0.1, None),
        ],
    )
    conn.commit()
    conn.close()
    return db


def _mds(vault):
    return sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*.md"))


def _frontmatter(text: str) -> str:
    """取首个 --- 与第二个 --- 之间的 frontmatter 段。"""
    parts = text.split("---", 2)
    return parts[1] if len(parts) >= 3 else ""


def test_export_structure_and_stats(kg_db, tmp_path):
    vault = tmp_path / "vault"
    stats = export_vault(db_path=kg_db, out_dir=vault)
    assert stats == {"nodes": 5, "edges": 4, "files": 6}  # 5 节点 + _index.md, 悬空边被忽略

    # 目录分层 + 文件名稳定 (node_id 清洗 ':' → '_')
    assert (vault / "policy" / "policy_p1.md").is_file()
    assert (vault / "industry_tdx" / "industry_tdx_881001.SH.md").is_file()
    assert (vault / "company" / "company_600000.md").is_file()
    assert (vault / "product" / "product_特种气体.md").is_file()
    assert (vault / "product" / "product_硅烷.md").is_file()
    assert (vault / "_index.md").is_file()


def test_idempotent_two_runs(kg_db, tmp_path):
    vault = tmp_path / "vault"
    export_vault(db_path=kg_db, out_dir=vault)
    snap1 = {rel: (vault / rel).read_text(encoding="utf-8") for rel in _mds(vault)}
    export_vault(db_path=kg_db, out_dir=vault)  # 连跑第二次
    snap2 = {rel: (vault / rel).read_text(encoding="utf-8") for rel in _mds(vault)}
    assert list(snap1) == list(snap2), "两次导出文件列表必须一致"
    # export_time 秒级时间戳可能跨秒, 仅 _index.md 豁免内容比较
    for rel in snap1:
        if rel != "_index.md":
            assert snap1[rel] == snap2[rel], f"{rel} 内容应稳定"


def test_types_filter(kg_db, tmp_path):
    """--types 只导出指定 node_type: product 被跳过, 跨类型边两端不全也被忽略。"""
    vault = tmp_path / "vault"
    stats = export_vault(db_path=kg_db, out_dir=vault,
                         types=["policy", "industry_tdx", "company"])
    assert stats["nodes"] == 3
    # 只剩两端都在 {policy, industry_tdx, company} 内的边: affects + belongs_to
    assert stats["edges"] == 2
    assert not (vault / "product").exists()
    text = (vault / "company" / "company_600000.md").read_text(encoding="utf-8")
    assert "[[industry_tdx_881001.SH|煤炭开采]]" in text
    assert "product_特种气体" not in text  # 被过滤类型的边不残留死链


def test_frontmatter_fields(kg_db, tmp_path):
    vault = tmp_path / "vault"
    export_vault(db_path=kg_db, out_dir=vault)
    text = (vault / "company" / "company_600000.md").read_text(encoding="utf-8")
    fm = _frontmatter(text)
    assert 'node_id: "company:600000"' in fm
    assert 'node_type: "company"' in fm
    assert 'code: "600000"' in fm
    assert 'name: "测试公司"' in fm
    assert "export_date:" in fm
    # payload 标量展开进 frontmatter
    assert 'fullname: "测试公司股份有限公司"' in fm
    # 嵌套 dict/list 不进 frontmatter (policy 节点)
    fm_policy = _frontmatter((vault / "policy" / "policy_p1.md").read_text(encoding="utf-8"))
    assert 'policy_id: "p1"' in fm_policy
    assert "nested" not in fm_policy and "lst" not in fm_policy


def test_wikilinks_density_and_weight(kg_db, tmp_path):
    vault = tmp_path / "vault"
    export_vault(db_path=kg_db, out_dir=vault)
    text = (vault / "company" / "company_600000.md").read_text(encoding="utf-8")
    # 出边双链: 目标 = 对方节点 md 文件名 (不含 .md), 别名 = 对方名称
    assert "[[industry_tdx_881001.SH|煤炭开采]]" in text
    assert "[[product_特种气体|特种气体]]" in text
    assert "weight=0.51" in text
    assert "`main_product`" in text
    # 悬空边不出现
    assert "不存在" not in text
    # 入边 (product 视角)
    text_p = (vault / "product" / "product_特种气体.md").read_text(encoding="utf-8")
    assert "[[company_600000|测试公司]]" in text_p
    assert "[[product_硅烷|硅烷]]" in text_p
    # 全 vault 链接密度 > 0
    total = sum((vault / rel).read_text(encoding="utf-8").count("[[") for rel in _mds(vault))
    assert total > 0


def test_index_counts(kg_db, tmp_path):
    vault = tmp_path / "vault"
    export_vault(db_path=kg_db, out_dir=vault)
    idx = (vault / "_index.md").read_text(encoding="utf-8")
    assert "- 节点总数: 5" in idx
    assert "- 边总数 (导出席位内): 4" in idx
    for t, c in (("policy", 1), ("industry_tdx", 1), ("company", 1), ("product", 2)):
        assert f"`{t}`: {c}" in idx


def test_max_nodes_truncates_edges(kg_db, tmp_path):
    # ORDER BY node_type, node_id → 前 2 个是 company:600000 + industry_tdx:881001.SH
    stats = export_vault(db_path=kg_db, out_dir=tmp_path / "vault", max_nodes=2)
    assert stats["nodes"] == 2
    assert stats["edges"] == 1  # 只剩 belongs_to, 截断出去的节点边一并消失
    assert stats["files"] == 3


def test_missing_db_graceful(tmp_path):
    stats = export_vault(db_path=tmp_path / "nope.db", out_dir=tmp_path / "vault")
    assert stats == {"nodes": 0, "edges": 0, "files": 0}
    assert not (tmp_path / "vault").exists()  # 不创建输出目录


def test_empty_tables_graceful(tmp_path):
    db = tmp_path / "empty.db"
    init_db(db).close()  # 有表无数据
    stats = export_vault(db_path=db, out_dir=tmp_path / "vault")
    assert stats == {"nodes": 0, "edges": 0, "files": 0}


def test_readonly_no_db_side_effects(kg_db, tmp_path):
    before = kg_db.read_bytes()
    export_vault(db_path=kg_db, out_dir=tmp_path / "vault")
    assert kg_db.read_bytes() == before  # db 内容零改动 (WAL 模式下 -wal/-shm 是读取产物, 非写库)
