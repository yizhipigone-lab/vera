"""brain/search_engine.py — VERA vault 语义搜索引擎（fastembed ONNX + numpy）。

模型：BAAI/bge-small-zh-v1.5（fastembed ONNX 版，首次运行经 hf-mirror 镜像自动下载
~200MB 到 data/brain_model_cache/）。加载 ~1-3s，后续搜索毫秒级。无 torch 依赖（铁律4）。

用法（CLI）：
  python -m brain.search_engine build [--force]       # 全量构建索引
  python -m brain.search_engine search "查询词" [--top-k 8]  # 语义搜索
  python -m brain.search_engine update path/to/file.md       # 增量更新单文件
  python -m brain.search_engine eval                          # 检索质量评估

LLM 通过 Bash 调用以上 CLI（大脑 LLM 只有 Read/Grep/Glob/Bash 四个工具）。
所有输出直接打印到 stdout，格式为 LLM 可读的纯文本或 JSON。

松耦合：任何失败只降级（返 [] / 打印错误），不抛异常。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from collections import Counter
from pathlib import Path
from typing import Optional

import numpy as np

from utils.logger import get_logger
from utils.sysutil import ensure_utf8_stdout, project_root

logger = get_logger(__name__)

# ── 常量（换模型只改 MODEL_NAME） ──────────────────────────
MODEL_NAME = "BAAI/bge-small-zh-v1.5"  # fastembed 支持列表内的中文模型
EMBED_DIM = 512  # bge-small-zh-v1.5 输出维度
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
VERA_ROOT = project_root()
VAULT_ROOT = VERA_ROOT / "vera_obs_vault"
INDEX_DIR = VERA_ROOT / "data" / "brain_vectors"
VAULT_DIRS = ("company", "对话沉淀")
CHUNK_MIN = 200
CHUNK_MAX = 450
MODEL_CACHE_DIR = VERA_ROOT / "data" / "brain_model_cache"  # ONNX 模型缓存（已 gitignore）

#: `source` 口径的版本号（写进索引；`update_file` 入口校验）。
#: 2026-09-17 由「vault 相对」改成「**项目根相对**」——旧索引的 `对话沉淀/x.md`
#: 与新形态 `vera_obs_vault/对话沉淀/x.md` 不兼容：收口后、下一次 `build --force`
#: 之前若有归档调用 `update_file`，它按 source 精确匹配会**匹配不到旧 key**，
#: 于是旧块保留 + 新块追加 = **同一文件两个 key 并存**（计划书 §12.2 HIGH-5）。
#: 所以要有一道版本闸门：不匹配就**拒绝写入并提示先 build --force**（fail-closed）。
SOURCE_FORMAT = 2

# ── 全局状态（进程内缓存，线程安全） ──────────────────────
_model_lock = threading.Lock()
_model = None  # fastembed TextEmbedding 单例
_index_lock = threading.Lock()  # 保护 build/update 互斥


def _get_model():
    """fastembed ONNX 模型 lazy 单例（线程安全）。首次运行自动下载 ~200MB。

    国内网络两个环境变量（仅缺省设置，不覆盖用户已有配置）：
    - HF_ENDPOINT=hf-mirror.com：HuggingFace 直连被墙时的镜像
    - HF_HUB_DISABLE_XET=1：镜像不支持 xet 协议（大文件走 xet 会 401），强制普通 HTTP
    模型缓存固定在 data/brain_model_cache/，不用 fastembed 默认的 Temp 目录（会被系统清理）。
    """
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        from fastembed import TextEmbedding
        MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _model = TextEmbedding(model_name=MODEL_NAME, cache_dir=str(MODEL_CACHE_DIR))
        return _model


# ── 分块 ──────────────────────────────────────────────────
def _strip_frontmatter(text: str) -> str:
    """去掉文件开头的 YAML frontmatter（--- ... ---），减少 chunk/snippet 噪音。

    公司文件的 frontmatter 是机器元数据（node_id/export_date 等），
    喂给 LLM 的检索片段不该带它。非 frontmatter 开头原样返回。
    """
    if not text.startswith("---"):
        return text
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1:]).lstrip("\n")
    return text


def _chunk(text: str) -> list[str]:
    """按 \\n\\n 切段 → 合并小段 → 硬切长段 → 丢弃碎块 → 返回 200-450 字的块列表。"""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paras:
        return []

    # 合并相邻短段
    merged: list[str] = []
    buf = ""
    for p in paras:
        if not buf:
            buf = p
        elif len(buf) + len(p) <= CHUNK_MAX:
            buf += "\n\n" + p
        else:
            if len(buf) >= CHUNK_MIN:
                merged.append(buf)
            else:
                # 缓冲区太小，尝试和下一段合并
                buf = buf + "\n\n" + p
                if len(buf) <= CHUNK_MAX:
                    continue
                # 合并后超了，硬切
                merged.extend(_hard_split(buf))
                buf = ""
                continue
            buf = p

    if buf:
        if len(buf) >= 50:  # CHUNK_MIN(200) 被 >=50 完全蕴含，原 or 条件是冗余
            merged.append(buf)

    # 硬切所有超长块
    result: list[str] = []
    for m in merged:
        if len(m) <= CHUNK_MAX:
            result.append(m)
        else:
            result.extend(_hard_split(m))

    # 丢弃孤块（<50 字且无法归入前一块）
    final: list[str] = []
    for r in result:
        if len(r) < 50 and final:
            final[-1] += "\n" + r
        elif len(r) >= 50:
            final.append(r)
    return final


def _hard_split(text: str) -> list[str]:
    """超长文本按句号/换行硬切到每块 ≤ CHUNK_MAX。"""
    parts = re.split(r"(?<=[。！？\n])", text)
    chunks: list[str] = []
    buf = ""
    for p in parts:
        if len(buf) + len(p) <= CHUNK_MAX:
            buf += p
        else:
            if buf:
                chunks.append(buf)
            buf = p
            while len(buf) > CHUNK_MAX:
                # 单句仍然超长，强行截断
                chunks.append(buf[:CHUNK_MAX])
                buf = buf[CHUNK_MAX:]
    if buf:
        chunks.append(buf)
    return chunks


# ── 嵌入 ──────────────────────────────────────────────────
def _embed(texts: list[str], is_query: bool = False) -> np.ndarray:
    """文本列表 → float32 (N, 512) 已 L2 归一化矩阵。查询侧加 QUERY_PREFIX。"""
    if is_query:
        texts = [QUERY_PREFIX + t for t in texts]
    model = _get_model()
    vecs = np.asarray(list(model.embed(texts)), dtype=np.float32)
    # fastembed 不保证归一化，统一手动 L2（点积即余弦相似度的前提）
    norms = np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12
    return vecs / norms


# ── 落盘与加载 ────────────────────────────────────────────
def _to_source(path, root: Path | None = None) -> str:
    """**全项目唯一**的 `source` 计算出口：文件 → **项目根相对** posix 路径。

    铁律（计划书 §3.1/§3.2）：不许有第二个地方算 source。历史坑是两处口径分叉 ——
    `build_index` 产出 vault 相对路径（`company/x.md`），而 `update_file` 对
    vault **之外**的文件退化成**绝对路径**（`E:/1target/VERA/research/x.md`）。
    加了 `research/`/`docs/` 两个新语料根之后，同一个文件会出现两个 key，
    检索结果里同一篇文章挤占多个 top-k 名额。

    出口处**直接拒绝绝对路径**（§3.2 防线②）：不静默退化，抛错让调用方知道。
    公开方法（`update_file`/`build_index`）在外面兜住异常并降级，保持"失败不抛"。

    顺带的净改善（计划书 §3 第 3 点）：返回的路径**可以直接丢给 Read 打开**，
    以前返回 `company/x.md`，大脑拿到还得自己猜前面要补 `vera_obs_vault/`。
    """
    p = Path(path)
    base = Path(root or VERA_ROOT)
    try:
        p, base = p.resolve(), base.resolve()
    except Exception:       # 解析失败（路径不存在等）就用原样，后面照常判相对
        pass
    try:
        rel = p.relative_to(base).as_posix()
    except ValueError as e:
        raise ValueError(
            f"_to_source: {p} 不在项目根 {base} 下，拒绝生成 source"
            "（绝对路径会与新语料根的相对路径撞成两个 key）") from e
    if Path(rel).is_absolute() or re.match(r"^[A-Za-z]:", rel):
        raise ValueError(f"_to_source: 结果仍是绝对路径 {rel!r}，拒绝")
    return rel


def _source_format_of(index_dir: Optional[Path] = None) -> int | None:
    """读索引里记的 `source` 口径版本号；没有（旧索引/空索引）返 None。"""
    mp = _meta_path(index_dir)
    if not mp.exists():
        return None
    sp = mp.with_name("source_format.txt")
    if not sp.exists():
        return None
    try:
        return int(sp.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _write_source_format(index_dir: Optional[Path]) -> None:
    """**只在真正执行构建时**写版本号（计划书 §12.3 的仲裁）。

    绝不因为"看到 skipped 早退"就顺手更新版本号 —— 那会让旧格式索引假装自己是新的。
    """
    base = index_dir or INDEX_DIR
    base.mkdir(parents=True, exist_ok=True)
    sp = base / "source_format.txt"
    tmp = sp.with_suffix(".txt.tmp")
    tmp.write_text(str(SOURCE_FORMAT), encoding="utf-8")
    os.replace(tmp, sp)


def _embed_with_retry(chunks: list[str], tries: int = 2) -> np.ndarray:
    """嵌入一次失败再重试一次（§12.1：减少 skipped 来源）。仍失败则抛。"""
    last: Exception | None = None
    for _ in range(max(1, tries)):
        try:
            return _embed(chunks, is_query=False)
        except Exception as e:      # 首次失败多为模型首次加载抖动，重试一次值得
            last = e
    raise last if last else RuntimeError("嵌入失败")


# ── 索引范围（配置驱动；§4.1/§4.2/§12.6）───────────────────
#: 内置默认 = **现有行为**（只有 vault 两个目录），即"没配置 = 老行为"。
#: 配 `config/brain_index.json`（`.gitignore` 已忽略 `*.json`，不入库）可扩到
#: `research/` 与 `docs/` —— 用户 2026-09-17 拍板选 C（全量索引，接受把命中的
#: 片段发给模型厂商）。
INDEX_CONFIG_PATH = VERA_ROOT / "config" / "brain_index.json"
DEFAULT_INDEX_ROOTS = ("vera_obs_vault/company", "vera_obs_vault/对话沉淀")
#: 永不索引的目录（前缀匹配）。**机器生成物必须排掉**（§12.6）：
#: `docs/brief`（每日简报）、`docs/report`（回测产物）—— 不排的话系统**每天**生成的
#: 报告会被自己的 RAG 索引进去，日积月累污染检索库。
#:
#: **`docs/audit` 2026-09-17 M7 已移出排除名单（审计 F-09）**，理由有两个：
#:   ① 自相矛盾：提示词（`brain/prompts.py` 模式 A）要求"审计/根因类问题必须先搜库再答"，
#:      而最该被搜到的语料正是我们自己的审计报告 —— 把它们排在索引外，等于要求搜一个
#:      空的库；
#:   ② 排除理由不成立：`docs/audit` 里 113 篇 md 只有 10 篇是 `tools/formula_lab.py`
#:      按需生成的"因子体检报告"，其余 103 篇是审计/实施报告，**不是每天自动落盘**的
#:      东西（共 1.3 MB）。按需生成的报告也算"我们自己的研究库"，该被引用。
DEFAULT_EXCLUDE_DIRS = (".git", "__pycache__", "node_modules", "data", "output",
                        "logs", ".agent-teams", "vera_obs_vault/对话沉淀/_tmp",
                        "docs/brief", "docs/report")
#: 小于这么多字符的文件不索引（碎片）
DEFAULT_MIN_CHARS = 50


def _index_config() -> dict:
    """读索引范围配置；读不到回落内置默认（零行为变化）。"""
    cfg = {"roots": list(DEFAULT_INDEX_ROOTS),
           "exclude_dirs": list(DEFAULT_EXCLUDE_DIRS),
           "min_chars": DEFAULT_MIN_CHARS}
    try:
        raw = json.loads(INDEX_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(raw.get("roots"), list) and raw["roots"]:
            cfg["roots"] = [str(x) for x in raw["roots"]]
        if isinstance(raw.get("exclude_dirs"), list):
            cfg["exclude_dirs"] = [str(x) for x in raw["exclude_dirs"]]
        if isinstance(raw.get("min_chars"), int):
            cfg["min_chars"] = raw["min_chars"]
    except FileNotFoundError:
        pass                        # 没配置 = 老行为, 不告警
    except Exception as e:
        logger.warning(f"读 {INDEX_CONFIG_PATH} 失败, 回落内置默认: {e}")
    return cfg


def _is_excluded(path: Path, excludes: tuple[str, ...]) -> bool:
    """路径是否落在排除目录里（按项目根相对 posix 前缀 + 目录名双判）。"""
    try:
        rel = _to_source(path)
    except ValueError:
        return True                 # 项目根之外的一律不收
    parts = rel.split("/")
    for ex in excludes:
        ex = ex.strip("/")
        if not ex:
            continue
        if rel == ex or rel.startswith(ex + "/"):
            return True
        if "/" not in ex and ex in parts[:-1]:
            return True
    return False


def _disk_files() -> list[tuple[str, Path, float]]:
    """按配置扫出**该索引的全部文件** → `[(source, path, mtime), ...]`。

    递归遍历（`rglob`）+ 排除目录剪枝 + 最小字符数过滤。

    **为什么返回 list 而不是 dict**：dict 以 source 为键，两个文件撞成同一个 source 时
    会**静默丢掉一个**，防线③ 就永远看不到冲突。用 list 才能把冲突暴露出来。

    这是"磁盘清单"的唯一来源：构建与新鲜度检查共用它，不写第二份。
    """
    cfg = _index_config()
    excludes = tuple(cfg["exclude_dirs"])
    min_chars = int(cfg["min_chars"])
    out: list[tuple[str, Path, float]] = []
    for root_name in cfg["roots"]:
        root = VERA_ROOT / root_name
        if root.is_file() and root.suffix == ".md":
            cands = [root]
        elif root.is_dir():
            cands = sorted(root.rglob("*.md"))
        else:
            continue
        for f in cands:
            if _is_excluded(f, excludes):
                continue
            try:
                if f.stat().st_size < min_chars:
                    continue
                out.append((_to_source(f), f, f.stat().st_mtime))
            except Exception:
                continue
    return out


def _check_build_consistency(disk: list[tuple[str, Path, float]], meta: list[dict],
                            skipped: list[dict], file_count: int) -> list[str]:
    """防线③ 自检（§3.2 + §12.1 修订版）→ 问题清单（空 = 通过）。

    判据：**不同的 source 个数 == 成功处理的文件数**（`meta` 里一个 source 天然会有
    多个块，所以按块判重复是错的 —— 要按**不同 source** 判）；
    以及 `索引 source 集合 == 磁盘文件集合 − skipped 集合`。
    """
    problems: list[str] = []
    srcs = [m["source"] for m in meta]
    distinct = set(srcs)
    if len(distinct) != file_count:
        problems.append(
            f"不同 source 个数 {len(distinct)} != 成功处理的文件数 {file_count} "
            "（有文件被记成了同一个 key，或同一文件被记了多个 key）")
    disk_srcs = [s for s, _p, _m in disk]
    # **磁盘侧就要查重**（审计 F-14）：如果两个不同的文件映射成同一个 source，
    # 那么"其中一个处理失败"时，上面那条"不同 source 数 == 成功文件数"会**同时减一**、
    # 等式照旧成立 → 静默少收一篇语料。直接在磁盘侧查重才抓得住根因。
    _dups = sorted(s for s, n in Counter(disk_srcs).items() if n > 1)
    if _dups:
        problems.append(
            f"有 {len(_dups)} 个 source 对应多个磁盘文件（会互相覆盖、必丢一篇）: "
            f"{_dups[:3]}")
    indexed = distinct
    skip = {s["path"] for s in skipped}
    missing = set(disk_srcs) - indexed - skip
    if missing:
        problems.append(f"有 {len(missing)} 个文件既没进索引也不在 skipped 里（真漏）: "
                        f"{sorted(missing)[:3]}")
    orphan = indexed - set(disk_srcs) - skip
    if orphan:
        problems.append(f"索引里有 {len(orphan)} 个 source 磁盘上已不存在（旧文件残留）: "
                        f"{sorted(orphan)[:3]}")
    return problems


def index_freshness(index_dir: Optional[Path] = None) -> dict:
    """索引新鲜度自检（计划书 §11.2 R1+R2，**不新建 stamp 文件** §12.3）。

    比对「磁盘上各文件的 (相对路径, mtime)」与「`meta.jsonl` 里各 source 的最大 mtime」：

    - `missing_from_index`：磁盘有、索引没有 → 新文件还没进索引（比如别人刚写的报告）
    - `changed`：两边都有但磁盘更新 → 内容变了没重索引
    - `orphan_in_index`：索引有、磁盘没有 → 已删文件还占着检索名额（**这条最该管**）
    - `source_format_ok`：口径版本是否匹配（不匹配时 `update_file` 会拒绝写入）

    **为什么必须有它**（外部最佳实践 R1/R2）：检索库最容易出的不是"搜不准"，
    而是"搜到的是上一版内容"或"删掉的文件还在被引用" —— 这两种都不会报错。
    """
    vectors, meta = _load_index(index_dir)
    fmt = _source_format_of(index_dir)
    has_index = len(meta) > 0
    disk = _disk_files()
    disk_map = {s: m for s, _p, m in disk}
    idx_mtime: dict[str, float] = {}
    for m in meta:
        s = m.get("source")
        if s:
            idx_mtime[s] = max(idx_mtime.get(s, 0.0), float(m.get("mtime") or 0.0))
    missing = sorted(set(disk_map) - set(idx_mtime))
    orphan = sorted(set(idx_mtime) - set(disk_map))
    # mtime 比较留 1 秒容差: 文件系统的秒级精度会让"刚写完就索引"误报
    changed = sorted(s for s in set(disk_map) & set(idx_mtime)
                     if disk_map[s] > idx_mtime[s] + 1.0)
    reasons = []
    if not has_index:
        reasons.append("还没有索引")
    if missing:
        reasons.append(f"{len(missing)} 个文件没进索引")
    if changed:
        reasons.append(f"{len(changed)} 个文件更新了没重索引")
    if orphan:
        reasons.append(f"{len(orphan)} 个已删文件还留在索引里")
    if fmt != SOURCE_FORMAT:
        reasons.append(f"source 口径版本 {fmt} != {SOURCE_FORMAT}")
    return {"has_index": has_index, "indexed_sources": len(idx_mtime),
            "indexed_chunks": len(meta), "disk_files": len(disk_map),
            "source_format": fmt, "stale": bool(reasons), "reasons": reasons,
            "missing_from_index": missing, "changed": changed,
            "orphan_in_index": orphan,
            "roots": list(_index_config()["roots"])}


def _vectors_path(index_dir: Optional[Path] = None) -> Path:
    return (index_dir or INDEX_DIR) / "vectors.npy"


def _meta_path(index_dir: Optional[Path] = None) -> Path:
    return (index_dir or INDEX_DIR) / "meta.jsonl"


def _load_index(index_dir: Optional[Path] = None) -> tuple[np.ndarray, list[dict]]:
    """加载索引。不存在返 (空数组, 空列表)。

    对齐校验：vectors 行数 != meta 行数说明索引处于撕裂/损坏态
    （原子写前的历史遗留或异常中断），降级返空而不是张冠李戴——
    用错误的 idx 取 meta 会把 A 文件的分数配到 B 文件的片段上。
    """
    vp = _vectors_path(index_dir)
    mp = _meta_path(index_dir)
    if not vp.exists() or not mp.exists():
        return np.empty((0, EMBED_DIM), dtype=np.float32), []
    vectors = np.load(vp)
    meta = []
    with open(mp, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                meta.append(json.loads(line))
    if vectors.shape[0] != len(meta):
        logger.warning(
            f"索引撕裂: vectors {vectors.shape[0]} 行 != meta {len(meta)} 行，"
            f"降级返空。请运行 python -m brain.search_engine build --force 重建")
        return np.empty((0, EMBED_DIM), dtype=np.float32), []
    return vectors, meta


def _save_index(index_dir: Optional[Path], vectors: np.ndarray, meta: list[dict]) -> None:
    """落盘 vectors.npy + meta.jsonl（整体重写，原子写）。

    两文件都先写 tmp 再 os.replace，meta 最后落盘——读者任意时刻要么看到
    完整的旧索引、要么看到完整的新索引，不会读到撕裂态（大脑每次调用都是
    独立 subprocess，进程内 threading.Lock 管不到跨进程并发）。
    """
    base = index_dir or INDEX_DIR
    base.mkdir(parents=True, exist_ok=True)
    vp, mp = _vectors_path(base), _meta_path(base)
    vtmp = vp.with_suffix(".npy.tmp")
    with open(vtmp, "wb") as f:  # np.save 会给非 .npy 结尾的路径自动加后缀，必须用文件句柄
        np.save(f, vectors)
    os.replace(vtmp, vp)
    mtmp = mp.with_suffix(".jsonl.tmp")
    with open(mtmp, "w", encoding="utf-8") as f:
        for m in meta:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    os.replace(mtmp, mp)


# ── 文件前缀提取（丰富公司文件的 chunks 上下文）─────────────────
def _extract_file_prefix(text: str, filepath: Path) -> str:
    """从 company markdown frontmatter 提取 name + 行业，生成分块前缀。非公司文件返 ''。"""
    if "/company/" not in filepath.as_posix() and "\\company\\" not in str(filepath):
        return ""
    lines = text.split("\n")
    name = ""
    code = ""
    industries: list[str] = []
    for line in lines[:50]:
        line = line.strip()
        if line.startswith("name:"):
            name = line.split("name:", 1)[1].strip().strip('"').strip()
        elif line.startswith("code:"):
            code = line.split("code:", 1)[1].strip().strip('"').strip()
        elif "industry_" in line and ("belongs_to" in line or "industry_tdx" in line):
            # 提取行业名：[[industry_630705|蓄电池及其他电池]]
            m = re.search(r"\|([^\]|]+)\]\]", line)
            if m:
                industries.append(m.group(1))
        if name and code and industries:
            break
    if not name:
        return ""
    parts = [f"公司：{name}"]
    if code:
        parts.append(f"股票代码：{code}")
    if industries:
        parts.append(f"所属行业：{'、'.join(industries[:3])}")
    return "。".join(parts) + "。\n\n"


# ── 文件 → 块/元数据（build_index 与 update_file 共用） ─────
def _meta_prefix(source) -> str:
    """给**所有**语料加一小段"这是什么文件"的上下文前缀（计划书 §11.3 R3）。

    原来只有公司档案加前缀（`_extract_file_prefix`），而 `research/` 与 `docs/` 两个
    新语料根进来之后，"这是一份研究报告 / 计划书 / 方法论"这种语境对检索很关键 ——
    用户问"我们以前研究过什么"时，命中块里带上文件类型，模型更容易判断该不该引用。

    前缀只放**路径里已经有的信息**（文件名 + 顶层目录），不引入任何新事实。
    入参可以是 Path 也可以是 str（`_read_chunks` 传的是 Path）。
    """
    try:
        rel = _to_source(source)
    except Exception:
        return ""                   # 口径外/算不出来 → 不给前缀, 不编
    top = rel.split("/")[0]
    kind = {"research": "研究报告库", "vera_obs_vault": "公司档案库",
            "docs": "项目文档库"}.get(top, "")
    name = Path(rel).stem
    if not kind and not name:
        return ""
    return f"[来自{kind or '资料库'}：{name}]\n"


def _read_chunks(filepath: Path) -> list[str]:
    """读文件 → 公司前缀增强 → 剥 frontmatter → 分块。读失败告警并返 []。"""
    try:
        raw_text = filepath.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"读取失败（跳过）: {filepath} — {e}")
        return []
    # 公司文件：提取 frontmatter 中的 name/行业作前缀丰富上下文；
    # 随后剥掉 frontmatter，避免 YAML 元数据进入 chunk/snippet
    prefix = _extract_file_prefix(raw_text, filepath)
    body = _strip_frontmatter(raw_text)
    if not prefix:      # 非公司文件：加"来自哪个库/哪份文件"的通用前缀 (§11.3)
        prefix = _meta_prefix(filepath)
    return _chunk((prefix + body) if prefix else body)


def _meta_entries(source: str, chunks: list[str], mtime: float) -> list[dict]:
    """chunks → meta 行（id = {source}#{chunk_idx}）。"""
    return [{"id": f"{source}#{i}", "source": source, "chunk_idx": i,
             "mtime": mtime, "snippet": c[:200]} for i, c in enumerate(chunks)]


def _remove_source(index_dir: Optional[Path], source: str) -> None:
    """从索引移除某 source 的全部块（文件删除/清空时调用）。"""
    vectors, meta = _load_index(index_dir)
    keep_idx = [j for j, m in enumerate(meta) if m["source"] != source]
    if len(keep_idx) < len(meta):
        _save_index(index_dir, vectors[keep_idx], [meta[j] for j in keep_idx])


# ── VaultSearchEngine ─────────────────────────────────────
class VaultSearchEngine:
    """Vault 语义搜索引擎，所有公开方法线程安全且失败不抛。

    index_dir: 实例级索引目录（测试隔离用）；不传则用模块常量 INDEX_DIR。
    """

    def __init__(self, index_dir: Optional[Path] = None):
        self._dir = index_dir

    def build_index(self, force_rebuild: bool = False) -> dict:
        """全量构建索引。

        返 `{"files", "chunks", "skipped", "skipped_detail", "problems"}`。

        **失败语义（计划书 §12.1 的四行处置表，逐条实现）**：

        | 情况 | 处置 |
        |---|---|
        | 文件没进索引，但在 `skipped` 里（读失败/嵌入失败/块数 0） | **不算失败**，记进 `skipped_detail` |
        | 文件没进索引，也不在 `skipped` 里 | **失败**（这是真漏） |
        | 索引里有、磁盘上没有（旧文件残留） | **失败**（真病：已删文件还占检索名额） |
        | 磁盘清单在构建期间变动（别的会话在写） | **告警 + 保留上一份索引**，绝不清空 |

        为什么不能"任一文件失败就整次失败"：那是把刻意的逐文件软降级改成硬失败，
        一个文件读不到就让整个索引停在旧状态，比现在更糟（计划书 HIGH-2）。
        """
        with _index_lock:
            meta_path = _meta_path(self._dir)
            if not force_rebuild and meta_path.exists() and meta_path.stat().st_size > 0:
                return {"files": 0, "chunks": 0, "skipped": True,
                        "skipped_detail": [], "problems": [], "disk_changed": []}

            disk = _disk_files()                 # [(source, path, mtime), ...]
            all_meta: list[dict] = []
            all_vecs: list[np.ndarray] = []
            skipped_detail: list[dict] = []
            file_count = 0

            for source, md_file, mtime in disk:
                chunks = _read_chunks(md_file)
                if not chunks:
                    skipped_detail.append({"path": source, "reason": "读不到内容或块数为 0"})
                    continue
                try:
                    vecs = _embed_with_retry(chunks)
                except Exception as e:
                    skipped_detail.append({"path": source, "reason": f"嵌入失败: {e}"})
                    continue
                all_meta.extend(_meta_entries(source, chunks, mtime))
                all_vecs.extend(vecs)
                file_count += 1

            # 防线③ 构建后自检（§3.2 + §12.1 修订版）:
            #   不同 source 个数 == 成功处理的文件数, 且索引 == 磁盘 − skipped
            problems = _check_build_consistency(disk, all_meta, skipped_detail, file_count)
            if problems:
                # **绝不落盘**: 宁可用旧索引, 也不写一份已知有病的索引
                logger.error("build_index 自检未通过, 拒绝落盘: %s", problems[:3])
                return {"files": 0, "chunks": 0, "skipped": False,
                        "skipped_detail": skipped_detail, "problems": problems}

            if not all_meta:
                _save_index(self._dir, np.empty((0, EMBED_DIM), dtype=np.float32), [])
                _write_source_format(self._dir)
                return {"files": 0, "chunks": 0, "skipped": False,
                        "skipped_detail": skipped_detail, "problems": [],
                        "disk_changed": []}

            vectors = np.stack(all_vecs, axis=0)
            _save_index(self._dir, vectors, all_meta)
            _write_source_format(self._dir)      # 只在真构建时写版本号 (§12.3)
            # §3.3「构建期间磁盘变动」检查 —— **原来只写在 docstring 里没实现**
            # （2026-09-17 M7 第二轮审计 F5）。这里真的比对一次: 本仓库同时有别的会话在写,
            # 收进"写到一半的文件"是有可能的。发现变动就**告警 + 在结果里标出来**
            # （索引本身是原子写, 不会读坏; 只是提醒"这一份可能少了刚写的文件"）。
            disk_after = {src for src, _p, _m in _disk_files()}
            changed = sorted(disk_after ^ {src for src, _p, _m in disk})
            if changed:
                logger.warning("build_index: 构建期间磁盘清单变了 %d 项（别的会话在写?）: %s",
                               len(changed), changed[:3])
            logger.info(f"build_index 完成: {file_count} 文件, {len(all_meta)} 块, "
                        f"skip {len(skipped_detail)}")
            return {"files": file_count, "chunks": len(all_meta), "skipped": False,
                    "skipped_detail": skipped_detail, "problems": [],
                    "disk_changed": changed}

    def update_file(self, filepath: Path) -> int:
        """单文件增量更新（archive.py 用）。按 source 去重：删旧块→写新块。返新块数。

        **入口先做版本闸门（§12.2 HIGH-5）**：索引的 `source` 口径与本代码的
        `SOURCE_FORMAT` 不一致时**拒绝写入**（fail-closed），因为按 source 精确匹配
        会匹配不到旧 key → 同一文件两个 key 并存。提示先跑 `build --force`。
        **对外仍然"失败不抛"**：异常一律降级成日志 + 返 0，绝不打断归档链路。
        """
        with _index_lock:
            try:
                source = _to_source(filepath)
            except ValueError as e:
                logger.warning(f"update_file 拒绝写入（source 口径外）: {filepath} — {e}")
                return 0
            fmt = _source_format_of(self._dir)
            if fmt != SOURCE_FORMAT:
                logger.warning(
                    "update_file 拒绝写入：索引 source 口径版本 %s != 当前 %s。"
                    "先跑 `python -m brain.search_engine build --force` 重建，"
                    "否则会产生同一文件两个 key（本次跳过: %s）",
                    fmt, SOURCE_FORMAT, source)
                return 0
            if not filepath.exists():
                _remove_source(self._dir, source)
                return 0

            chunks = _read_chunks(filepath)
            if not chunks:
                # 文件变空 → 清理旧块
                _remove_source(self._dir, source)
                return 0

            try:
                vecs = _embed_with_retry(chunks)
            except Exception as e:
                logger.warning(f"update_file 嵌入失败: {filepath} — {e}")
                return 0

            new_chunks = _meta_entries(source, chunks, filepath.stat().st_mtime)

            vectors, meta = _load_index(self._dir)
            keep_idx = [j for j, m in enumerate(meta) if m["source"] != source]
            if keep_idx:
                vectors = np.concatenate([vectors[keep_idx], vecs], axis=0)
                meta = [meta[j] for j in keep_idx] + new_chunks
            else:
                vectors = vecs
                meta = new_chunks
            _save_index(self._dir, vectors, meta)
            return len(new_chunks)

    def search(self, query: str, top_k: int = 8) -> list[dict]:
        """语义搜索。返 [{"path": str, "score": float, "snippet": str}]，按 score 降序。"""
        try:
            vectors, meta = _load_index(self._dir)
            if len(meta) == 0:
                return []
            q_vec = _embed([query], is_query=True)
            scores = (q_vec @ vectors.T).flatten()
            if len(scores) == 0:
                return []
            top_idx = np.argsort(scores)[::-1][:top_k]
            results: list[dict] = []
            for idx in top_idx:
                if scores[idx] <= 0:
                    continue
                results.append({
                    "path": meta[idx]["source"],
                    "score": float(scores[idx]),
                    "snippet": meta[idx].get("snippet", ""),
                })
            return results
        except Exception as e:
            logger.warning(f"search 异常（返 []）: {e}")
            return []

    def eval_search(self, evalset: Optional[Path] = None) -> dict:
        """跑检索质量评估。默认用 brain/search_evalset.json。

        返 `{"total","hits","hit_rate","misses","mrr","ranked_low","by_cat"}`。

        **为什么必须加 MRR（计划书 §11.4）**：只看 top-5 命中率会掩盖一件事 ——
        正确答案可能从第 1 名掉到第 5 名，命中率一点没变，但大脑读到的第一条
        已经是错的了。MRR（平均倒数排名）= 正确答案排在第几名的倒数的平均，
        **第 1 名得 1.0、第 2 名 0.5、第 3 名 0.33**，排名掉了它就掉。
        `ranked_low` 列出"命中了但排在第 3 名之后"的用例，是真正要盯的那批。
        """
        if evalset is None:
            evalset = Path(__file__).resolve().parent / "search_evalset.json"
        try:
            cases = json.loads(evalset.read_text(encoding="utf-8"))
        except Exception as e:
            return {"error": f"读取 evalset 失败: {e}", "total": 0, "hits": 0,
                    "hit_rate": 0.0, "misses": [], "mrr": 0.0, "ranked_low": [],
                    "by_cat": {}}
        hits = 0
        rr_sum = 0.0
        misses: list[str] = []
        ranked_low: list[dict] = []
        by_cat: dict[str, dict] = {}
        for c in cases:
            cat = str(c.get("cat") or "未分类")
            st = by_cat.setdefault(cat, {"total": 0, "hits": 0})
            st["total"] += 1
            results = self.search(c["q"], top_k=5)
            paths = [r["path"] for r in results]
            want = c.get("expect_path")
            if want in paths:
                hits += 1
                st["hits"] += 1
                rank = paths.index(want) + 1
                rr_sum += 1.0 / rank
                if rank > 2:
                    ranked_low.append({"id": c["id"], "rank": rank, "q": c["q"]})
            else:
                misses.append(c["id"])
                rr_sum += 0.0
        n = len(cases) or 1
        for st in by_cat.values():
            st["hit_rate"] = round(st["hits"] / st["total"], 4) if st["total"] else 0.0
        return {
            "total": len(cases),
            "hits": hits,
            "hit_rate": hits / n,
            "mrr": round(rr_sum / n, 4),
            "misses": misses,
            "ranked_low": ranked_low,
            "by_cat": by_cat,
        }


# ── CLI ────────────────────────────────────────────────────
def _cmd_build(args):
    engine = VaultSearchEngine()
    result = engine.build_index(force_rebuild=args.force)
    ensure_utf8_stdout()
    print(json.dumps(result, ensure_ascii=False))
    # **审计 F7**: 自检没过时 build_index 拒绝落盘(对的), 但 CLI 原来照样 return 0
    # → 脚本里 `&&` 后面那些步骤会**在索引没更新的情况下继续跑**, 把"拒绝落盘"静默化。
    # 这里显式非零退出, 让调用方能看见。
    if result.get("problems"):
        print("【错】索引自检未通过, 已拒绝落盘（详见上面的 problems）", file=sys.stderr)
        sys.exit(4)


def _cmd_search(args):
    engine = VaultSearchEngine()
    vectors_path = _vectors_path()
    meta_path = _meta_path()
    if not vectors_path.exists() or not meta_path.exists() or meta_path.stat().st_size == 0:
        ensure_utf8_stdout()
        print("（索引不存在，请先运行: python -m brain.search_engine build）")
        sys.exit(2)
    results = engine.search(args.query, top_k=args.top_k)
    ensure_utf8_stdout()
    if not results:
        print("（无匹配结果）")
        return
    for i, r in enumerate(results, 1):
        snippet = r["snippet"][:200] if r["snippet"] else ""
        print(f"[{i}] score={r['score']:.3f}  {r['path']}")
        print(f"    {snippet}")


def _cmd_update(args):
    engine = VaultSearchEngine()
    fp = Path(args.filepath)
    if not fp.is_absolute():
        fp = VERA_ROOT / fp
    n = engine.update_file(fp)
    ensure_utf8_stdout()
    print(json.dumps({"updated": fp.as_posix(), "chunks": n}, ensure_ascii=False))


def _cmd_fresh(args):
    """打印索引新鲜度（计划书 §11.2 R1+R2）。--json 供大脑/脚本消费。"""
    r = index_freshness()
    ensure_utf8_stdout()
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return
    if not r.get("has_index"):
        print("（没有索引，先跑 python -m brain.search_engine build --force）")
        return
    print(f"索引: {r['indexed_sources']} 个 source / 磁盘: {r['disk_files']} 个文件"
          f"  口径版本: {r['source_format']}（当前 {SOURCE_FORMAT}）")
    if r["stale"]:
        print(f"⚠ 索引不新鲜（{'、'.join(r['reasons'])}）→ "
              "跑 python -m brain.search_engine build --force")
        for s in r["missing_from_index"][:5]:
            print(f"    磁盘有但索引没有: {s}")
        for s in r["changed"][:5]:
            print(f"    索引比磁盘旧: {s}")
        for s in r["orphan_in_index"][:5]:
            print(f"    索引有但磁盘没有: {s}")
    else:
        print("✅ 索引与磁盘一致（没有新增/更新/删除）")


def _cmd_eval(args):
    engine = VaultSearchEngine()
    evalset = Path(args.evalset) if args.evalset else None
    result = engine.eval_search(evalset)
    ensure_utf8_stdout()
    if "error" in result:
        print(f"（评估失败: {result['error']}）")
        sys.exit(3)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["misses"]:
        print(f"\n未命中: {', '.join(result['misses'])}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m brain.search_engine")
    sub = ap.add_subparsers(dest="cmd")

    p_build = sub.add_parser("build", help="全量构建索引")
    p_build.add_argument("--force", action="store_true")

    p_search = sub.add_parser("search", help="语义搜索")
    p_search.add_argument("query", help="自然语言查询")
    p_search.add_argument("--top-k", type=int, default=8)

    p_update = sub.add_parser("update", help="增量更新单文件")
    p_update.add_argument("filepath", help="文件路径")

    p_eval = sub.add_parser("eval", help="检索质量评估")
    p_eval.add_argument("--evalset", default=None, help="评估集路径（默认 brain/search_evalset.json）")

    p_fresh = sub.add_parser("fresh", help="索引新鲜度自检（磁盘 vs 索引）")
    p_fresh.add_argument("--json", action="store_true", help="输出 JSON（供大脑消费）")

    args = ap.parse_args(argv)
    if args.cmd == "build":
        _cmd_build(args)
    elif args.cmd == "search":
        _cmd_search(args)
    elif args.cmd == "update":
        _cmd_update(args)
    elif args.cmd == "eval":
        _cmd_eval(args)
    elif args.cmd == "fresh":
        _cmd_fresh(args)
    else:
        ap.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
