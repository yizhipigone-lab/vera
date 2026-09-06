# -*- coding: utf-8 -*-
"""AI 设置页签 · API 端点单测 (TestClient): /api/ai/*。

覆盖: 读取打码 (Key 绝不明文回传)、保存合并 (留空保留旧 Key)、
连通测试转发 (fast=OpenAI 兼容, standard=Anthropic)。全部 tmp_path 隔离。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ai_api import router as ai_router  # noqa: E402
from llm import ai_config  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """独立 app (只挂 ai 路由) + config/ai.json 指向 tmp。"""
    monkeypatch.setattr(ai_config, "path", lambda: tmp_path / "ai.json")
    app = FastAPI()
    app.include_router(ai_router)
    return TestClient(app)


def test_get_config_empty_returns_all_sections(client):
    """无配置时 GET 返回三档空壳, success=True, 不炸。"""
    r = client.get("/api/ai/config")
    assert r.status_code == 200
    d = r.json()
    assert d["success"] is True
    assert set(d["config"].keys()) == {"fast", "standard", "deep"}
    assert d["config"]["fast"]["key_mask"] == ""
    assert d["config"]["fast"]["key_set"] is False


def test_save_then_get_masks_key(client):
    """保存后 GET 只回 mask, 绝不明文回传 Key。"""
    body = {"fast": {"base_url": "https://api.deepseek.com",
                     "api_key": "sk-super-secret-1234",
                     "model": "deepseek-v4-flash"}}
    r = client.post("/api/ai/save", json=body)
    assert r.status_code == 200
    assert r.json()["success"] is True

    d = client.get("/api/ai/config").json()
    fast = d["config"]["fast"]
    assert fast["key_set"] is True
    assert fast["key_mask"] == "sk-s****1234"
    assert "sk-super-secret-1234" not in str(d)      # 明文不可见 (含 key_mask 字段)


def test_save_blank_key_keeps_old(client):
    """保存时 Key 留空/缺省 → 保留旧 Key 不清空 (合并语义)。"""
    client.post("/api/ai/save", json={"fast": {"base_url": "https://a.com",
                                               "api_key": "sk-old-key-9999",
                                               "model": "m1"}})
    r = client.post("/api/ai/save", json={"fast": {"base_url": "https://b.com",
                                                   "api_key": "",
                                                   "model": "m2"}})
    assert r.status_code == 200
    cfg = ai_config.load()
    assert cfg["fast"]["api_key"] == "sk-old-key-9999"   # Key 未清
    assert cfg["fast"]["base_url"] == "https://b.com"     # 其他字段已更新
    assert cfg["fast"]["model"] == "m2"


def test_save_deep_blank_keeps_old(client):
    """保存时 deep 档 provider/model 留空 → 保留旧值 (与 fast/standard 同语义)。

    回归: 早期 deep 分支用 is-not-None 判断, 而前端全量提交三档,
    空串 "" is not None → 覆盖为空白 — 用户只改快速档保存会把深度档清掉。
    """
    client.post("/api/ai/save", json={"deep": {"provider": "deepseek-official",
                                               "model": "deepseek-v4-pro"}})
    # 前端 collectBody 总是带全三档; deep 留空保存
    r = client.post("/api/ai/save", json={
        "fast": {"base_url": "https://api.deepseek.com", "api_key": "", "model": "m"},
        "standard": {"base_url": "", "api_key": "", "model": ""},
        "deep": {"provider": "", "model": ""}})
    assert r.status_code == 200
    cfg = ai_config.load()
    assert cfg["deep"]["provider"] == "deepseek-official"  # 未被空串清掉
    assert cfg["deep"]["model"] == "deepseek-v4-pro"


def test_save_invalid_shape_400_no_write(client, tmp_path):
    """非法 shape (非 dict) → 400, 不写盘。"""
    r = client.post("/api/ai/save", json={"fast": "not-a-dict"})
    assert r.status_code == 400
    assert not (tmp_path / "ai.json").exists()


def test_test_fast_calls_openai_compat(client, monkeypatch):
    """连通测试 fast → POST {base}/chat/completions (OpenAI 兼容)。"""
    import ai_api as api_mod
    captured = {}

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "pong"}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(api_mod.requests, "post", fake_post)
    body = {"which": "fast", "base_url": "https://my.openai/v1",
            "api_key": "sk-test-1", "model": "m-fast"}
    r = client.post("/api/ai/test", json=body)
    assert r.status_code == 200
    d = r.json()
    assert d["success"] is True
    assert captured["url"] == "https://my.openai/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test-1"
    assert captured["json"]["model"] == "m-fast"


def test_test_standard_calls_anthropic_messages(client, monkeypatch):
    """连通测试 standard → POST {base}/v1/messages (Anthropic 兼容)。"""
    import ai_api as api_mod
    captured = {}

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"content": [{"text": "pong"}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(api_mod.requests, "post", fake_post)
    body = {"which": "standard", "base_url": "https://api.z.ai/api/anthropic",
            "api_key": "tok-test-1", "model": "glm-5.3"}
    r = client.post("/api/ai/test", json=body)
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert captured["url"] == "https://api.z.ai/api/anthropic/v1/messages"
    assert captured["json"]["model"] == "glm-5.3"


def test_save_clear_flag_zeroes_section(client):
    """前端「清空本档」→ __clear__:true → 该档置空回落默认, 其他档保留。"""
    client.post("/api/ai/save", json={
        "fast": {"base_url": "https://a.com", "api_key": "sk-f-1", "model": "m1"},
        "deep": {"provider": "deepseek-official", "model": "deep-v4-pro"}})
    r = client.post("/api/ai/save", json={"fast": {"__clear__": True}})
    assert r.status_code == 200
    cfg = ai_config.load()
    assert cfg["fast"] == {"base_url": "", "api_key": "", "model": ""}
    assert cfg["deep"] == {"provider": "deepseek-official", "model": "deep-v4-pro"}


def test_save_unknown_field_ignored(client):
    """档内未知字段 → 忽略不落盘 (字段白名单)。"""
    r = client.post("/api/ai/save", json={
        "fast": {"base_url": "https://a.com", "api_key": "sk-x", "model": "m",
                 "not_a_field": "boom"}})
    assert r.status_code == 200
    cfg = ai_config.load()
    assert "not_a_field" not in cfg["fast"]


def test_test_missing_which_400(client):
    r = client.post("/api/ai/test", json={"which": "deep"})
    assert r.status_code == 400


def test_test_http_error_returns_false_with_hint(client, monkeypatch):
    """HTTP 非 200 → success=False + 人话摘要, 不抛。"""
    import ai_api as api_mod

    class _Resp:
        status_code = 401
        text = "invalid api key"

        def json(self):
            return {}

    def fake_post(*a, **kw):
        return _Resp()

    monkeypatch.setattr(api_mod.requests, "post", fake_post)
    body = {"which": "fast", "base_url": "https://api.deepseek.com",
            "api_key": "sk-bad", "model": "deepseek-v4-flash"}
    r = client.post("/api/ai/test", json=body)
    d = r.json()
    assert d["success"] is False
    assert "401" in d["message"] or "invalid api key" in d["message"]
