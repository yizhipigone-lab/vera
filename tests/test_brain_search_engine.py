"""tests/test_brain_search_engine.py — brain/search_engine.py 单元测试。

用 monkeypatch 替换 _embed 为确定性假嵌入，不依赖真实模型，CI 可跑。
"""
from __future__ import annotations

import json

import numpy as np
import pytest

import brain.search_engine as se


# ── 假嵌入工厂 ──────────────────────────────────────────────
def _fake_embed_factory(expected_dim: int = 512):
    """返回一个(文本→确定性假向量)的 callable。"""

    def _embed(texts: list[str], is_query: bool = False) -> np.ndarray:
        vecs = np.zeros((len(texts), expected_dim), dtype=np.float32)
        for i, t in enumerate(texts):
            # 用字符串的 CRC 替代 hash()（跨进程稳定）
            import zlib
            h = zlib.crc32(t.encode("utf-8"))
            rng = np.random.RandomState(abs(h))
            v = rng.randn(expected_dim).astype(np.float32)
            v = v / (np.linalg.norm(v) + 1e-10)
            vecs[i] = v
        return vecs

    return _embed


# 构造足够长的测试文本（每段 ≥50 字才能通过分块） —— 用于多段落场景
def _make_text(paragraphs: list[str]) -> str:
    """用段落列表构造文本，每段至少重复到 55 字。"""
    parts = []
    for p in paragraphs:
        while len(p) < 55:
            p = p + "。" + p
        parts.append(p[:450])
    return "\n\n".join(parts)


# ── conftest 级 fixture：隔离 data/brain_vectors/ ───────────
@pytest.fixture(autouse=True)
def _isolate_brain_vectors(monkeypatch, tmp_path):
    """所有测试使用临时目录，不污染真实索引。"""
    monkeypatch.setattr(se, "INDEX_DIR", tmp_path / "brain_vectors")
    monkeypatch.setattr(se, "VAULT_ROOT", tmp_path / "vault")


# ── 测试 ─────────────────────────────────────────────────────
class TestChunk:
    def test_合并与上限(self):
        """构造长短混合文本，断言所有块 ≤450 字。"""
        # 短段会合并
        short = "短段A。" * 20  # ~80字
        long_text = "长段" + "X" * 500 + "。" * 20  # >450字
        text = f"{short}\n\n{long_text}"
        chunks = se._chunk(text)
        for c in chunks:
            assert len(c) <= se.CHUNK_MAX + 20, f"块超上限太多 {len(c)}"
        # 至少有一个块
        assert len(chunks) >= 1, f"应有至少一个块，实有 {len(chunks)}"

    def test_空文本(self):
        assert se._chunk("") == []
        assert se._chunk("\n\n\n") == []

    def test_正常段落(self):
        """足够长的正常段落能产出块。"""
        text = _make_text(["这是第一段内容", "这是第二段内容"])
        chunks = se._chunk(text)
        assert len(chunks) >= 1, f"正常段落应有块，实有 {len(chunks)}"


class TestUpdateFile:
    def test_去重(self, monkeypatch, tmp_path):
        """同一文件 update 两次，断言 meta 中该 source 行数 == 第二次的块数。"""
        monkeypatch.setattr(se, "_embed", _fake_embed_factory(512))
        vault = tmp_path / "vault" / "company"
        vault.mkdir(parents=True)
        f = vault / "test.md"
        f.write_text(_make_text(["段落一内容", "段落二内容", "段落三内容"]), encoding="utf-8")

        engine = se.VaultSearchEngine()
        n1 = engine.update_file(f)
        assert n1 > 0
        _, meta = se._load_index()
        # source 存的是相对 VAULT_ROOT 的路径
        rel = str(f.relative_to(tmp_path / "vault").as_posix())
        count1 = sum(1 for m in meta if m["source"] == rel)
        assert count1 == n1

        # 不改文件内容，再次 update
        n2 = engine.update_file(f)
        assert n2 == n1
        _, meta = se._load_index()
        count2 = sum(1 for m in meta if m["source"] == rel)
        assert count2 == n2, f"去重失败: 第二次应有 {n2} 块，实有 {count2}"

    def test_删除(self, monkeypatch, tmp_path):
        """文件删除后 update，断言该 source 块数清零。"""
        monkeypatch.setattr(se, "_embed", _fake_embed_factory(512))
        vault = tmp_path / "vault" / "company"
        vault.mkdir(parents=True)
        f = vault / "temp.md"
        f.write_text(_make_text(["测试第一部分", "测试第二部分"]), encoding="utf-8")

        engine = se.VaultSearchEngine()
        engine.update_file(f)
        _, meta = se._load_index()
        rel = str(f.relative_to(tmp_path / "vault").as_posix())
        assert sum(1 for m in meta if m["source"] == rel) > 0

        # 删文件
        f.unlink()
        engine.update_file(f)
        _, meta = se._load_index()
        assert sum(1 for m in meta if m["source"] == f.as_posix()) == 0


class TestSearch:
    def test_排序与字段(self, monkeypatch, tmp_path):
        """假嵌入下构造已知相似度，断言 top-k 顺序与字段齐全。"""
        monkeypatch.setattr(se, "_embed", _fake_embed_factory(512))
        vault = tmp_path / "vault" / "company"
        vault.mkdir(parents=True)
        f1 = vault / "a.md"
        f1.write_text(_make_text(["内容段落A", "内容段落B"]), encoding="utf-8")

        engine = se.VaultSearchEngine()
        engine.build_index(force_rebuild=True)
        results = engine.search("测试查询", top_k=3)
        assert len(results) > 0, "应有至少一条结果"
        for r in results:
            assert "path" in r, f"缺 path: {r}"
            assert "score" in r, f"缺 score: {r}"
            assert "snippet" in r, f"缺 snippet: {r}"
        # 验证降序
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True), f"非降序: {scores}"

    def test_索引缺失返回空(self):
        """索引不存在时 search 返 [] 不抛异常。"""
        engine = se.VaultSearchEngine()
        results = engine.search("任意查询")
        assert results == []


class TestBuildIndex:
    def test_build_and_skip(self, monkeypatch, tmp_path):
        """首次 build 不 skip，二次 build 应 skip。"""
        monkeypatch.setattr(se, "_embed", _fake_embed_factory(512))
        vault = tmp_path / "vault" / "company"
        vault.mkdir(parents=True)
        (vault / "x.md").write_text(_make_text(["测试内容段落"]), encoding="utf-8")

        engine = se.VaultSearchEngine()
        r1 = engine.build_index(force_rebuild=True)
        assert r1["skipped"] is False
        assert r1["files"] >= 1

        r2 = engine.build_index(force_rebuild=False)
        assert r2["skipped"] is True

    def test_force(self, monkeypatch, tmp_path):
        """force_rebuild=True 跳过缓存检查。"""
        monkeypatch.setattr(se, "_embed", _fake_embed_factory(512))
        vault = tmp_path / "vault" / "company"
        vault.mkdir(parents=True)
        (vault / "y.md").write_text(_make_text(["内容段落"]), encoding="utf-8")

        engine = se.VaultSearchEngine()
        engine.build_index(force_rebuild=False)
        r = engine.build_index(force_rebuild=True)
        assert r["skipped"] is False


class TestIndexIntegrity:
    """2026-08-01 审计修复的回归：frontmatter 噪音 / 撕裂索引 / 原子写。"""

    def test_frontmatter剥离(self):
        fm = '---\nnode_id: "company:000001"\nname: "平安银行"\n---\n\n正文内容从这里开始。'
        assert se._strip_frontmatter(fm) == "正文内容从这里开始。"
        # 非 frontmatter 开头原样返回
        assert se._strip_frontmatter("直接正文") == "直接正文"
        # 只有起始标记没有结束标记，原样返回
        assert se._strip_frontmatter("---\n没有结束") == "---\n没有结束"

    def test_撕裂索引降级返空(self, monkeypatch, tmp_path):
        """vectors 行数 != meta 行数 → _load_index 返空，search 返 []（不张冠李戴）。"""
        monkeypatch.setattr(se, "_embed", _fake_embed_factory(512))
        vault = tmp_path / "vault" / "company"
        vault.mkdir(parents=True)
        (vault / "z.md").write_text(_make_text(["完整性测试段落"]), encoding="utf-8")

        engine = se.VaultSearchEngine()
        engine.build_index(force_rebuild=True)
        _, meta_ok = se._load_index()
        assert len(meta_ok) > 0, "正常索引应能加载"

        # 人为制造撕裂：meta 追加一行，vectors 不变
        mp = se._meta_path()
        with open(mp, "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": "x#0", "source": "x", "chunk_idx": 0,
                                "mtime": 0, "snippet": "s"}) + "\n")
        vectors, meta = se._load_index()
        assert len(meta) == 0 and vectors.shape[0] == 0, "撕裂索引应降级返空"
        assert engine.search("完整性测试") == []

    def test_原子写不留tmp(self, monkeypatch, tmp_path):
        """update_file 后索引目录无 .tmp 残留（原子写经 os.replace 落盘）。"""
        monkeypatch.setattr(se, "_embed", _fake_embed_factory(512))
        vault = tmp_path / "vault" / "company"
        vault.mkdir(parents=True)
        f = vault / "w.md"
        f.write_text(_make_text(["原子写测试段落"]), encoding="utf-8")

        engine = se.VaultSearchEngine()
        engine.update_file(f)
        leftovers = list(se.INDEX_DIR.glob("*.tmp"))
        assert leftovers == [], f"原子写应无 tmp 残留: {leftovers}"
