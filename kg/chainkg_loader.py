"""kg ChainKnowledgeGraph 数据导入 (政策研究平台 P0, 计划书 §6 P0 步骤2)。

7 个 JSONL → kg_nodes + kg_edges。code 归一化 (去后缀)。
幂等: 重跑先清后导 (DELETE THEN INSERT)。
坏行/缺字段记 warning 跳过 (松耦合, 不抛)。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from kg.schema import init_db, normalize_code
from utils.logger import get_logger

logger = get_logger(__name__)

_DATA_DIR = Path(__file__).resolve().parent / "data"


def _read_jsonl(path: Path) -> Iterable[dict]:
    """读 JSONL (每行一个 json). 跳过空行/坏行."""
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _node_id(node_type: str, key: str) -> str:
    """构造节点主键: 'company:301079' / 'industry:110000' / 'product:特种气体'."""
    return f"{node_type}:{key}"


def _safe_float(x, default: float = 1.0) -> float:
    """float() 兜底: 转不了给默认值 (坏行不炸, 两处共用)。"""
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _insert_node(conn, node_type: str, key: str, code: str | None,
                 name: str, obj: dict) -> None:
    """节点 INSERT OR REPLACE 样板 (company/industry/product 三处共用)。"""
    conn.execute(
        "INSERT OR REPLACE INTO kg_nodes (node_id, node_type, code, name, payload_json) VALUES (?,?,?,?,?)",
        (_node_id(node_type, key), node_type, code, name,
         json.dumps(obj, ensure_ascii=False)),
    )


def _insert_edge(conn, src_id: str, dst_id: str, edge_type: str,
                 weight: float, obj: dict) -> None:
    """边 INSERT OR IGNORE 样板 (4 处共用)。"""
    conn.execute(
        "INSERT OR IGNORE INTO kg_edges (src_id, dst_id, edge_type, weight, payload_json) VALUES (?,?,?,?,?)",
        (src_id, dst_id, edge_type, weight, json.dumps(obj, ensure_ascii=False)),
    )


def _ensure_product_node(conn, name: str) -> None:
    """补建 product 节点 (company_product/product_product 涉及的产品可能不在 product.json)."""
    if not name:
        return
    conn.execute(
        "INSERT OR IGNORE INTO kg_nodes (node_id, node_type, code, name, payload_json) VALUES (?,?,?,?,?)",
        (_node_id("product", name), "product", None, name,
         json.dumps({"name": name}, ensure_ascii=False)),
    )


def load_all(data_dir: Path | None = None, db_path: Path | None = None) -> dict:
    """导入全部 7 文件 → SQLite. 返回统计 {nodes, edges, skipped}。

    幂等: 重跑覆盖 (先清 kg_nodes/kg_edges 再导)。
    """
    data_dir = data_dir or _DATA_DIR
    if not data_dir.exists():
        raise FileNotFoundError(f"kg data 目录不存在: {data_dir}")

    conn = init_db(db_path)
    stats = {"nodes": 0, "edges": 0, "skipped": 0}

    # 幂等: 先清 (重跑覆盖, 不留旧)
    conn.execute("DELETE FROM kg_nodes")
    conn.execute("DELETE FROM kg_edges")

    # 1. company 节点 (code 不带后缀)
    for obj in _read_jsonl(data_dir / "company.json"):
        code = normalize_code(obj.get("code", ""))
        if not code:
            stats["skipped"] += 1
            continue
        _insert_node(conn, "company", code, code, obj.get("name", ""), obj)
        stats["nodes"] += 1

    # 2. industry 节点 (code 6 位)
    for obj in _read_jsonl(data_dir / "industry.json"):
        code = obj.get("code", "")
        if not code:
            stats["skipped"] += 1
            continue
        _insert_node(conn, "industry", code, code, obj.get("name", ""), obj)
        stats["nodes"] += 1

    # 3. product 节点 (无 code, name 作 key)
    for obj in _read_jsonl(data_dir / "product.json"):
        name = obj.get("name", "").strip()
        if not name:
            stats["skipped"] += 1
            continue
        _insert_node(conn, "product", name, None, name, obj)
        stats["nodes"] += 1

    # 4. company → industry 边 (belongs_to)
    for obj in _read_jsonl(data_dir / "company_industry.json"):
        src_code = normalize_code(obj.get("company_code", ""))
        ind_code = obj.get("industry_code", "")
        if not src_code or not ind_code:
            stats["skipped"] += 1
            continue
        _insert_edge(conn, _node_id("company", src_code),
                     _node_id("industry", ind_code), "belongs_to", 1.0, obj)
        stats["edges"] += 1

    # 5. company → product 边 (main_product, 带 rel_weight)
    for obj in _read_jsonl(data_dir / "company_product.json"):
        src_code = normalize_code(obj.get("company_code", ""))
        prod_name = obj.get("product_name", "").strip()
        if not src_code or not prod_name:
            stats["skipped"] += 1
            continue
        _ensure_product_node(conn, prod_name)  # 补建 product 节点
        weight = _safe_float(obj.get("rel_weight", 1.0))
        _insert_edge(conn, _node_id("company", src_code),
                     _node_id("product", prod_name), "main_product", weight, obj)
        stats["edges"] += 1

    # 6. industry → industry 上游边 (industry_up: {industry, ups:{name:freq}})
    # industry_up 用 name, 要映射回 code (查 kg_nodes)
    for obj in _read_jsonl(data_dir / "industry_up.json"):
        ind_name = obj.get("industry", "")
        if not ind_name:
            stats["skipped"] += 1
            continue
        src_row = conn.execute(
            "SELECT code FROM kg_nodes WHERE node_type='industry' AND name=?", (ind_name,)
        ).fetchone()
        if not src_row:
            stats["skipped"] += 1
            continue
        src_code = src_row[0]
        for up_name, freq in (obj.get("ups") or {}).items():
            dst_row = conn.execute(
                "SELECT code FROM kg_nodes WHERE node_type='industry' AND name=?", (up_name,)
            ).fetchone()
            if not dst_row:
                continue
            w = _safe_float(freq)
            _insert_edge(conn, _node_id("industry", src_code),
                         _node_id("industry", dst_row[0]), "industry_upstream", w,
                         {"industry": ind_name, "upstream": up_name, "freq": freq})
            stats["edges"] += 1

    # 7. product → product 上游边 (product_product: {from_entity, rel, to_entity})
    for obj in _read_jsonl(data_dir / "product_product.json"):
        from_name = obj.get("from_entity", "").strip()
        to_name = obj.get("to_entity", "").strip()
        if not from_name or not to_name:
            stats["skipped"] += 1
            continue
        _ensure_product_node(conn, from_name)
        _ensure_product_node(conn, to_name)
        _insert_edge(conn, _node_id("product", from_name),
                     _node_id("product", to_name), "product_upstream", 1.0, obj)
        stats["edges"] += 1

    conn.commit()
    conn.close()
    logger.info(
        f"kg 导入完成: nodes={stats['nodes']}, edges={stats['edges']}, skipped={stats['skipped']}"
    )
    return stats


if __name__ == "__main__":
    # python -m kg.chainkg_loader
    s = load_all()
    print(f"[OK] 导入完成: {s}")
