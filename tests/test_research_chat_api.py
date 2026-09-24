# -*- coding: utf-8 -*-
"""对话大脑 API 端点 (TestClient): /api/research/chat + /reset。

brain.claude_cli.ask_brain 全 mock (不碰 claude CLI/网络)。
覆盖: 正常问答 (channel 映射)、空问题、非法会话号归 default (防注入)、
大脑异常不炸 server (松耦合)、reset 调对 channel。
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import brain.claude_cli as brain_cli  # noqa: E402
import brain.memory as brain_memory  # noqa: E402
from server import app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app)


def _mock_brain(monkeypatch, resp=None, capture=None):
    async def fake_ask(question, session_id=None, timeout=120, max_turns=8,
                       channel="default", on_line=None):
        if capture is not None:
            capture.update({"question": question, "channel": channel,
                            "timeout": timeout, "max_turns": max_turns})
        return resp or {"answer": "答: 据 `kg/graph.db` 共 3 条边",
                        "success": True, "low_confidence": False,
                        "warnings": [], "session_id": "s-1"}
    monkeypatch.setattr(brain_cli, "ask_brain", fake_ask)


def test_chat_ok_channel_mapping(client, monkeypatch):
    cap = {}
    _mock_brain(monkeypatch, capture=cap)
    r = client.post("/api/research/chat", json={"question": "持仓有哪些", "conv": "c1"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True and "答:" in body["answer"]
    assert cap["channel"] == "research_tab_c1"     # 会话 → 独立 channel
    assert cap["timeout"] == 300 and cap["max_turns"] == 20  # 网页档参数


def test_chat_empty_question(client):
    r = client.post("/api/research/chat", json={"question": "  "})
    assert r.json()["success"] is False


def test_chat_evil_conv_falls_back_default(client, monkeypatch):
    cap = {}
    _mock_brain(monkeypatch, capture=cap)
    for evil in ["../../etc", "a;b", "x" * 50, "中文会话"]:
        client.post("/api/research/chat", json={"question": "q", "conv": evil})
        assert cap["channel"] == "research_tab_default", evil


def test_chat_brain_exception_no_500(client, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("大脑爆炸")
    monkeypatch.setattr(brain_cli, "ask_brain", boom)
    r = client.post("/api/research/chat", json={"question": "q", "conv": "c1"})
    assert r.status_code == 200                      # 松耦合: 不抛 500
    assert r.json()["success"] is False and "大脑爆炸" in r.json()["answer"]


def test_reset_calls_memory(client, monkeypatch):
    called = {}
    monkeypatch.setattr(brain_memory, "reset_session",
                        lambda ch: called.setdefault("ch", ch))
    r = client.post("/api/research/chat/reset", json={"conv": "c9"})
    assert r.json()["success"] is True
    assert called["ch"] == "research_tab_c9"


# ---------------------------------------------------------------- 三态对话 (2026-09-05)

def test_chat_stream_fast_mode_uses_quick_chat(client, monkeypatch):
    """mode=fast: 只走 quick_chat (DeepSeek 直答), 不碰 claude/DSH/fastpath。"""
    import brain.dsh_channel as dsh
    import brain.fastpath as fp

    async def boom(*a, **k):
        raise AssertionError("fast 档不该调 DSH / ask_brain")

    monkeypatch.setattr(dsh, "run_dsh", boom)
    monkeypatch.setattr(brain_cli, "ask_brain", boom)

    async def no_fast(*a, **k):
        raise AssertionError("fast 档不该碰 fastpath (个股诊断是重活, 留标准/深度档)")

    monkeypatch.setattr(fp, "try_stock_diagnosis", no_fast)
    monkeypatch.setattr(fp, "try_market_brief", no_fast)

    cap = {}

    async def fake_quick(question, history=None, on_line=None, channel=None):
        cap.update({"q": question, "channel": channel})
        return {"answer": "秒回答", "success": True, "low_confidence": False,
                "warnings": [], "session_id": None}

    import brain.quick_chat as qc
    monkeypatch.setattr(qc, "quick_answer", fake_quick)
    r = client.post("/api/research/chat/stream",
                    json={"question": "什么是均线", "conv": "c1", "mode": "fast"})
    assert r.status_code == 200 and "秒回答" in r.text
    assert cap["channel"] == "research_tab_c1"   # 快速档也走 vault 沉淀


def test_chat_fast_mode_ignores_stock_diagnosis_question(client, monkeypatch):
    """mode=fast 边界: 个股诊断是重活 (取数几十秒+八段报告), 不进快速档。

    (2026-09-05 实测校正: 把 fastpath 的 agent 式成文指令喂给 DeepSeek 直答
    会输出"写作计划"而非成品; 问个股/大盘请用标准或深度档。)
    """
    import brain.fastpath as fp
    import brain.quick_chat as qc

    async def fake_diag(*a, **k):
        raise AssertionError("fast 档不该碰 fastpath 个股诊断")

    monkeypatch.setattr(fp, "try_stock_diagnosis", fake_diag)
    monkeypatch.setattr(fp, "try_market_brief", fake_diag)

    async def fake_quick(question, history=None, on_line=None, channel=None):
        return {"answer": "快速知识答", "success": True, "low_confidence": False,
                "warnings": [], "session_id": None}

    monkeypatch.setattr(qc, "quick_answer", fake_quick)
    r = client.post("/api/research/chat/stream",
                    json={"question": "宁德时代怎么样", "conv": "c1", "mode": "fast"})
    assert r.status_code == 200 and "快速知识答" in r.text


def test_chat_mode_standard_falls_to_ask_brain(client, monkeypatch):
    """mode=standard: 快路径不命中 → claude ask_brain (原标准大脑)。"""
    import brain.fastpath as fp
    cap = {}

    async def fake_ask(question, session_id=None, timeout=480, max_turns=40,
                       channel="default", on_line=None):
        cap.update({"q": question, "timeout": timeout, "max_turns": max_turns})
        return {"answer": "标准答", "success": True, "low_confidence": False,
                "warnings": [], "session_id": "s-2"}

    monkeypatch.setattr(brain_cli, "ask_brain", fake_ask)

    async def no_fast(*a, **k):
        return None

    monkeypatch.setattr(fp, "try_stock_diagnosis", no_fast)
    monkeypatch.setattr(fp, "try_market_brief", no_fast)
    r = client.post("/api/research/chat/stream",
                    json={"question": "复杂问题", "conv": "c1", "mode": "standard"})
    assert r.status_code == 200 and "标准答" in r.text
    assert cap["timeout"] == 480 and cap["max_turns"] == 40


def test_chat_mode_deep_routes_to_dsh_with_run_id(client, monkeypatch):
    """mode=deep 显式 → DSH; channel 事件带同 run_id (与旧 deep:true 等价)。"""
    import brain.dsh_channel as dsh  # noqa: E402  (本地 import 惯例: 靠近使用)
    cap = {}

    async def fake_run(question, history=None, run_id=None, on_line=None,
                       channel=None, **kw):
        cap["run_id"] = run_id
        return {"answer": "深答", "success": True, "low_confidence": False,
                "warnings": [], "session_id": None}

    monkeypatch.setattr(dsh, "run_dsh", fake_run)
    r = client.post("/api/research/chat/stream", json={
        "question": "深问", "conv": "c1", "mode": "deep",
        "history": [{"role": "user", "content": "上一问"}]})
    assert r.status_code == 200
    assert re.fullmatch(r"[0-9a-f]{12}", cap["run_id"])
    assert f'"run_id": "{cap["run_id"]}"' in r.text
    assert "深答" in r.text


def test_chat_stream_deep_routes_to_dsh(client, monkeypatch):
    import re as _re

    import brain.dsh_channel as dsh
    cap = {}

    async def fake_run(question, history=None, run_id=None, on_line=None,
                       channel=None, **kw):
        cap.update({"question": question, "history": history,
                    "run_id": run_id, "channel": channel})
        return {"answer": "DSH 答", "success": True, "low_confidence": False,
                "warnings": [], "session_id": None}

    monkeypatch.setattr(dsh, "run_dsh", fake_run)
    r = client.post("/api/research/chat/stream", json={
        "question": "深度问题", "conv": "c1", "deep": True,
        "history": [{"role": "user", "content": "上一问"}]})
    assert r.status_code == 200
    assert cap["history"] == [{"role": "user", "content": "上一问"}]
    assert cap["channel"] == "research_tab_c1"      # D12: vault 沉淀要用
    assert _re.fullmatch(r"[0-9a-f]{12}", cap["run_id"])  # 服务端生成 12 位 hex
    assert f'"run_id": "{cap["run_id"]}"' in r.text       # channel 事件带同一 run_id
    assert "DSH 答" in r.text


def test_chat_stream_no_deep_ignores_dsh(client, monkeypatch):
    """不勾选 = 现有路径零改动 (fastpath/ask_brain), DSH 不被触碰。"""
    import brain.dsh_channel as dsh

    async def boom(*a, **k):
        raise AssertionError("不该调 DSH")

    monkeypatch.setattr(dsh, "run_dsh", boom)
    _mock_brain(monkeypatch)
    import brain.fastpath as fp

    async def no_fast(*a, **k):
        return None

    monkeypatch.setattr(fp, "try_stock_diagnosis", no_fast)
    monkeypatch.setattr(fp, "try_market_brief", no_fast)
    r = client.post("/api/research/chat/stream",
                    json={"question": "普通问题", "conv": "c1"})
    assert r.status_code == 200 and "答:" in r.text


def test_chat_stop_calls_dsh(client, monkeypatch):
    import brain.dsh_channel as dsh
    called = {}

    def fake_stop(rid):
        called["rid"] = rid
        return True

    monkeypatch.setattr(dsh, "stop_dsh", fake_stop)
    r = client.post("/api/research/chat/stop", json={"run_id": "abc123"})
    assert r.json()["success"] is True and called["rid"] == "abc123"
