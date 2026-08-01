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
        """全量构建索引。返 {"files": int, "chunks": int, "skipped": bool}。"""
        with _index_lock:
            meta_path = _meta_path(self._dir)
            if not force_rebuild and meta_path.exists() and meta_path.stat().st_size > 0:
                return {"files": 0, "chunks": 0, "skipped": True}

            all_meta: list[dict] = []
            all_vecs: list[np.ndarray] = []
            file_count = 0

            for dir_name in VAULT_DIRS:
                d = VAULT_ROOT / dir_name
                if not d.is_dir():
                    continue
                for md_file in sorted(d.glob("*.md")):
                    chunks = _read_chunks(md_file)
                    if not chunks:
                        continue
                    try:
                        vecs = _embed(chunks, is_query=False)
                    except Exception as e:
                        logger.warning(f"嵌入失败（跳过）: {md_file} — {e}")
                        continue
                    source = str(md_file.relative_to(VAULT_ROOT).as_posix())
                    all_meta.extend(_meta_entries(source, chunks, md_file.stat().st_mtime))
                    all_vecs.extend(vecs)
                    file_count += 1

            if not all_meta:
                _save_index(self._dir, np.empty((0, EMBED_DIM), dtype=np.float32), [])
                return {"files": 0, "chunks": 0, "skipped": False}

            vectors = np.stack(all_vecs, axis=0)
            _save_index(self._dir, vectors, all_meta)
            logger.info(f"build_index 完成: {file_count} 文件, {len(all_meta)} 块")
            return {"files": file_count, "chunks": len(all_meta), "skipped": False}

    def update_file(self, filepath: Path) -> int:
        """单文件增量更新（archive.py 用）。按 source 去重：删旧块→写新块。返新块数。"""
        with _index_lock:
            source = str(filepath.relative_to(VAULT_ROOT).as_posix()) if filepath.is_absolute() and VAULT_ROOT in filepath.parents else filepath.as_posix()
            if not filepath.exists():
                _remove_source(self._dir, source)
                return 0

            chunks = _read_chunks(filepath)
            if not chunks:
                # 文件变空 → 清理旧块
                _remove_source(self._dir, source)
                return 0

            try:
                vecs = _embed(chunks, is_query=False)
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
        """跑检索质量评估。默认用 brain/search_evalset.json。"""
        if evalset is None:
            evalset = Path(__file__).resolve().parent / "search_evalset.json"
        try:
            cases = json.loads(evalset.read_text(encoding="utf-8"))
        except Exception as e:
            return {"error": f"读取 evalset 失败: {e}", "total": 0, "hits": 0, "hit_rate": 0.0, "misses": []}
        hits = 0
        misses: list[str] = []
        for c in cases:
            results = self.search(c["q"], top_k=5)
            paths = [r["path"] for r in results]
            if c.get("expect_path") in paths:
                hits += 1
            else:
                misses.append(c["id"])
        return {
            "total": len(cases),
            "hits": hits,
            "hit_rate": hits / len(cases) if cases else 0.0,
            "misses": misses,
        }


# ── CLI ────────────────────────────────────────────────────
def _cmd_build(args):
    engine = VaultSearchEngine()
    result = engine.build_index(force_rebuild=args.force)
    ensure_utf8_stdout()
    print(json.dumps(result, ensure_ascii=False))


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

    args = ap.parse_args(argv)
    if args.cmd == "build":
        _cmd_build(args)
    elif args.cmd == "search":
        _cmd_search(args)
    elif args.cmd == "update":
        _cmd_update(args)
    elif args.cmd == "eval":
        _cmd_eval(args)
    else:
        ap.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
