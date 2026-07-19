# -*- coding: utf-8 -*-
"""公式体检端点(TestClient): 提交/状态/历史/报告 + 互斥 409 + 校验 400"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from server import app, lab_status, pipeline_status  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app)


def test_lab_status_endpoint(client):
    r = client.get("/api/lab/status")
    assert r.status_code == 200
    body = r.json()
    assert "running" in body and "queue" in body


def test_lab_run_rejects_bad_formula(client):
    r = client.post("/api/lab/run", json={"formulas": ["../evil"]})
    assert r.status_code == 400


def test_lab_run_rejects_empty(client):
    r = client.post("/api/lab/run", json={"formulas": []})
    assert r.status_code == 400


def test_lab_run_accepts_and_defaults_tags(client):
    r = client.post("/api/lab/run", json={"formulas": ["TESTFX_API"], "tag2": None})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] and body["tag"] and body["tag2"]
    # 清理: 把任务标 failed 防工作线程真跑
    for t in lab_status._tasks:
        if "TESTFX_API" in t.formulas:
            t.status = "failed"


def test_lab_run_empty_tag2_means_single_window(client):
    """冒烟回归(2026-07-20): tag2="" 显式单窗口; 缺省才是近3年。"""
    r = client.post("/api/lab/run", json={"formulas": ["TESTFX_TAG2"], "tag": "20250719_20260718", "tag2": ""})
    assert r.status_code == 200 and r.json()["tag2"] is None
    r2 = client.post("/api/lab/run", json={"formulas": ["TESTFX_TAG2B"]})
    assert r2.json()["tag2"]  # 缺省自动生成近3年
    for t in lab_status._tasks:
        if t.formulas and t.formulas[0].startswith("TESTFX_TAG2"):
            t.status = "failed"



    r = client.get("/api/lab/history")
    assert r.status_code == 200
    items = r.json()["items"]
    quant = [i for i in items if i["formula"] == "QUANTQQ"]
    assert quant and quant[0]["rules"] >= 1 and "adopted" in quant[0]


def test_lab_report_endpoint(client):
    r = client.get("/api/lab/report", params={"formula": "QUANTQQ"})
    assert r.status_code == 200 and r.json()["success"]
    r2 = client.get("/api/lab/report", params={"formula": "NOSUCHFX"})
    assert r2.json()["success"] is False


def test_run_409_when_lab_running(client, monkeypatch):
    class _FakeLab:
        running = True
    monkeypatch.setattr("server.lab_status", _FakeLab())
    r = client.post("/api/run", json={"formula_name": "UPN", "start_time": "20240101",
                                      "end_time": "20250101"})
    assert r.status_code == 409
