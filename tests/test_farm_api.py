# -*- coding: utf-8 -*-
"""farm_api + farm_runner 离线测试 (假 runner, 不起子进程, 不碰 TDX)。"""
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import farm_runner as fr
from core.farm_api import create_farm_router


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import core.farm_api as fa
    monkeypatch.setattr(fr, "LAST", tmp_path / "last_status.json")
    monkeypatch.setattr(fr, "DATA", tmp_path)
    monkeypatch.setattr(fa, "REPORTS", tmp_path / "reports")  # 隔离真实报告目录
    farm = fr.FarmRunner(runner=lambda run: 0)  # 假 runner: 秒回成功
    app = FastAPI()
    app.include_router(create_farm_router(farm, SimpleNamespace(running=False)))
    return TestClient(app), farm


def _wait_idle(farm, timeout=5):
    t0 = time.time()
    while farm.running and time.time() - t0 < timeout:
        time.sleep(0.05)


def test_status_lists_gates(client):
    c, farm = client
    d = c.get("/api/farm/status").json()
    assert d["running"] is False
    # 2026-09-11: 第四闸门 verify (定量复核: 重画 + 未来函数) 补上计划书 §4 步骤 6
    assert set(d["gates"]) == {"check", "onboard", "verify", "backtest"}


def test_verify_gate_is_startable(client):
    """第四闸门可通过 API 启动 (假 runner, 只验接线)。"""
    c, farm = client
    farm._runner = lambda run: 0
    r = c.post("/api/farm/verify")
    assert r.status_code == 200 and r.json() == {"ok": True, "gate": "verify"}
    _wait_idle(farm)
    d = c.get("/api/farm/status").json()
    assert d["last"]["verify"]["status"] == "done"


def test_start_check_then_status_running_or_done(client):
    c, farm = client
    r = c.post("/api/farm/check")
    assert r.status_code == 200 and r.json()["ok"]
    _wait_idle(farm)
    d = c.get("/api/farm/status").json()
    assert d["current"]["gate"] == "check"
    assert d["current"]["status"] in ("done", "failed")
    assert d["last"]["check"]["status"] == d["current"]["status"]


def test_busy_conflict_409(client):
    c, farm = client
    # 换一个"卡住"的假 runner
    farm._runner = lambda run: time.sleep(3) or 0
    assert c.post("/api/farm/check").status_code == 200
    r = c.post("/api/farm/onboard")
    assert r.status_code == 409
    farm.stop()  # 假 runner 没有真进程, 只验证不炸
    _wait_idle(farm)


def test_backtest_rejected_when_pipeline_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "LAST", tmp_path / "last_status.json")
    farm = fr.FarmRunner(runner=lambda run: 0)
    app = FastAPI()
    app.include_router(create_farm_router(farm, SimpleNamespace(running=True)))
    c = TestClient(app)
    r = c.post("/api/farm/backtest")
    assert r.status_code == 409


def test_report_endpoints(client, monkeypatch):
    import core.farm_api as fa
    c, farm = client
    # 无报告 → 空表
    assert c.get("/api/farm/reports").json()["items"] == []
    # 放一份报告再查
    fa.REPORTS.mkdir(parents=True, exist_ok=True)
    (fa.REPORTS / "2026-09-06_测试_报告.md").write_text("# 测试\n", encoding="utf-8")
    items = c.get("/api/farm/reports").json()["items"]
    assert items and items[0]["file"] == "2026-09-06_测试_报告.md"
    d = c.get("/api/farm/report", params={"file": items[0]["file"]}).json()
    assert "测试" in d["markdown"]
    # 路径穿越被拒
    assert c.get("/api/farm/report", params={"file": "../x.md"}).status_code == 400
    assert c.get("/api/farm/report", params={"file": "nope.md"}).status_code == 404
    # 清理
    (fa.REPORTS / "2026-09-06_测试_报告.md").unlink()
