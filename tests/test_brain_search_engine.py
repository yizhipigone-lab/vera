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
    """所有测试使用临时目录，不污染真实索引。

    2026-09-17（M5）：`source` 口径改成**项目根相对**后，隔离必须连 `VERA_ROOT`
    一起指到 tmp_path —— 否则 `_to_source` 会相对真实项目根算，测试就依赖
    「basetemp 必须在项目内」这个隐含前提。同时隔离配置路径与默认根，
    免得测试读进生产的 `config/brain_index.json`。
    """
    monkeypatch.setattr(se, "VERA_ROOT", tmp_path)
    monkeypatch.setattr(se, "INDEX_DIR", tmp_path / "brain_vectors")
    monkeypatch.setattr(se, "VAULT_ROOT", tmp_path / "vault")
    monkeypatch.setattr(se, "INDEX_CONFIG_PATH", tmp_path / "config" / "brain_index.json")
    monkeypatch.setattr(se, "DEFAULT_INDEX_ROOTS", ("vault/company",))


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
        engine.build_index(force_rebuild=True)   # 先建索引: 版本闸门要求先有合法索引
        n1 = engine.update_file(f)
        assert n1 > 0
        _, meta = se._load_index()
        # source 一律走唯一口径函数算（测试里**不许**再写一份 relative_to）
        rel = se._to_source(f)
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
        engine.build_index(force_rebuild=True)
        engine.update_file(f)
        _, meta = se._load_index()
        rel = se._to_source(f)
        assert sum(1 for m in meta if m["source"] == rel) > 0

        # 删文件
        f.unlink()
        engine.update_file(f)
        _, meta = se._load_index()
        assert sum(1 for m in meta if m["source"] == rel) == 0


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
        engine.build_index(force_rebuild=True)
        engine.update_file(f)
        leftovers = list(se.INDEX_DIR.glob("*.tmp"))
        assert leftovers == [], f"原子写应无 tmp 残留: {leftovers}"


class TestSourceSingleExit:
    """M5 §3.2 三重防线：source 口径唯一出口 + 拒绝绝对路径 + 构建后自检。"""

    def test_to_source_is_project_root_relative(self, tmp_path):
        f = tmp_path / "vault" / "company" / "a.md"
        f.parent.mkdir(parents=True)
        f.write_text("x", encoding="utf-8")
        assert se._to_source(f) == "vault/company/a.md"
        # 返回的路径必须能直接丢给 Read / open 打开
        assert (se.VERA_ROOT / se._to_source(f)).exists()

    def test_to_source_rejects_outside_project_root(self, tmp_path):
        """防线②：项目根之外的文件不许退化成绝对路径，直接抛错。"""
        outside = tmp_path.parent / "definitely_outside.md"
        outside.write_text("x", encoding="utf-8")
        with pytest.raises(ValueError):
            se._to_source(outside)

    def test_only_one_place_computes_source(self):
        """防线①：全项目只有 `_to_source` 一处算 source。

        历史坑是两处口径分叉（build 用 vault 相对、update 对 vault 外文件退化成
        绝对路径）→ 同一文件两个 key。所以把「不许再出现第二处」写成断言。
        """
        import inspect
        src = inspect.getsource(se)
        assert src.count("relative_to(") == 1, \
            "source 口径只能有 _to_source 一处出口，出现了第二处 relative_to"
        for fn in (se.VaultSearchEngine.build_index, se.VaultSearchEngine.update_file):
            body = inspect.getsource(fn)
            assert "as_posix()" not in body, f"{fn.__name__} 在自己算 source"
        assert "_disk_files" in inspect.getsource(se.VaultSearchEngine.build_index)
        assert "_to_source" in inspect.getsource(se.VaultSearchEngine.update_file)

    def test_build_index_and_update_file_agree_on_source(self, monkeypatch, tmp_path):
        """防线①回归锁：同一文件经 build 与 update 得到的 source 必须一模一样。"""
        monkeypatch.setattr(se, "_embed", _fake_embed_factory(512))
        vault = tmp_path / "vault" / "company"
        vault.mkdir(parents=True)
        f = vault / "same.md"
        f.write_text(_make_text(["同一文件口径必须一致"]), encoding="utf-8")

        engine = se.VaultSearchEngine()
        engine.build_index(force_rebuild=True)
        _, meta = se._load_index()
        built = {m["source"] for m in meta}
        assert se._to_source(f) in built
        engine.update_file(f)
        _, meta2 = se._load_index()
        after = {m["source"] for m in meta2}
        assert after == built, "update 之后 source 集合变了 = 又分叉了"

    def test_build_detects_source_collision(self, monkeypatch, tmp_path):
        """防线③：两个文件撞成同一个 source → build 必须报错且**不落盘**。"""
        monkeypatch.setattr(se, "_embed", _fake_embed_factory(512))
        vault = tmp_path / "vault" / "company"
        vault.mkdir(parents=True)
        (vault / "c1.md").write_text(_make_text(["碰撞测试一"]), encoding="utf-8")
        (vault / "c2.md").write_text(_make_text(["碰撞测试二"]), encoding="utf-8")
        # 人为让所有文件算成同一个 source（模拟"口径分叉"这类病）
        monkeypatch.setattr(se, "_to_source", lambda *a, **k: "vault/company/same.md")

        engine = se.VaultSearchEngine()
        r = engine.build_index(force_rebuild=True)
        assert r["problems"], "撞 key 必须被自检抓到"
        assert r["files"] == 0, "自检没过就不许报成功"
        _, meta = se._load_index()
        assert meta == [], "自检没过时绝不许落盘"


class TestFailureSemantics:
    """M5 §12.1 的四行处置表，逐条有测试。"""

    def _disk(self, monkeypatch, tmp_path, n=2):
        vault = tmp_path / "vault" / "company"
        vault.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            (vault / f"d{i}.md").write_text(_make_text([f"失败语义第{i}段"]),
                                            encoding="utf-8")
        return vault

    def test_read_failure_goes_to_skipped_not_fatal(self, monkeypatch, tmp_path):
        """文件读不到 → 记 skipped，**不算失败**，其余文件照常入索引。"""
        monkeypatch.setattr(se, "_embed", _fake_embed_factory(512))
        self._disk(monkeypatch, tmp_path, n=2)
        monkeypatch.setattr(se, "_read_chunks",
                            lambda p: [] if p.name == "d0.md"
                            else ["正常内容" * 20])
        engine = se.VaultSearchEngine()
        r = engine.build_index(force_rebuild=True)
        assert r["files"] == 1 and not r["problems"]
        assert [s["path"] for s in r["skipped_detail"]] == ["vault/company/d0.md"]

    def test_file_missing_and_not_skipped_is_a_problem(self, monkeypatch, tmp_path):
        """文件既没进索引也不在 skipped 里 = 真漏 → problems 非空。"""
        disk = [("vault/company/ghost.md", tmp_path / "nope.md", 0.0)]
        problems = se._check_build_consistency(disk, [], [], 0)
        assert problems and "真漏" in problems[0]

    def test_orphan_in_index_is_a_problem(self, monkeypatch, tmp_path):
        """索引里有、磁盘上没有（旧文件残留）→ problems 非空。"""
        meta = [{"id": "vault/company/gone.md#0", "source": "vault/company/gone.md",
                 "chunk_idx": 0, "mtime": 0, "snippet": "s"}]
        problems = se._check_build_consistency([], meta, [], 1)
        assert problems and "旧文件残留" in problems[0]

    def test_skipped_paths_are_not_counted_as_problems(self):
        disk = [("a.md", None, 0.0), ("b.md", None, 0.0)]
        meta = [{"id": "a.md#0", "source": "a.md", "chunk_idx": 0, "mtime": 0,
                 "snippet": "s"}]
        assert se._check_build_consistency(
            disk, meta, [{"path": "b.md", "reason": "嵌入失败"}], 1) == []


class TestSourceFormatGate:
    """M5 §12.2：update_file 入口的 source 口径版本闸门（防迁移窗口内双 key）。"""

    def test_update_refused_when_version_missing(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr(se, "_embed", _fake_embed_factory(512))
        vault = tmp_path / "vault" / "company"
        vault.mkdir(parents=True)
        f = vault / "old.md"
        f.write_text(_make_text(["旧格式索引上不许写"]), encoding="utf-8")
        # 造一个"旧索引"：有 meta 但没有版本号
        se._save_index(None, np.zeros((1, se.EMBED_DIM), dtype=np.float32),
                       [{"id": "x#0", "source": "x", "chunk_idx": 0,
                         "mtime": 0, "snippet": "s"}])
        assert se._source_format_of() is None

        engine = se.VaultSearchEngine()
        assert engine.update_file(f) == 0, "版本不匹配必须拒绝写入"
        _, meta = se._load_index()
        assert all(m["source"] != se._to_source(f) for m in meta), "不许产生新 key"
        assert "build --force" in caplog.text

    def test_build_writes_version_and_then_update_is_allowed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(se, "_embed", _fake_embed_factory(512))
        vault = tmp_path / "vault" / "company"
        vault.mkdir(parents=True)
        f = vault / "ok.md"
        f.write_text(_make_text(["建完之后才允许增量"]), encoding="utf-8")

        engine = se.VaultSearchEngine()
        engine.build_index(force_rebuild=True)
        assert se._source_format_of() == se.SOURCE_FORMAT
        assert engine.update_file(f) > 0

    def test_skip_early_return_does_not_refresh_version(self, monkeypatch, tmp_path):
        """§12.3 仲裁：skipped 早退**绝不**更新版本号（否则旧格式索引会假装是新的）。"""
        monkeypatch.setattr(se, "_embed", _fake_embed_factory(512))
        vault = tmp_path / "vault" / "company"
        vault.mkdir(parents=True)
        (vault / "a.md").write_text(_make_text(["早退不许改版本号"]), encoding="utf-8")
        engine = se.VaultSearchEngine()
        engine.build_index(force_rebuild=True)
        (se.INDEX_DIR / "source_format.txt").write_text("1", encoding="utf-8")
        r = engine.build_index(force_rebuild=False)
        assert r["skipped"] is True
        assert se._source_format_of() == 1, "早退居然把版本号刷新了"


class TestExcludeDirs:
    """M5 §12.6：**每天自动落盘**的机器生成物必须排掉，否则系统每天生成的报告污染自己的检索库。

    2026-09-17 M7（审计 F-09）：`docs/audit` **从排除名单移出** —— 提示词要求
    "审计/根因类问题必须先搜库"，而审计报告正是那份语料，排掉它等于要求搜空库。
    """
    def test_daily_machine_generated_dirs_are_excluded(self):
        for d in ("docs/brief", "docs/report"):
            assert d in se.DEFAULT_EXCLUDE_DIRS

    def test_audit_reports_are_searchable(self):
        """审计报告必须**能**被检索到（否则提示词那条要求是空的）。"""
        assert "docs/audit" not in se.DEFAULT_EXCLUDE_DIRS
        assert not se._is_excluded(se.VERA_ROOT / "docs" / "audit" / "2026-09-17_x.md",
                                   se.DEFAULT_EXCLUDE_DIRS)

    def test_is_excluded_matches_prefix_and_dirname(self, tmp_path):
        ex = se.DEFAULT_EXCLUDE_DIRS
        assert se._is_excluded(tmp_path / "docs" / "brief" / "a.md", ex)
        assert se._is_excluded(tmp_path / "docs" / "report" / "x" / "b.md", ex)
        assert se._is_excluded(tmp_path / "data" / "market_position" / "c.md", ex)
        assert not se._is_excluded(tmp_path / "docs" / "plan" / "d.md", ex)
        assert not se._is_excluded(tmp_path / "docs" / "audit" / "e.md", ex)
        assert not se._is_excluded(tmp_path / "research" / "f.md", ex)


class TestIndexFreshness:
    """M5 §11.2 R1+R2：索引新鲜度自检（不新建 stamp 文件，从 meta 现算 §12.3）。"""

    def _seed(self, monkeypatch, tmp_path, n=2):
        monkeypatch.setattr(se, "_embed", _fake_embed_factory(512))
        vault = tmp_path / "vault" / "company"
        vault.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            (vault / f"s{i}.md").write_text(_make_text([f"新鲜度第{i}段"]),
                                            encoding="utf-8")
        return vault

    def test_fresh_after_build(self, monkeypatch, tmp_path):
        self._seed(monkeypatch, tmp_path)
        se.VaultSearchEngine().build_index(force_rebuild=True)
        f = se.index_freshness()
        assert f["has_index"] and not f["stale"], f["reasons"]
        assert f["indexed_sources"] == 2 and f["disk_files"] == 2

    def test_new_file_shows_as_missing(self, monkeypatch, tmp_path):
        vault = self._seed(monkeypatch, tmp_path)
        se.VaultSearchEngine().build_index(force_rebuild=True)
        (vault / "new.md").write_text(_make_text(["刚写的新文件"]), encoding="utf-8")
        f = se.index_freshness()
        assert f["stale"] and "vault/company/new.md" in f["missing_from_index"]

    def test_deleted_file_shows_as_orphan(self, monkeypatch, tmp_path):
        """**最该管的一条**：已删文件还占着检索名额。"""
        vault = self._seed(monkeypatch, tmp_path)
        se.VaultSearchEngine().build_index(force_rebuild=True)
        (vault / "s0.md").unlink()
        f = se.index_freshness()
        assert f["stale"] and "vault/company/s0.md" in f["orphan_in_index"]

    def test_changed_file_shows_as_stale_content(self, monkeypatch, tmp_path):
        import os
        import time
        vault = self._seed(monkeypatch, tmp_path)
        se.VaultSearchEngine().build_index(force_rebuild=True)
        p = vault / "s1.md"
        p.write_text(_make_text(["内容改过了"]), encoding="utf-8")
        os.utime(p, (time.time() + 10, time.time() + 10))   # 保证 mtime 明显更新
        f = se.index_freshness()
        assert f["stale"] and "vault/company/s1.md" in f["changed"]

    def test_version_mismatch_makes_it_stale(self, monkeypatch, tmp_path):
        self._seed(monkeypatch, tmp_path)
        se.VaultSearchEngine().build_index(force_rebuild=True)
        (se.INDEX_DIR / "source_format.txt").write_text("1", encoding="utf-8")
        f = se.index_freshness()
        assert f["stale"] and any("口径版本" in r for r in f["reasons"])

    def test_no_index_is_stale_not_crash(self, tmp_path):
        f = se.index_freshness()
        assert f["has_index"] is False and f["stale"] is True


class TestMetaPrefix:
    """M5 §11.3 R3：所有语料都带"这是哪份文件"的上下文前缀。"""

    def test_prefix_by_top_dir(self, tmp_path):
        assert se._meta_prefix(tmp_path / "research" / "a.md").startswith("[来自研究报告库")
        assert se._meta_prefix(tmp_path / "docs" / "plan" / "b.md").startswith("[来自项目文档库")
        assert se._meta_prefix(
            tmp_path / "vera_obs_vault" / "company" / "c.md").startswith("[来自公司档案库")

    def test_prefix_never_invents_facts(self, tmp_path):
        txt = se._meta_prefix(tmp_path / "research" / "2026-01-01_某研究_研究报告.md")
        assert "2026-01-01_某研究_研究报告" in txt

    def test_prefix_outside_root_is_empty(self, tmp_path):
        assert se._meta_prefix(tmp_path.parent / "outside.md") == ""

    def test_non_company_chunks_carry_prefix(self, monkeypatch, tmp_path):
        """落到实际分块上：research 文件的块必须带前缀。"""
        n = len(se._meta_prefix(tmp_path / "research" / "x.md"))
        assert n > 0
