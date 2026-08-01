# -*- coding: utf-8 -*-
"""对话大脑 API 端点 (TestClient): /api/research/chat + /reset。

brain.claude_cli.ask_brain 全 mock (不碰 claude CLI/网络)。
覆盖: 正常问答 (channel 映射)、空问题、非法会话号归 default (防注入)、
大脑异常不炸 server (松耦合)、reset 调对 channel。
"""
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
                       channel="default"):
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
