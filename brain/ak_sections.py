"""brain/ak_sections.py — akshare 惰性加载 + Markdown 小节助手 (brain 级公共小模块)。

2026-09-15 深模块审计收口: data_tools 与 market_panel 分家时各自原样复制了
_HAS_AK/_ak/_section/_no_ak 四个助手 (迁移纪律自承"长期若再新增公共小工具,
应收 core/brain 级公共模块") —— 同一条规则不写第二份, 收口于此。
"""
from __future__ import annotations

import importlib.util

HAS_AK = importlib.util.find_spec("akshare") is not None


def ak():
    """惰性 import akshare (加载慢, 只在真用时付代价)。"""
    import akshare
    return akshare


def section(title: str, body: str) -> str:
    return f"## {title}\n\n{body}"


def no_ak() -> str:
    return "【缺】未安装 akshare（pip install akshare）"
