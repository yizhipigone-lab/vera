# -*- coding: utf-8 -*-
"""AI 设置页签 · 后端纯模块单测 (2026-09-06)。

llm/ai_config.py: config/ai.json 读写 / Key 打码 / 分档读取回落。
llm/providers.py: fast 档配置热生效 (base_url/api_key/默认模型), 无配置回落 .env。

全部用 tmp_path 隔离, 绝不碰项目真实 config/ai.json (防投毒惯例)。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from llm import ai_config  # noqa: E402


# ──────────────────────────── ai_config 纯读写 ────────────────────────────

def test_mask_key():
    assert ai_config.mask_key("") == ""
    assert ai_config.mask_key("short") == "****"
    assert ai_config.mask_key("sk-1234567890abcd") == "sk-1****abcd"
    assert ai_config.mask_key(None) == ""


def test_save_load_roundtrip(tmp_path):
    p = tmp_path / "ai.json"
    cfg = {"fast": {"base_url": "https://x.com", "api_key": "sk-secret123", "model": "m1"}}
    ai_config.save(cfg, path_=p)
    assert ai_config.load(p) == cfg


def test_load_missing_returns_empty(tmp_path):
    assert ai_config.load(tmp_path / "nope.json") == {}


def test_load_corrupt_returns_empty(tmp_path):
    p = tmp_path / "ai.json"
    p.write_text("{not json", encoding="utf-8")
    assert ai_config.load(p) == {}


def test_section_missing_and_blank_returns_none(tmp_path):
    p = tmp_path / "ai.json"
    assert ai_config.section("fast", path_=p) is None  # 文件不存在
    ai_config.save({"fast": {}}, path_=p)
    assert ai_config.section("fast", path_=p) is None  # 空 dict
    ai_config.save({"standard": {"api_key": "k"}}, path_=p)
    assert ai_config.section("fast", path_=p) is None  # 该档缺失


def test_section_returns_dict(tmp_path):
    p = tmp_path / "ai.json"
    ai_config.save({"fast": {"base_url": "u", "api_key": "k", "model": "m"}}, path_=p)
    assert ai_config.section("fast", path_=p) == {"base_url": "u", "api_key": "k", "model": "m"}


# ──────────────────── merge_patch: 合并语义唯一实现 ────────────────────

def _full_old():
    return {
        "fast": {"base_url": "https://fast.old", "api_key": "fk-old", "model": "fm-old"},
        "standard": {"base_url": "https://std.old", "api_key": "tk-old", "model": "tm-old"},
        "deep": {"provider": "deepseek-official", "model": "deep-v4-pro"},
    }


def test_merge_patch_field_override_blank_keeps():
    """逐字段: 非空覆盖, 空串/缺省保留旧值 (三档同语义, 防漂移)。"""
    patch = {
        "fast": {"base_url": "https://fast.new", "api_key": "", "model": "fm-new"},
        "standard": {"base_url": "", "api_key": "tk-new", "model": ""},
        "deep": {"provider": "zai", "model": ""},
    }
    out = ai_config.merge_patch(_full_old(), patch)
    assert out["fast"] == {"base_url": "https://fast.new", "api_key": "fk-old", "model": "fm-new"}
    assert out["standard"] == {"base_url": "https://std.old", "api_key": "tk-new", "model": "tm-old"}
    # deep 留空 → 保留旧值 (回归: 曾用 is-not-None 把空串写成空白)
    assert out["deep"] == {"provider": "zai", "model": "deep-v4-pro"}


def test_merge_patch_missing_section_untouched():
    """patch 没出现的档 → 原样保留。"""
    out = ai_config.merge_patch(_full_old(), {"fast": {"base_url": "x", "api_key": "y", "model": "z"}})
    assert out["standard"] == _full_old()["standard"]
    assert out["deep"] == _full_old()["deep"]


def test_merge_patch_clear_flag_zeroes_section():
    """档内 __clear__: true → 整档清空 (回落默认), 其他档不动。"""
    patch = {
        "fast": {"__clear__": True},
        "deep": {"__clear__": True},
    }
    out = ai_config.merge_patch(_full_old(), patch)
    assert out["fast"] == {"base_url": "", "api_key": "", "model": ""}
    assert out["deep"] == {"provider": "", "model": ""}
    assert out["standard"] == _full_old()["standard"]  # 未标清空的档保留


def test_merge_patch_clear_falsy_string_not_clear():
    """__clear__ 传非布尔真值 (如 "false") → 不当清空处理 (防误清)。"""
    out = ai_config.merge_patch(_full_old(), {"fast": {"__clear__": "false"}})
    assert out["fast"] == _full_old()["fast"]  # 原样保留 (字段遍历忽略未知键)


def test_merge_patch_empty_old_and_patch():
    """old/patch 都空/缺 → 返回三档空壳 (可写盘形状)。"""
    out = ai_config.merge_patch({}, {})
    assert set(out.keys()) == {"fast", "standard", "deep"}


# ──────────────────── providers: fast 配置热生效 ────────────────────

class _FakeResp:
    def __init__(self, json_body=None, status=200):
        self._body = json_body or {"choices": [{"message": {"content": "ok"}}]}
        self.status_code = status
        self.text = "err" if status != 200 else ""

    def json(self):
        return self._body


def _capture_post(monkeypatch, captured):
    """拦截 requests.post, 记录参数并返 _FakeResp。"""
    import requests as _requests
    from llm import providers

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResp()
    monkeypatch.setattr(providers.requests, "post", fake_post)


def test_chat_uses_ai_config_fast_when_configured(tmp_path, monkeypatch):
    """配置了 fast 档 → chat 用其 base_url/api_key/model (热生效核心)。"""
    from llm import providers
    p = tmp_path / "ai.json"
    ai_config.save({"fast": {"base_url": "https://my.openai/v1",
                             "api_key": "cfg-key-123",
                             "model": "cfg-model"}}, path_=p)
    monkeypatch.setattr(ai_config, "path", lambda: p)   # 让 _fast_cfg 读到 tmp
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    captured = {}
    _capture_post(monkeypatch, captured)
    c = providers.get_client()
    r = c.chat([{"role": "user", "content": "hi"}])
    assert r == "ok"
    assert captured["url"] == "https://my.openai/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer cfg-key-123"
    assert captured["json"]["model"] == "cfg-model"


def test_chat_falls_back_to_env_when_no_config(tmp_path, monkeypatch):
    """无 fast 配置 → 回落 .env DEEPSEEK_API_KEY + 内置 base/model (零行为变化)。"""
    from llm import providers
    p = tmp_path / "no.json"
    monkeypatch.setattr(ai_config, "path", lambda: p)   # 指向不存在文件
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key-999")
    captured = {}
    _capture_post(monkeypatch, captured)
    r = providers.get_client().chat([{"role": "user", "content": "hi"}])
    assert r == "ok"
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer env-key-999"
    assert captured["json"]["model"] == "deepseek-v4-flash"


def test_chat_skips_without_any_key(tmp_path, monkeypatch):
    """任何来源都无 key → 返 None 不请求 (松耦合)。"""
    from llm import providers
    p = tmp_path / "no.json"
    monkeypatch.setattr(ai_config, "path", lambda: p)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    hit = {"n": 0}
    import requests as _requests
    def fake_post(*a, **kw):
        hit["n"] += 1
        return _FakeResp()
    monkeypatch.setattr(providers.requests, "post", fake_post)
    assert providers.get_client().chat([{"role": "user", "content": "hi"}]) is None
    assert hit["n"] == 0
