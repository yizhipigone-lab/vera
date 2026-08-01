"""core/tdx_path.py — TDX 路径单一解析器测试 (批次 6 · D2)。"""

import os

from core.tdx_path import DEFAULT_TDX_HOME, tdx_home, tdx_plugins_user


def test_default_when_env_missing(monkeypatch):
    monkeypatch.delenv("TDX_HOME", raising=False)
    assert tdx_home() == DEFAULT_TDX_HOME


def test_env_override(monkeypatch):
    monkeypatch.setenv("TDX_HOME", r"D:\MyTdx")
    assert tdx_home() == r"D:\MyTdx"
    assert tdx_plugins_user() == os.path.join(r"D:\MyTdx", "PYPlugins", "user")


def test_connector_uses_tdx_home(monkeypatch):
    """core/connector.TQCENTER_PATH 与 tdx_home 一致 (单一真相, 防再漂移)。"""
    monkeypatch.delenv("TDX_HOME", raising=False)
    from core import connector

    assert os.path.join(
        tdx_home(), "PYPlugins", "user", "tqcenter.py"
    ) == connector.TQCENTER_PATH
