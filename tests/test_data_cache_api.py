# -*- coding: utf-8 -*-
"""数据准备 TAB API 端点 (TestClient): /api/data_cache/* 。

core.kline_cache_maintenance 全 mock (不碰 sqlite/子进程)。
覆盖: 状态总览转发、手动补拉参数校验、补拉已在运行不重复起、日志 tail 钳制、
松耦合异常兜底。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import core.kline_cache_maintenance as maint  # noqa: E402
from server import app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app)


def _mock_status(monkeypatch, refreshing=False):
    monkeypatch.setattr(maint, "cache_status", lambda: {
        "expected": "20260813", "refreshing": refreshing,
        "periods": [{"period": "5m", "stocks": 6098, "first_date": "20240627",
                     "last_date": "20260810", "not_intact": 3,
                     "expected": "20260813", "stale": True}]})


class TestStatus:
    def test_状态总览转发(self, client, monkeypatch):
        _mock_status(monkeypatch, refreshing=True)
        r = client.get("/api/data_cache/status")
        body = r.json()
        assert body["success"] is True
        assert body["refreshing"] is True
        p = body["periods"][0]
        assert p["period"] == "5m" and p["stale"] is True and p["stocks"] == 6098

    def test_异常兜底(self, client, monkeypatch):
        def _boom():
            raise RuntimeError("manifest 炸了")
        monkeypatch.setattr(maint, "cache_status", _boom)
        r = client.get("/api/data_cache/status")
        assert r.json()["success"] is False   # 不 500


class TestBackfill:
    def test_正常启动(self, client, monkeypatch):
        cap = {}
        def fake_start(segments=None, trigger="manual", universe=None,
                       end="", limit=0):
            cap.update({"segments": segments, "trigger": trigger,
                        "universe": universe, "end": end, "limit": limit})
            return {"started": True, "trigger": trigger, "periods": ["5m"]}
        monkeypatch.setattr(maint, "start_backfill", fake_start)
        r = client.post("/api/data_cache/backfill", json={
            "period": "5m", "start": "20240627", "end": "20260813",
            "universe": "24", "limit": 20})
        body = r.json()
        assert body["success"] is True
        assert cap["segments"] == [("5m", "20240627")]
        assert cap["trigger"] == "manual_tab"
        assert cap["universe"] == "24" and cap["end"] == "20260813"
        assert cap["limit"] == 20

    def test_已在运行不重复起(self, client, monkeypatch):
        monkeypatch.setattr(maint, "start_backfill", lambda **kw: {
            "started": False, "reason": "其他进程补拉进行中（refresh.lock）"})
        r = client.post("/api/data_cache/backfill", json={
            "period": "5m", "start": "20240627"})
        body = r.json()
        assert body["success"] is False
        assert "进行中" in body["reason"]

    @pytest.mark.parametrize("payload", [
        {"period": "3m", "start": "20240627"},          # 非法周期
        {"period": "5m", "start": "2024-06-27"},        # 日期格式错
        {"period": "5m", "start": "20240627", "end": "x"},
        {"period": "5m", "start": "20240627", "universe": "99"},
        {"period": "5m", "start": "20240627", "limit": 99999},
        {"period": "5m"},                                # 缺 start
    ])
    def test_参数校验(self, client, payload):
        r = client.post("/api/data_cache/backfill", json=payload)
        assert r.json()["success"] is False


class TestLog:
    def test_日志尾部(self, client, monkeypatch):
        monkeypatch.setattr(maint, "read_log_tail",
                            lambda n: [f"line{i}" for i in range(n)])
        r = client.get("/api/data_cache/log?tail=10")
        body = r.json()
        assert body["success"] is True and len(body["lines"]) == 10

    def test_tail钳制(self, client, monkeypatch):
        cap = {}
        monkeypatch.setattr(maint, "read_log_tail",
                            lambda n: cap.setdefault("n", n) or [])
        client.get("/api/data_cache/log?tail=99999")
        assert cap["n"] == 500
