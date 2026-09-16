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
    """core/connector.TQCENTER_PATH 与 tdx_home 一致 (单一真相, 防再漂移)。

    2026-09-15 修复环境敏感: TQCENTER_PATH 在 connector 模块 import 时计算,
    机器级 TDX_HOME 已设置时 (如本机生产环境), 先前行测试已 import 过的
    模块常量与 delenv 后的 tdx_home() 必然不一致 → 环境红。reload 让模块
    在本用例的 delenv 状态下重算, 既保防漂移本意又与机器环境解耦。
    """
    monkeypatch.delenv("TDX_HOME", raising=False)
    import importlib

    from core import connector
    importlib.reload(connector)

    assert os.path.join(
        tdx_home(), "PYPlugins", "user", "tqcenter.py"
    ) == connector.TQCENTER_PATH
