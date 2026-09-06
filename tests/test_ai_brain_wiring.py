# -*- coding: utf-8 -*-
"""AI 设置页签 · 标准/深度档接线单测 (红→绿)。

标准档: brain/claude_cli.py 有 standard 配置时 spawn env 注入
        ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_MODEL,
        无配置时 env 与现状一致 (零行为变化)。
深度档: brain/dsh_channel.py 的 settings.yaml 改写纯函数 —
        deep 配置 {provider, model} → 只替换 agent-default-model 两行,
        无 deep 配置不触碰文件。全部 tmp_path 隔离。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from llm import ai_config  # noqa: E402


# ──────────────────── 标准档: claude spawn env 注入 ────────────────────

def test_cli_env_with_standard_config(monkeypatch):
    """有 standard 配置 → env 含 ANTHROPIC_BASE_URL/AUTH_TOKEN/MODEL。"""
    from brain import claude_cli
    monkeypatch.setattr(ai_config, "path", lambda: Path("__never_exists__"))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://old.example.com")

    monkeypatch.setattr(
        claude_cli, "_standard_cfg",
        lambda: {"base_url": "https://api.z.ai/api/anthropic",
                 "api_key": "tok-abc-123", "model": "glm-5.3"})
    env = claude_cli._cli_env()
    assert env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "tok-abc-123"
    assert env["ANTHROPIC_MODEL"] == "glm-5.3"


def test_cli_env_without_standard_config(monkeypatch):
    """无 standard 配置 → env 不含 ANTHROPIC_MODEL 覆盖 (回落现状)。"""
    from brain import claude_cli
    monkeypatch.setattr(
        claude_cli, "_standard_cfg", lambda: None)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    env = claude_cli._cli_env()
    assert "ANTHROPIC_MODEL" not in env


def test_ask_brain_no_provider_warning_when_configured(monkeypatch):
    """AI 设置页配了 standard 档 → 不再对 ~/.claude provider 弹软告警。"""
    from brain import claude_cli
    # _check_provider 会"发现"非 DeepSeek (旧逻辑会告警的场景)
    monkeypatch.setattr(claude_cli, "_check_provider",
                        lambda *a, **kw: "provider 不是 DeepSeek(应被抑制)")
    monkeypatch.setattr(claude_cli, "_standard_cfg",
                        lambda: {"base_url": "https://api.z.ai/api/anthropic",
                                 "api_key": "tok", "model": "glm-5.3"})
    assert claude_cli._provider_warning() is None

    # 无 standard 配置 → 沿用旧逻辑 (非 DeepSeek 端点仍告警)
    monkeypatch.setattr(claude_cli, "_standard_cfg", lambda: None)
    assert claude_cli._provider_warning() == "provider 不是 DeepSeek(应被抑制)"


# ──────────────────── 深度档: DSH settings 改写纯函数 ────────────────────

_SETTINGS_TPL = (
    "# 第二套 DSH（VERA 深度思考通道）最小配置\n"
    "agent-default-model:\n"
    "  provider: deepseek-official\n"
    "  model: deepseek-v4-flash\n"
)


def test_rewrite_agent_default_model():
    """{provider, model} → 只改 agent-default-model 两行, 注释/其他保留。"""
    from brain.dsh_channel import rewrite_agent_default_model
    out = rewrite_agent_default_model(
        _SETTINGS_TPL, provider="zai", model="glm-5.3")
    assert out.startswith("# 第二套 DSH")
    assert "provider: zai\n" in out
    assert "model: glm-5.3\n" in out
    assert "deepseek-official" not in out
    assert "deepseek-v4-flash" not in out


def test_rewrite_keeps_other_content():
    """settings 里存在其他配置段时不丢失 (只动 agent-default-model 块)。"""
    from brain.dsh_channel import rewrite_agent_default_model
    src = _SETTINGS_TPL + "some-other-key:\n  enabled: true\n"
    out = rewrite_agent_default_model(src, provider="p", model="m")
    assert "some-other-key:\n  enabled: true\n" in out
    assert "provider: p\n" in out


def test_apply_deep_settings_no_config_untouched(tmp_path, monkeypatch):
    """无 deep 配置 → settings 文件原样不动。"""
    from brain import dsh_channel
    p = tmp_path / "settings.yaml"
    p.write_text(_SETTINGS_TPL, encoding="utf-8")
    ai_json = tmp_path / "ai.json"
    ai_json.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ai_config, "path", lambda: ai_json)
    monkeypatch.setattr(dsh_channel, "DSH_SETTINGS", p)
    dsh_channel.apply_deep_settings()
    assert p.read_text(encoding="utf-8") == _SETTINGS_TPL


def test_apply_deep_settings_writes_when_configured(tmp_path, monkeypatch):
    """有 deep 配置 → settings 的 agent-default-model 被改写。"""
    from brain import dsh_channel
    p = tmp_path / "settings.yaml"
    p.write_text(_SETTINGS_TPL, encoding="utf-8")
    ai_json = tmp_path / "ai.json"
    ai_json.write_text(
        '{"deep": {"provider": "deepseek-official", "model": "deepseek-v4-pro"}}',
        encoding="utf-8")
    monkeypatch.setattr(ai_config, "path", lambda: ai_json)
    monkeypatch.setattr(dsh_channel, "DSH_SETTINGS", p)
    dsh_channel.apply_deep_settings()
    text = p.read_text(encoding="utf-8")
    assert "model: deepseek-v4-pro\n" in text
    assert "deepseek-v4-flash" not in text


def test_apply_deep_settings_missing_file_ok(tmp_path, monkeypatch):
    """settings 文件不存在 (DSH 未部署) → 静默跳过不抛。"""
    from brain import dsh_channel
    ai_json = tmp_path / "ai.json"
    ai_json.write_text('{"deep": {"provider": "p", "model": "m"}}',
                       encoding="utf-8")
    monkeypatch.setattr(ai_config, "path", lambda: ai_json)
    monkeypatch.setattr(dsh_channel, "DSH_SETTINGS", tmp_path / "nope.yaml")
    dsh_channel.apply_deep_settings()  # 不抛即过
