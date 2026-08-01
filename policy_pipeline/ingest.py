"""policy_pipeline ingest — 抽取结果 → kg AFFECTS 边入库 (P1a-4, 计划书 §6)。

Policy 节点 + 通达信行业节点(industry_tdx) + AFFECTS 边(Policy→Industry, 带方向/力度/证据).
内容哈希去重 (同政策不重复入). 失败不抛 (松耦合).

公开接口:
- insert_policy(extracted, source_text) 入库 Policy + AFFECTS 边
- ingest_policy_text(title, text) 一键: 抽取(DeepSeek) + 入库
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from kg.schema import init_db
from policy_pipeline.extractor import extract_policy
from utils.logger import get_logger

logger = get_logger(__name__)

_STRENGTH_W = {"strong": 1.0, "medium": 0.6, "weak": 0.3}
_DIR_SIGN = {"利好": 1, "利空": -1, "中性": 0}


def _content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def insert_policy(extracted: dict, source_text: str = "", db_path=None) -> Optional[str]:
    """抽取结果 → Policy 节点 + AFFECTS 边. 返 policy_id 或 None (失败松耦合).

    weight = 力度分(strong1.0/medium0.6/weak0.3) × 方向(利好+1/利空-1/中性0).
    """
    if not extracted:
        return None
    conn = init_db(db_path)
    try:
        chash = _content_hash(source_text)
        policy_node = f"policy:{chash}"  # 用内容哈希做唯一 key, 防 LLM policy_id 冲突
        pid = extracted.get("policy_id") or chash  # pid 作 code/name(显示), 不做 node_id
        # 同政策(同内容哈希)不重复入
        existing = conn.execute("SELECT 1 FROM kg_nodes WHERE node_id=?", (policy_node,)).fetchone()
        if existing:
            logger.info(f"政策已入库 (内容哈希命中): {pid}")
            return policy_node
        title = extracted.get("title", "")[:200]
        payload = {**extracted, "content_hash": chash, "source_text_preview": (source_text or "")[:200]}
        conn.execute(
            "INSERT OR REPLACE INTO kg_nodes (node_id, node_type, code, name, payload_json) VALUES (?,?,?,?,?)",
            (policy_node, "policy", pid, title, json.dumps(payload, ensure_ascii=False)),
        )
        for ind in extracted.get("affected_industries", []):
            sc = ind.get("sector_code")
            if not sc:
                continue  # 清单外的在 other_industries, 不入 AFFECTS
            direction = ind.get("direction", "中性")
            weight = _STRENGTH_W.get(ind.get("strength", ""), 0.5) * _DIR_SIGN.get(direction, 0)
            edge_payload = {
                "direction": direction,
                "strength": ind.get("strength"),
                "evidence_quote": ind.get("evidence_quote", ""),
                "industry_name": ind.get("industry_name"),
            }
            conn.execute(
                "INSERT OR IGNORE INTO kg_edges (src_id, dst_id, edge_type, weight, payload_json) VALUES (?,?,?,?,?)",
                (policy_node, f"industry_tdx:{sc}", "affects", weight,
                 json.dumps(edge_payload, ensure_ascii=False)),
            )
        conn.commit()
        return policy_node  # 返唯一 node_id (内容哈希), 不是 LLM pid
    except Exception as e:
        logger.warning(f"insert_policy 失败(松耦合): {e}")
        conn.rollback()
        return None
    finally:
        conn.close()


def ingest_policy_text(title: str, text: str, db_path=None) -> Optional[dict]:
    """一键: 抽取(DeepSeek) + 入库. 返 extracted 或 None (松耦合)."""
    r = extract_policy(title, text)
    if r:
        insert_policy(r, source_text=text, db_path=db_path)
    return r
