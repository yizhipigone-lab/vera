"""kg 查询 — 票/持仓 → 产业链穿透 (政策研究平台 P0, 计划书 §6 P0 步骤3 闸门)。

P0 闸门: 持仓票查到上下游产品/公司, 数字合理。
松耦合: 图谱空/票不在 → 返 None / 空列表, 不抛 (serialize 不加 key)。
code 归一化: 接受带后缀 (159226.SZ) 或不带 (159226), 内部 normalize_code 查。

公开接口 (≤8, 计划书 §3):
- get_company(stock_code) → 公司节点 | None
- get_company_industry(stock_code) → [{code,name}]
- get_company_products(stock_code, top_n) → [{product,weight}]
- get_product_upstream(product_name, max_depth) → [{level,upstream}]  (递归 CTE)
- get_holdings_chain(stock_code) → 穿透 dict | None  (★P0 闸门)
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Optional

from kg.schema import get_db_path, normalize_code
from utils.logger import get_logger

logger = get_logger(__name__)


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or get_db_path()))
    conn.row_factory = sqlite3.Row
    return conn


def _query(sql: str, params: tuple = (), db_path: Path | None = None) -> list:
    """连接→执行→关连接 样板 (5 处共用)。contextlib.closing 保证异常路径也关连接。
    返回全部行 (sqlite3.Row); 异常不吞, 由调用方 catch 转松耦合返回。"""
    with contextlib.closing(_connect(db_path)) as conn:
        return conn.execute(sql, params).fetchall()


def _company_node_id(stock_code: str) -> str:
    """票代码 → company node_id. 接受带/不带后缀."""
    return f"company:{normalize_code(stock_code)}"


def get_company(stock_code: str, db_path: Path | None = None) -> Optional[dict]:
    """票 → 公司节点. 不在图谱返 None (松耦合)."""
    try:
        rows = _query(
            "SELECT node_id, code, name, payload_json FROM kg_nodes WHERE node_id=?",
            (_company_node_id(stock_code),), db_path)
        if not rows:
            return None
        row = rows[0]
        return {
            "node_id": row["node_id"], "code": row["code"], "name": row["name"],
            "payload": json.loads(row["payload_json"] or "{}"),
        }
    except sqlite3.Error as e:
        logger.warning(f"get_company 查询失败(松耦合返None): {e}")
        return None


def get_company_industry(stock_code: str, db_path: Path | None = None) -> list[dict]:
    """票 → 所属行业列表 (一票可能多行业). 失败返 []."""
    try:
        rows = _query(
            """SELECT n.code, n.name FROM kg_edges e
               JOIN kg_nodes n ON e.dst_id = n.node_id
               WHERE e.src_id=? AND e.edge_type='belongs_to'""",
            (_company_node_id(stock_code),), db_path)
        return [{"code": r["code"], "name": r["name"]} for r in rows]
    except sqlite3.Error as e:
        logger.warning(f"get_company_industry 失败(返[]): {e}")
        return []


def get_company_products(stock_code: str, db_path: Path | None = None, top_n: int = 5) -> list[dict]:
    """票 → 主营产品 (按 rel_weight 降序, top_n). 失败返 []."""
    try:
        rows = _query(
            """SELECT n.name, e.weight FROM kg_edges e
               JOIN kg_nodes n ON e.dst_id = n.node_id
               WHERE e.src_id=? AND e.edge_type='main_product'
               ORDER BY e.weight DESC LIMIT ?""",
            (_company_node_id(stock_code), top_n), db_path)
        return [{"product": r["name"], "weight": r["weight"]} for r in rows]
    except sqlite3.Error as e:
        logger.warning(f"get_company_products 失败(返[]): {e}")
        return []


def get_product_upstream(product_name: str, db_path: Path | None = None, max_depth: int = 2) -> list[dict]:
    """产品 → 上游材料 (递归 CTE 多跳). 返回 [{level, upstream}], level=1 直接上游.

    max_depth 限深度防爆 (产品图可能有大组件连通).
    """
    try:
        rows = _query(
            """
            WITH RECURSIVE upstream(level, node_id) AS (
                SELECT 0, ?
                UNION
                SELECT u.level + 1, e.dst_id
                FROM upstream u JOIN kg_edges e ON e.src_id = u.node_id
                WHERE e.edge_type = 'product_upstream' AND u.level < ?
            )
            SELECT DISTINCT u.level, n.name FROM upstream u
            JOIN kg_nodes n ON u.node_id = n.node_id
            WHERE u.level > 0
            ORDER BY u.level, n.name
            """,
            (f"product:{product_name}", max_depth), db_path)
        return [{"level": r["level"], "upstream": r["name"]} for r in rows]
    except sqlite3.Error as e:
        logger.warning(f"get_product_upstream 失败(返[]): {e}")
        return []


def get_holdings_chain(stock_code: str, db_path: Path | None = None) -> Optional[dict]:
    """★P0 闸门: 票 → 产业链穿透.

    返回 {stock, company, industry:[...], products:[{product,weight,upstream:[...]}],
          upstream_companies:[{code,name,via_product}]}
    票不在图谱返 None。
    """
    company = get_company(stock_code, db_path)
    if not company:
        return None

    industry = get_company_industry(stock_code, db_path)
    products = get_company_products(stock_code, top_n=5, db_path=db_path)
    for p in products:
        p["upstream"] = get_product_upstream(p["product"], max_depth=1, db_path=db_path)[:5]

    # 上游公司反查: 主营产品的上游材料, 还有哪些公司主营
    upstream_companies = []
    seen = set()
    try:
        for p in products:
            for up in p["upstream"]:
                rows = _query(
                    """SELECT DISTINCT n.code, n.name FROM kg_edges e
                       JOIN kg_nodes n ON e.src_id = n.node_id
                       WHERE e.dst_id=? AND e.edge_type='main_product' LIMIT 5""",
                    (f"product:{up['upstream']}",), db_path)
                for r in rows:
                    key = (r["code"], up["upstream"])
                    if key in seen:
                        continue
                    seen.add(key)
                    upstream_companies.append({
                        "code": r["code"], "name": r["name"],
                        "via_product": up["upstream"],
                    })
    except sqlite3.Error as e:
        logger.warning(f"upstream_companies 反查失败(返[]): {e}")

    return {
        "stock": normalize_code(stock_code),
        "company": company,
        "industry": industry,
        "products": products,
        "upstream_companies": upstream_companies[:10],  # 限 10 防爆
    }


if __name__ == "__main__":
    # CLI: python -m kg.query <票代码>   例: python -m kg.query 600259.SH
    import sys

    if len(sys.argv) < 2:
        print("用法: python -m kg.query <票代码>   例: python -m kg.query 600259.SH")
        print("  票代码带后缀 (600259.SH) 或不带 (600259) 都行")
        sys.exit(1)

    code = sys.argv[1]
    r = get_holdings_chain(code)
    if not r:
        print(f"[ {code} ] 不在知识图谱 (可能 ETF/可转债/新股/退市/未覆盖)")
        sys.exit(0)

    comp = r["company"]["payload"]
    print(f"\n=== {r['company']['name']} ({r['stock']}) ===")
    if comp.get("fullname"):
        print(f"全称: {comp['fullname']}  上市: {comp.get('time', '')}  {comp.get('location', '')}")
    print(f"行业: {[i['name'] for i in r['industry']]}")
    print("\n主营产品 + 上游材料:")
    for p in r["products"]:
        ups = [u["upstream"] for u in p["upstream"]]
        line = f"  - {p['product']} (权重 {p['weight']:.2f})"
        if ups:
            line += f"  ->  上游: {ups}"
        print(line)
    if r["upstream_companies"]:
        print("\n上游公司反查 (谁还主营这些材料, top5):")
        for c in r["upstream_companies"][:5]:
            print(f"  - {c['name']} ({c['code']})  via {c['via_product']}")
