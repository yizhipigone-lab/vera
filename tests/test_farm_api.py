# -*- coding: utf-8 -*-
"""farm_api + farm_runner 离线测试 (假 runner, 不起子进程, 不碰 TDX)。"""
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import farm_runner as fr
from farm_api import create_farm_router


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import farm_api as fa
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
    # 2026-09-16 看板计划书阶段 2: status 响应带成绩单 + 总览 (fail-soft 不拖垮接口)
    assert "summary" in d and "overview" in d
    assert set(d["summary"]) == {"check", "onboard", "verify", "backtest"}
    assert "funnel" in d["overview"] and "board" in d["overview"]


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


def test_stop_marks_run_stopped(client):
    """2026-09-16: 人工停止 → status=stopped 且不带退出码报错, 与真失败分开。"""
    c, farm = client
    farm._runner = lambda run: time.sleep(0.2) or 1   # 假 runner 慢吞吞返回非 0
    assert c.post("/api/farm/check").status_code == 200
    farm.stop()   # 无真进程, 只立「人工停止」标记
    _wait_idle(farm)
    d = c.get("/api/farm/status").json()
    assert d["last"]["check"]["status"] == "stopped"
    assert d["last"]["check"]["error"] == ""


def test_failure_without_stop_still_failed(client):
    """对照组: 没点停止的真失败, 仍记 failed + 退出码。"""
    c, farm = client
    farm._runner = lambda run: 1
    assert c.post("/api/farm/check").status_code == 200
    _wait_idle(farm)
    d = c.get("/api/farm/status").json()
    assert d["last"]["check"]["status"] == "failed"
    assert "退出码" in d["last"]["check"]["error"]


# ── 2026-09-16: 失败病因结构化 (完整日志落盘 + 真错误行提取 + /api/farm/log) ──

def test_extract_error_traceback():
    lines = ["启动", "Traceback (most recent call last):",
             '  File "farm_onboard.py", line 21, in <module>',
             "    import psutil",
             "ModuleNotFoundError: No module named 'psutil'"]
    assert fr._extract_error(lines) == "ModuleNotFoundError: No module named 'psutil'"


def test_extract_error_picks_last_traceback():
    lines = ["Traceback (most recent call last):", "ValueError: first",
             "重试中", "Traceback (most recent call last):", "KeyError: 'second'"]
    assert fr._extract_error(lines) == "KeyError: 'second'"


def test_extract_error_traceback_block_end_ignores_trailing_output():
    """复审 M3: Traceback 块之后脚本还有输出时, 必须抓块末的异常行,
    不能抓整个日志的最后一行 (无关行)。"""
    lines = ["启动", "Traceback (most recent call last):",
             '  File "x.py", line 5, in <module>', "    boom()",
             "RuntimeError: 真病因",
             "   ! 未来函数甄别 第 1 次失败: rc=1", "   · 20 秒后重试"]
    assert fr._extract_error(lines) == "RuntimeError: 真病因"


def test_extract_error_human_reason_fallback():
    """无 Traceback 时取脚本自己打印的人话原因 (SystemExit 前的 log)。"""
    lines = ["[1/4] 检查通达信进程...", "没有待入库清单 — 先点『检查增量』"]
    assert fr._extract_error(lines) == "没有待入库清单 — 先点『检查增量』"


def test_failed_gate_writes_log_and_extracts_reason(tmp_path, monkeypatch):
    """端到端: 真子进程报错 → 完整日志落盘 + error 带真病因 + last 记 log_file。"""
    script = tmp_path / "boom.py"
    script.write_text(
        "print('启动中')\nraise ModuleNotFoundError(\"No module named 'psutil'\")\n",
        encoding="utf-8")
    monkeypatch.setattr(fr, "LAST", tmp_path / "last_status.json")
    monkeypatch.setattr(fr, "DATA", tmp_path)
    monkeypatch.setitem(fr.GATES, "check", ("检查增量", script))
    farm = fr.FarmRunner()   # 真 runner, 起子进程
    run, err = farm.start("check")
    assert run is not None and not err
    _wait_idle(farm)
    st = farm.status()
    cur = st["current"]
    assert cur["status"] == "failed"
    assert "退出码 1" in cur["error"]
    assert "ModuleNotFoundError: No module named 'psutil'" in cur["error"]
    log_file = cur["log_file"]
    assert log_file
    content = open(log_file, encoding="utf-8").read()
    assert "启动中" in content and "Traceback" in content
    assert st["last"]["check"]["log_file"] == log_file


def test_farm_log_endpoint(client, monkeypatch):
    import farm_api as fa
    c, farm = client
    monkeypatch.setattr(fa, "RUNS", fa.REPORTS.parent / "runs")
    # 没跑过 → 404; 未知闸门 → 400
    assert c.get("/api/farm/log", params={"gate": "check"}).status_code == 404
    assert c.get("/api/farm/log", params={"gate": "hack"}).status_code == 400
    # 落一份日志 (glob 兜底路径) → 200 且内容完整
    d = fa.RUNS / "2026-09-16"
    d.mkdir(parents=True)
    (d / "check.log").write_text("第一行\nTraceback (most recent call last):\n"
                                 "ModuleNotFoundError: boom\n", encoding="utf-8")
    r = c.get("/api/farm/log", params={"gate": "check"})
    assert r.status_code == 200
    body = r.json()
    assert "ModuleNotFoundError: boom" in body["log"]
    assert body["truncated"] is False


def test_farm_log_rejects_tampered_path(client, monkeypatch):
    """复审 LOW: last_status.json 被改 → 指向 runs 外必须 400, 非字符串不炸 500。"""
    import farm_api as fa
    c, farm = client
    monkeypatch.setattr(fa, "RUNS", fa.REPORTS.parent / "runs")
    fa.RUNS.mkdir(parents=True, exist_ok=True)
    evil = fa.REPORTS.parent / "evil.log"
    evil.write_text("secret", encoding="utf-8")
    farm._last = {"check": {"log_file": str(evil)}}
    assert c.get("/api/farm/log", params={"gate": "check"}).status_code == 400
    farm._last = {"check": {"log_file": 123}}   # 篡改非字符串 → 兜底 glob → 404
    assert c.get("/api/farm/log", params={"gate": "check"}).status_code == 404


def test_farm_log_truncates_long_file(client, monkeypatch):
    """复审 LOW: 超上限只 tail-read 并标注, 不全文读入。"""
    import farm_api as fa
    c, farm = client
    monkeypatch.setattr(fa, "RUNS", fa.REPORTS.parent / "runs")
    monkeypatch.setattr(fa, "LOG_MAX_CHARS", 100)
    d = fa.RUNS / "2026-09-16"
    d.mkdir(parents=True)
    (d / "check.log").write_text("x" * 500, encoding="utf-8")
    r = c.get("/api/farm/log", params={"gate": "check"})
    assert r.status_code == 200
    body = r.json()
    assert body["truncated"] is True
    assert "日志过长" in body["log"]
    assert body["log"].endswith("x" * 100)


def test_farm_log_does_not_pay_summary_cost(client, monkeypatch):
    """审计 L4: 读日志走 last_status() 轻路径 —— 全程不触发全量汇总。"""
    import farm_api as fa
    from core import farm_summary as fsm
    c, farm = client
    monkeypatch.setattr(fa, "RUNS", fa.REPORTS.parent / "runs")
    d = fa.RUNS / "2026-09-16"
    d.mkdir(parents=True)
    (d / "check.log").write_text("x", encoding="utf-8")
    calls = []
    monkeypatch.setattr(fsm, "gate_summaries", lambda *a: calls.append(1) or {})
    assert c.get("/api/farm/log", params={"gate": "check"}).status_code == 200
    assert not calls


# ── 2026-09-16 达标榜回填回测页: /api/farm/prefill ──

def _write_archive(fa, gs="GS0607", params=True):
    best = {"key": "k", "annret": 0.244,
            "params": {"cost": -0.2, "act": 0.08, "dd": 0.005, "ladder": "off",
                       "time_days": 20, "cond_days": 0, "cond_profit": 0.0}} \
        if params else None
    fa.FARM_DATA.mkdir(parents=True, exist_ok=True)
    (fa.FARM_DATA / "archive.json").write_text(__import__("json").dumps(
        {gs: {"file": "a.md", "url": "http://x", "best": best,
              "verdict": {"code": "pass"}, "window": ["2024-09-11", "2026-09-16"]}}),
        encoding="utf-8")


def test_prefill_endpoint_200(client, monkeypatch):
    import farm_api as fa
    from core import farm_summary as fsm
    c, farm = client
    monkeypatch.setattr(fa, "FARM_DATA", fa.REPORTS.parent)
    fsm._CACHE.clear()
    _write_archive(fa)
    r = c.get("/api/farm/prefill", params={"gs": "GS0607"})
    assert r.status_code == 200
    body = r.json()
    assert body["gs"] == "GS0607"
    assert body["caliber"]["universe_type"] == "23"
    assert "硬止损20%" in body["combo_text"]


def test_prefill_endpoint_errors(client, monkeypatch):
    import farm_api as fa
    from core import farm_summary as fsm
    c, farm = client
    monkeypatch.setattr(fa, "FARM_DATA", fa.REPORTS.parent)
    fsm._CACHE.clear()
    _write_archive(fa)
    assert c.get("/api/farm/prefill", params={"gs": "evil"}).status_code == 400
    assert c.get("/api/farm/prefill", params={"gs": "GS9999"}).status_code == 404
    fsm._CACHE.clear()
    _write_archive(fa, gs="GS0002", params=False)
    assert c.get("/api/farm/prefill", params={"gs": "GS0002"}).status_code == 409


def test_backtest_rejected_when_pipeline_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "LAST", tmp_path / "last_status.json")
    farm = fr.FarmRunner(runner=lambda run: 0)
    app = FastAPI()
    app.include_router(create_farm_router(farm, SimpleNamespace(running=True)))
    c = TestClient(app)
    r = c.post("/api/farm/backtest")
    assert r.status_code == 409


def test_report_endpoints(client, monkeypatch):
    import farm_api as fa
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
