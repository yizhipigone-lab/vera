"""test_extractor — P1 政策抽取/入库测试 (计划书 §7).

mock DeepSeek (不依赖真实 API), 测 extract_policy 逻辑 + insert_policy 去重 + policy_id 冲突修复 + 松耦合.
"""
import json
from unittest.mock import patch

from policy_pipeline import extractor, ingest


def test_extract_policy_hit():
    """mock DeepSeek → 结构化抽取 (清单约束, sector_code 命中)."""
    fake = {
        "policy_id": "test_p", "title": "测试政策",
        "affected_industries": [
            {"sector_code": "881319.SH", "industry_name": "半导体", "direction": "利好",
             "strength": "strong", "evidence_quote": "原文摘录"}
        ],
        "other_industries": [], "valid_from": None, "valid_to": None, "policy_tools": ["补贴"],
    }
    with patch.object(extractor, "get_client") as gc:
        gc.return_value.chat.return_value = json.dumps(fake)
        r = extractor.extract_policy("测试政策", "政策原文")
    assert r and r["policy_id"] == "test_p"
    assert r["affected_industries"][0]["sector_code"] == "881319.SH"


def test_extract_policy_deepseek挂返None():
    """DeepSeek 无响应 → None (松耦合, 不抛)."""
    with patch.object(extractor, "get_client") as gc:
        gc.return_value.chat.return_value = None
        assert extractor.extract_policy("t", "text") is None


def test_extract_policy_空输入():
    """空 title/text → None."""
    assert extractor.extract_policy("", "text") is None
    assert extractor.extract_policy("t", "") is None


def test_insert_policy_去重(tmp_path):
    """同政策(同内容哈希)不重复入."""
    from kg.schema import init_db
    db = tmp_path / "t.db"
    init_db(db)
    extracted = {
        "policy_id": "p1", "title": "测",
        "affected_industries": [
            {"sector_code": "881319.SH", "industry_name": "半导体", "direction": "利好",
             "strength": "strong", "evidence_quote": "x"}
        ],
    }
    id1 = ingest.insert_policy(extracted, source_text="原文A", db_path=db)
    id2 = ingest.insert_policy(extracted, source_text="原文A", db_path=db)  # 同内容哈希
    assert id1 == id2  # 去重


def test_insert_policy_不同文本不同id(tmp_path):
    """不同政策文本 → 不同 policy_node (修 policy_id 冲突: 用内容哈希唯一)."""
    from kg.schema import init_db
    db = tmp_path / "t.db"
    init_db(db)
    e = {"policy_id": "same_id", "title": "t", "affected_industries": []}
    id1 = ingest.insert_policy(e, source_text="内容A", db_path=db)
    id2 = ingest.insert_policy(e, source_text="内容B", db_path=db)
    assert id1 != id2  # 即使 LLM 给相同 policy_id, 内容哈希不同 → 不同 node


def test_insert_policy_松耦合空输入():
    """空 extracted → None (不抛)."""
    assert ingest.insert_policy(None) is None
    assert ingest.insert_policy({}) is None


def test_insert_policy_affects边入库(tmp_path):
    """Policy 节点 + AFFECTS 边都入 (sector_code 命中清单)."""
    import sqlite3

    from kg.schema import init_db
    db = tmp_path / "t.db"
    init_db(db)
    extracted = {
        "policy_id": "p1", "title": "测",
        "affected_industries": [
            {"sector_code": "881319.SH", "industry_name": "半导体", "direction": "利好",
             "strength": "strong", "evidence_quote": "原文"}
        ],
    }
    ingest.insert_policy(extracted, source_text="原文", db_path=db)
    conn = sqlite3.connect(str(db))
    n_policy = conn.execute("SELECT COUNT(*) FROM kg_nodes WHERE node_type='policy'").fetchone()[0]
    n_affects = conn.execute("SELECT COUNT(*) FROM kg_edges WHERE edge_type='affects'").fetchone()[0]
    conn.close()
    assert n_policy == 1
    assert n_affects == 1  # 一条 AFFECTS 边 (Policy→881319)
