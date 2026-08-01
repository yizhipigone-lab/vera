"""kg schema — SQLite 存图表定义 (政策研究平台 P0, 计划书 §3/§6/§9 M-8)。

SCHEMA_VERSION: schema 漂移时 +1 触发迁移 (借鉴 selection_cache.SCHEMA_VERSION)。
init_db 幂等 (IF NOT EXISTS), 重复调用安全。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from utils.logger import get_logger

logger = get_logger(__name__)

# 计划书 §9 M-8: schema 漂移时 +1, 触发迁移脚本 (防历史 edges 丢失)
SCHEMA_VERSION = 1

_DB_PATH = Path(__file__).resolve().parent / "graph.db"


def get_db_path() -> Path:
    """kg 图谱 SQLite 路径 (kg/graph.db)."""
    return _DB_PATH


def init_db(db_path: Path | None = None) -> sqlite3.Connection:
    """建表 + 索引 (IF NOT EXISTS, 幂等). 返回连接 (调用方负责 close)."""
    db_path = db_path or _DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        f"""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS kg_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        INSERT OR IGNORE INTO kg_meta (key, value) VALUES ('schema_version', '{SCHEMA_VERSION}');

        CREATE TABLE IF NOT EXISTS kg_nodes (
            node_id TEXT PRIMARY KEY,       -- 'company:301079' / 'industry:110000' / 'product:特种气体'
            node_type TEXT NOT NULL,        -- 'company' / 'industry' / 'product'
            code TEXT,                      -- company/industry code (不带后缀), product 为 NULL
            name TEXT NOT NULL,
            payload_json TEXT               -- 完整原始 JSON (溯源用)
        );
        CREATE INDEX IF NOT EXISTS idx_nodes_code ON kg_nodes(code);
        CREATE INDEX IF NOT EXISTS idx_nodes_name ON kg_nodes(name);
        CREATE INDEX IF NOT EXISTS idx_nodes_type ON kg_nodes(node_type);

        CREATE TABLE IF NOT EXISTS kg_edges (
            src_id TEXT NOT NULL,
            dst_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,        -- 'belongs_to' / 'main_product' / 'industry_upstream' / 'product_upstream'
            weight REAL DEFAULT 1.0,        -- main_product 用 rel_weight, industry_upstream 用频次, 其余 1.0
            payload_json TEXT,
            PRIMARY KEY (src_id, dst_id, edge_type)
        );
        CREATE INDEX IF NOT EXISTS idx_edges_src ON kg_edges(src_id);
        CREATE INDEX IF NOT EXISTS idx_edges_dst ON kg_edges(dst_id);
        CREATE INDEX IF NOT EXISTS idx_edges_type ON kg_edges(edge_type);
        """
    )
    conn.commit()
    return conn


def normalize_code(code: str) -> str:
    """归一化股票代码: 去后缀 → 6 位 code。

    ChainKG company.json 不带后缀 (301079), company_industry/company_product 带后缀 (600373.SH),
    VERA 持仓/trades 带后缀 (159226.SZ)。kg 内部统一用不带后缀 (company.json 原生格式, 最稳)。
    """
    if not code:
        return ""
    return str(code).split(".")[0].strip()
