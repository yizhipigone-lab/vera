"""obsidian.export — kg/graph.db → Obsidian vault 导出 (计划书 P2, 只读渲染层)。

用法:
    python -m obsidian.export --db kg/graph.db --out ~/vera_vault [--max-nodes 100]

幂等策略: 目标目录下维护清单文件 .vera_export_manifest.json (记录上一次导出生成的文件)。
每次导出先按清单删除本次不再生成的旧 md, 再逐文件覆盖写入 —— 不触碰用户自建文件,
重跑不产生重复文件, 同日重跑内容逐字节一致 (导出日期同一天)。

只读: SQLite 连接用 file:...?mode=ro URI, 绝不写 kg/graph.db。
松耦合: db 不存在 / 表缺失或为空 / jinja2 缺失或模板渲染失败 → warning + 优雅降级
(降级为内置纯 Python 渲染器), 返回统计 dict 不抛异常。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

from utils.logger import get_logger

logger = get_logger(__name__)

_MANIFEST = ".vera_export_manifest.json"
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# frontmatter 保留字段, payload 标量展开时避让 (冲突加 payload_ 前缀)
_RESERVED_KEYS = {"node_id", "node_type", "code", "name", "export_date"}
# Windows/Obsidian 文件名非法字符 → '_', 保证 node_id → 文件名稳定映射
_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|]')
# YAML 简单 key 白名单, 不合法的 payload key 不进 frontmatter (仍在正文属性里)
_VALID_YAML_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sanitize_stem(node_id: str) -> str:
    """node_id → 稳定 md 文件名 (不含 .md)。'industry_tdx:881001.SH' → 'industry_tdx_881001.SH'。"""
    return _ILLEGAL_FILENAME.sub("_", node_id).strip() or "unnamed"


def _yaml_scalar(v) -> str:
    """标量 → YAML 安全字面量。字符串走 json.dumps (合法 YAML 双引号标量)。"""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """只读连接 (file:...?mode=ro URI), 绝不写 db, 也不产生 -wal 文件。"""
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_graph(db_path: Path, max_nodes: int | None,
                types: list[str] | None = None):
    """读节点 + 两端都在导出席位内的边 (悬空边/截断边忽略, 避免 vault 里大量死链)。

    types: 只导出这些 node_type (如 ["policy","industry_tdx"])。9.5 万 product
    节点全量导出会让 Obsidian 图谱卡死, 正式使用建议按类型过滤。
    """
    conn = _connect_ro(db_path)
    try:
        sql = "SELECT node_id, node_type, code, name, payload_json FROM kg_nodes"
        if types:
            ph = ",".join("?" for _ in types)
            sql += f" WHERE node_type IN ({ph})"
        sql += " ORDER BY node_type, node_id"
        if max_nodes:
            sql += f" LIMIT {int(max_nodes)}"
        nodes = conn.execute(sql, types or []).fetchall()
        ids = {r["node_id"] for r in nodes}
        edges = [
            e for e in conn.execute(
                "SELECT src_id, dst_id, edge_type, weight FROM kg_edges"
            ).fetchall()
            if e["src_id"] in ids and e["dst_id"] in ids
        ]
    finally:
        conn.close()
    return nodes, edges


def _node_view(row, out_edges, in_edges, name_of):
    """节点行 → 模板视图 dict: frontmatter 字段 (标量展开) + 属性摘要 + 出边/入边。"""
    try:
        payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
        if not isinstance(payload, dict):
            payload = {}
    except (json.JSONDecodeError, TypeError):
        payload = {}

    fields, payload_lines = {}, []
    for k, v in payload.items():
        if isinstance(v, (dict, list)):
            # 嵌套结构跳过 frontmatter, 正文里留截断 JSON 摘要
            s = json.dumps(v, ensure_ascii=False)
            payload_lines.append((k, s[:80] + ("…" if len(s) > 80 else "")))
            continue
        payload_lines.append((k, v))
        if _VALID_YAML_KEY.match(str(k)):
            key = str(k) if str(k) not in _RESERVED_KEYS else f"payload_{k}"
            fields[key] = v

    def edge_view(e, other_key):
        other = e[other_key]
        return {
            "stem": sanitize_stem(other),
            "name": name_of.get(other, other),
            "edge_type": e["edge_type"],
            "weight_str": f"{e['weight']:.4g}" if e["weight"] is not None else "1",
        }

    return {
        "node_id": row["node_id"],
        "node_type": row["node_type"],
        "code": row["code"],
        "name": row["name"],
        "fields": fields,
        "payload_lines": payload_lines,
        "out_edges": [edge_view(e, "dst_id") for e in out_edges],
        "in_edges": [edge_view(e, "src_id") for e in in_edges],
    }


def _build_env():
    """加载 jinja2 环境 + 预编译模板; 任一失败 → 返 None (降级内置渲染器)。"""
    try:
        from jinja2 import Environment, FileSystemLoader

        env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), keep_trailing_newline=True)
        env.filters["yaml_scalar"] = _yaml_scalar
        env.get_template("node.md.j2")
        env.get_template("index.md.j2")
        return env
    except Exception as e:  # jinja2 缺失 / 模板缺失或语法错 → 松耦合降级
        logger.warning(f"jinja2/模板不可用, 降级内置渲染器: {e}")
        return None


def _render_node(env, view, export_date: str) -> str:
    if env is not None:
        try:
            return env.get_template("node.md.j2").render(node=view, export_date=export_date)
        except Exception as e:
            logger.warning(f"模板渲染失败, 降级内置渲染 (node={view['node_id']}): {e}")
    return _render_node_fallback(view, export_date)


def _render_index(env, ctx: dict) -> str:
    if env is not None:
        try:
            return env.get_template("index.md.j2").render(**ctx)
        except Exception as e:
            logger.warning(f"索引模板渲染失败, 降级内置渲染: {e}")
    return _render_index_fallback(ctx)


def _edge_md(e) -> str:
    """单条边的 markdown 行 (模板与降级渲染器共用同一格式)。"""
    return f"- [[{e['stem']}|{e['name']}]] · `{e['edge_type']}` · weight={e['weight_str']}"


def _render_node_fallback(view, export_date: str) -> str:
    """内置纯 Python 渲染 (jinja2 缺失/失败时兜底, 输出结构与模板一致)。"""
    fm = ["---", f"node_id: {_yaml_scalar(view['node_id'])}",
          f"node_type: {_yaml_scalar(view['node_type'])}",
          f"code: {_yaml_scalar(view['code'])}",
          f"name: {_yaml_scalar(view['name'])}",
          f"export_date: {_yaml_scalar(export_date)}"]
    fm += [f"{k}: {_yaml_scalar(v)}" for k, v in view["fields"].items()]
    fm.append("---")
    body = [f"# {view['name']}", "",
            f"> 类型 `{view['node_type']}`"
            + (f" · 代码 `{view['code']}`" if view["code"] else "")
            + f" · 节点 `{view['node_id']}`", "",
            "## 属性"]
    body += [f"- **{k}**: {v}" for k, v in view["payload_lines"]] or ["- (无)"]
    body += ["", f"## 出边 ({len(view['out_edges'])})"]
    body += [_edge_md(e) for e in view["out_edges"]] or ["- (无)"]
    body += ["", f"## 入边 ({len(view['in_edges'])})"]
    body += [_edge_md(e) for e in view["in_edges"]] or ["- (无)"]
    return "\n".join(fm) + "\n\n" + "\n".join(body) + "\n"


def _render_index_fallback(ctx: dict) -> str:
    lines = ["# VERA 知识图谱索引", "",
             f"- 导出时间: {ctx['export_time']}",
             f"- 节点总数: {ctx['total_nodes']}",
             f"- 边总数 (导出席位内): {ctx['total_edges']}", "",
             "## 各类型节点计数"]
    lines += [f"- `{t}`: {c}" for t, c in ctx["type_counts"]]
    lines += ["", "## 说明",
              "- 数据源: kg/graph.db (SQLite 只读渲染, 真相源在 db)",
              '- Dataview 示例: `TABLE node_type, code WHERE node_type = "policy"`']
    return "\n".join(lines) + "\n"


def _prune_stale(out_dir: Path, current: set[str]) -> None:
    """按清单删除上次生成、本次不再生成的旧 md, 清理空目录, 重写清单 (幂等核心)。"""
    manifest = out_dir / _MANIFEST
    prev: set[str] = set()
    if manifest.exists():
        try:
            prev = set(json.loads(manifest.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"清单读取失败, 跳过旧文件清理: {e}")
    for rel in prev - current:
        try:
            (out_dir / rel).unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"旧文件删除失败 {rel}: {e}")
    for d in sorted(p for p in out_dir.iterdir() if p.is_dir()):
        try:
            d.rmdir()  # 仅空目录能删掉, 用户自建内容不受影响
        except OSError:
            pass
    manifest.write_text(json.dumps(sorted(current), ensure_ascii=False, indent=1), encoding="utf-8")


def export_vault(db_path: str | Path = "kg/graph.db", out_dir: str | Path = "~/vera_vault",
                 max_nodes: int | None = None, export_date: str | None = None,
                 types: list[str] | None = None) -> dict:
    """导出 kg 图谱为 Obsidian vault。返回 {nodes, edges, files} 统计。

    types: 只导出指定 node_type (如 ["policy", "industry_tdx", "industry", "company"],
    跳过 9.5 万 product 噪声节点)。松耦合: db 不存在 / 读库失败 / 表为空 →
    warning + 返回全 0, 不抛异常。
    """
    stats = {"nodes": 0, "edges": 0, "files": 0}
    db_path, out_dir = Path(db_path), Path(out_dir).expanduser()
    if not db_path.exists():
        logger.warning(f"kg db 不存在, 跳过导出 (松耦合): {db_path}")
        return stats
    try:
        nodes, edges = _load_graph(db_path, max_nodes, types)
    except sqlite3.Error as e:
        logger.warning(f"kg db 读取失败 (松耦合返空): {e}")
        return stats
    if not nodes:
        logger.warning(f"kg_nodes 为空, 无内容可导出: {db_path}")
        return stats

    export_date = export_date or date.today().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    env = _build_env()

    name_of = {r["node_id"]: r["name"] for r in nodes}
    out_map: dict[str, list] = {}
    in_map: dict[str, list] = {}
    for e in sorted(edges, key=lambda e: (e["edge_type"], e["src_id"], e["dst_id"])):
        out_map.setdefault(e["src_id"], []).append(e)
        in_map.setdefault(e["dst_id"], []).append(e)

    written: set[str] = set()
    for row in nodes:
        view = _node_view(row, out_map.get(row["node_id"], []),
                          in_map.get(row["node_id"], []), name_of)
        rel = f"{row['node_type']}/{sanitize_stem(row['node_id'])}.md"
        p = out_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_render_node(env, view, export_date), encoding="utf-8")
        written.add(rel)

    type_counts: dict[str, int] = {}
    for row in nodes:
        type_counts[row["node_type"]] = type_counts.get(row["node_type"], 0) + 1
    ctx = {"export_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "total_nodes": len(nodes), "total_edges": len(edges),
           "type_counts": sorted(type_counts.items())}
    (out_dir / "_index.md").write_text(_render_index(env, ctx), encoding="utf-8")
    written.add("_index.md")

    _prune_stale(out_dir, written)
    stats.update(nodes=len(nodes), edges=len(edges), files=len(written))
    logger.info(f"obsidian vault 导出完成: {stats} -> {out_dir}")
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian.export", description="kg/graph.db → Obsidian vault 导出 (只读渲染层)")
    parser.add_argument("--db", default="kg/graph.db", help="kg SQLite 路径 (默认 kg/graph.db)")
    parser.add_argument("--out", default="~/vera_vault", help="vault 输出目录 (默认 ~/vera_vault)")
    parser.add_argument("--max-nodes", type=int, default=None, help="最多导出节点数 (调试用)")
    parser.add_argument("--types", default=None,
                        help="只导出这些 node_type, 逗号分隔 (如 policy,industry_tdx,industry,company; 跳过 9.5 万 product)")
    args = parser.parse_args(argv)
    types = [t.strip() for t in args.types.split(",") if t.strip()] if args.types else None
    stats = export_vault(db_path=args.db, out_dir=args.out, max_nodes=args.max_nodes,
                         types=types)
    print(f"导出完成: nodes={stats['nodes']} edges={stats['edges']} "
          f"files={stats['files']} -> {Path(args.out).expanduser()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
