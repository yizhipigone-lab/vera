# -*- coding: utf-8 -*-
"""core/progress 细粒度进度报告器 + /api/status 融合测试 (2026-07-26)。

钉死:
1. 锚点映射: stage+frac → 全局 pct (formula 0.5 → 26+(45-26)*0.5=35.5)
2. frac 截断 [0,1]; 未知 stage 忽略
3. reset 清空状态
4. 速率法 ETA: done/total + 时间推进 → eta_s; 数据不足时 -1
5. snapshot 不含私有键
6. /api/status: running 时融合细粒度 (pct/detail/eta_s additive);
   单调不回退 guard; 非 running 不带 detail
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import progress  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    progress.reset()
    yield
    progress.reset()


class TestAnchors:
    def test_stage_frac_maps_to_global_pct(self):
        progress.report("formula", 0.5, "批次 25/51", 25, 51)
        snap = progress.snapshot()
        assert snap["pct"] == pytest.approx(26.0 + 19.0 * 0.5)
        assert snap["detail"] == "批次 25/51"

    def test_frac_clamped(self):
        progress.report("fetch", 1.7)
        assert progress.snapshot()["pct"] == 78.0
        progress.report("fetch", -0.2)
        assert progress.snapshot()["pct"] == 45.0

    def test_unknown_stage_ignored(self):
        progress.report("nope", 0.5, "x")
        assert progress.snapshot()["stage"] == ""

    def test_reset_clears(self):
        progress.report("loop", 0.5, "x", 50, 100)
        progress.reset()
        snap = progress.snapshot()
        assert snap["pct"] == 0.0 and snap["detail"] == "" and snap["ts"] == 0.0

    def test_snapshot_hides_private_keys(self):
        progress.report("formula", 0.1, "x", 1, 10)
        assert all(not k.startswith("_") for k in progress.snapshot())


class TestEta:
    def test_eta_from_rate(self, monkeypatch):
        t = [1000.0]
        monkeypatch.setattr(progress.time, "monotonic", lambda: t[0])
        progress.report("formula", 0.02, "", 1, 51)   # 建立速率基线
        t[0] += 1.0
        progress.report("formula", 0.22, "", 11, 51)  # 10 批/秒
        snap = progress.snapshot()
        assert snap["eta_s"] == pytest.approx((51 - 11) / 10.0)

    def test_eta_negative_when_insufficient_data(self, monkeypatch):
        t = [1000.0]
        monkeypatch.setattr(progress.time, "monotonic", lambda: t[0])
        progress.report("formula", 0.02, "", 1, 51)   # 首个样本, 无速率
        assert progress.snapshot()["eta_s"] == -1.0
        progress.report("formula", 0.5, "x")           # 无 done/total
        assert progress.snapshot()["eta_s"] == -1.0

    def test_eta_stage_change_rebaselines(self, monkeypatch):
        t = [1000.0]
        monkeypatch.setattr(progress.time, "monotonic", lambda: t[0])
        progress.report("st_filter", 0.2, "", 1000, 5000)
        t[0] += 2.0
        progress.report("st_filter", 0.6, "", 3000, 5000)  # 1000只/s
        assert progress.snapshot()["eta_s"] == pytest.approx(2.0)
        progress.report("formula", 0.02, "", 1, 51)   # 换阶段 → 重新基线
        assert progress.snapshot()["eta_s"] == -1.0


class TestStatusFusion:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from server import app
        return TestClient(app)

    def test_status_merges_fine_progress(self, client):
        import server
        server.pipeline_status.running = True
        server._last_served_pct = 0.0
        try:
            progress.report("formula", 0.5, "批次 25/51", 25, 51)
            body = client.get("/api/status").json()
            assert body["progress"] == pytest.approx(35.5, abs=0.01)
            assert body["detail"] == "批次 25/51"
            assert body["step"] == "公式选股"  # 细粒度阶段名替换粗粒度
            assert "eta_s" in body  # additive 字段存在
        finally:
            server.pipeline_status.running = False
            server._last_served_pct = 0.0

    def test_status_monotonic_no_regress(self, client):
        import server
        server.pipeline_status.running = True
        server._last_served_pct = 0.0
        try:
            progress.report("fetch", 0.5, "取数", 100, 200)   # 61.5
            p1 = client.get("/api/status").json()["progress"]
            progress.report("formula", 0.1, "批次 5/51")       # 乱序上报 → 27.9
            p2 = client.get("/api/status").json()["progress"]
            assert p2 >= p1  # 单调不回退
        finally:
            server.pipeline_status.running = False
            server._last_served_pct = 0.0

    def test_status_idle_no_detail(self, client):
        import server
        server.pipeline_status.running = False
        server._last_served_pct = 0.0
        progress.report("formula", 0.5, "批次 25/51", 25, 51)
        body = client.get("/api/status").json()
        assert body["detail"] == ""  # 非运行中不展示细粒度
