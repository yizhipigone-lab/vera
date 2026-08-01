"""TDX 安装路径单一解析器 — 全仓唯一真相 (批次 6 · D2)。

优先级: 环境变量 TDX_HOME > 默认 E:\\NEW_TDX。
历史: 路径曾散落在 core/connector.py、trade/signals.py、tests/conftest.py、
tools/gs_*.py 多处, 换机器要改 N 处。此后一律 `from core.tdx_path import tdx_home`。
"""

from __future__ import annotations

import os

DEFAULT_TDX_HOME = r"E:\NEW_TDX"


def tdx_home() -> str:
    """返回通达信安装根目录 (优先 TDX_HOME 环境变量)。"""
    return os.environ.get("TDX_HOME", DEFAULT_TDX_HOME)


def tdx_plugins_user() -> str:
    """返回 TQ 插件目录 ({TDX_HOME}/PYPlugins/user)。"""
    return os.path.join(tdx_home(), "PYPlugins", "user")
