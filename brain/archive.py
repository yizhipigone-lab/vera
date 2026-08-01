"""brain/archive.py — 对话归档: 每个问答追加进 Obsidian vault「对话沉淀」。

用户 2026-07-28 拍板: 对话 = 宝贵的思考过程, 必须沉淀为知识库。
UI 可随便清 (关服务器/关会话), vault 里的归档一个标点都不丢。

设计:
- 一条会话(channel) = 一个 md 文件, 文件名 = 日期_首问摘要.md,
  channel → 文件名映射存 data/brain_archive_map.json (同日多轮追加进同文件)。
- 成功/失败都归档 (失败标 [失败], 思考过程包含试错)。
- 松耦合: 归档任何异常只记 warning, 绝不影响回答本身。
- 隐私口径 (L-1 的边界): 归档只落盘本地 vault, 不进 prompt 不外发;
  不要把 vera_obs_vault 同步到云端 (里面有持仓讨论)。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

from utils.logger import get_logger
from utils.sysutil import load_json_or_empty, project_root

logger = get_logger(__name__)

_ROOT = project_root()
ARCHIVE_DIR = _ROOT / "vera_obs_vault" / "对话沉淀"
_MAP_PATH = _ROOT / "data" / "brain_archive_map.json"
_KG_DB = _ROOT / "kg" / "graph.db"

_LINK_SECTION = "\n## 关联节点\n"
# 段 = 标题 + 连续的 "- " 列表行; 新问答会追加在旧段之后, 剥段必须全文找
# (教训: split(标题)[0] 会把段后的新问答一起丢掉 —— 实测抓出)
_SECTION_RE = re.compile(r"\n## 关联节点\n(?:- [^\n]*\n?)+")
_MAX_LINKS = 40          # 每文件链接上限, 防"链接汤"
_CANDIDATES_CACHE: list | None = None  # 进程级缓存 (kg 名字基本不变)


def _sanitize_stem(node_id: str) -> str:
    """与 obsidian.export 同款文件名映射 (复用, 不写第二份)。"""
    from obsidian.export import sanitize_stem
    return sanitize_stem(node_id)


def _link_candidates(db_path: Path | None = None) -> list:
    """[(匹配词, 文件stem, 显示名, 类型)] 从 kg 读 industry_tdx/industry/company。

    company 额外按代码匹配 (文本里常出现 "300750"); 代码限 0/3/6 开头
    (A 股股票段), 防把日期/时间数字误当代码。db 缺失/读失败 → [] (松耦合,
    跳过打链不影响归档)。policy 只有几条且标题长, 不匹配。
    """
    global _CANDIDATES_CACHE
    if _CANDIDATES_CACHE is not None:
        return _CANDIDATES_CACHE
    import sqlite3
    db = db_path or _KG_DB
    out: list = []
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT node_id, node_type, code, name FROM kg_nodes "
                "WHERE node_type IN ('industry_tdx','industry','company')").fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"链接候选读取失败 (松耦合, 跳过打链): {e}")
        return out
    type_label = {"industry_tdx": "行业(通达信)", "industry": "行业(产业链)",
                  "company": "公司"}
    for node_id, ntype, code, name in rows:
        stem = _sanitize_stem(node_id)
        label = type_label[ntype]
        disp = name or code or node_id
        if name and len(name) >= 2:
            out.append((name, stem, disp, label))
        if ntype == "company" and code and code[0] in "036" and len(code) == 6:
            out.append((code, stem, disp, label))
    # 长词优先 (防"半导体"抢在"半导体设备"前命中后重复统计, 显示更精确)
    out.sort(key=lambda c: -len(c[0]))
    _CANDIDATES_CACHE = out
    return out


def _refresh_links(md_path: Path) -> None:
    """重算归档文件的 [[]] 双链段 (幂等: 剥旧段→全文扫→重写, 随追加自动更新)。"""
    cands = _link_candidates()
    if not cands or not md_path.exists():
        return
    text = md_path.read_text(encoding="utf-8")
    body = _SECTION_RE.sub("", text)  # 剥旧段 (可能不在末尾), 内容一字不丢
    hits: dict[str, tuple[str, str]] = {}   # stem → (disp, label), stem 去重
    for word, stem, disp, label in cands:
        if stem not in hits and word in body:
            hits[stem] = (disp, label)
        if len(hits) >= _MAX_LINKS:
            break
    if not hits:
        new_text = body
    else:
        by_label: dict[str, list[str]] = {}
        for stem, (disp, label) in hits.items():
            by_label.setdefault(label, []).append(f"[[{stem}|{disp}]]")
        lines = [_LINK_SECTION]
        for label in ("行业(通达信)", "行业(产业链)", "公司"):
            if label in by_label:
                lines.append(f"- {label}: " + " · ".join(by_label[label]) + "\n")
        new_text = body.rstrip("\n") + "\n" + "".join(lines)
    md_path.write_text(new_text, encoding="utf-8")

_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')
_WS = re.compile(r"\s+")


def _safe_stem(text: str, max_len: int = 12) -> str:
    """问题摘要 → 文件名安全片段 (清洗非法字符/压缩空白/截断)。"""
    s = _ILLEGAL.sub("", text or "")
    s = _WS.sub("", s).strip(" .")
    return s[:max_len] or "未命名"


def _file_for(channel: str, question: str, out_dir: Path) -> Path:
    """channel → 归档文件 (没有则按 日期_首问摘要 建名并记映射)。"""
    mapping = load_json_or_empty(_MAP_PATH)
    name = mapping.get(channel)
    if not name:
        day = dt.datetime.now().strftime("%Y-%m-%d")
        name = f"{day}_{_safe_stem(question)}.md"
        mapping[channel] = name
        _MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        _MAP_PATH.write_text(json.dumps(mapping, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    return out_dir / name


def archive_exchange(channel: str, question: str, result: dict,
                     out_dir: str | Path | None = None) -> Path | None:
    """归档一次问答。返归档文件路径; 失败返 None (松耦合, 不抛)。

    result: ask_brain 的返回 dict (answer/success/low_confidence/warnings)。
    """
    try:
        out = Path(out_dir) if out_dir else Path(ARCHIVE_DIR)
        out.mkdir(parents=True, exist_ok=True)
        f = _file_for(channel, question, out)

        now = dt.datetime.now()
        if not f.exists():
            f.write_text(
                "---\n"
                f'conv: "{channel}"\n'
                f'created: "{now.strftime("%Y-%m-%d")}"\n'
                "---\n\n"
                f"# 对话沉淀: {_safe_stem(question, 30)}\n",
                encoding="utf-8")

        ts = now.strftime("%H:%M")
        tags = ""
        if not result.get("success"):
            tags = " [失败]"
        elif result.get("low_confidence"):
            tags = " [低置信]"
        warns = result.get("warnings") or []
        warn_md = ("\n> ⚠ " + "；".join(str(w) for w in warns)) if warns else ""

        with open(f, "a", encoding="utf-8") as fp:
            fp.write(f"\n## {ts} 你问\n\n{question}\n\n"
                     f"## {ts} 大脑答{tags}\n\n{result.get('answer', '')}{warn_md}\n")
        _refresh_links(f)  # 每次归档即重扫: 提到的行业/公司自动打 [[]] 双链 (第二大脑)
        # 归档即同步语义索引（松耦合，失败不影响归档）
        if not os.environ.get("BRAIN_NO_INDEX_SYNC"):
            try:
                from brain.search_engine import VaultSearchEngine
                VaultSearchEngine().update_file(f)
            except Exception as e:
                logger.warning(f"语义索引增量更新失败 (松耦合, 不影响归档): {e}")
        return f
    except Exception as e:
        logger.warning(f"对话归档失败 (松耦合, 不影响回答): {e}")
        return None
