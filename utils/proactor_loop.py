"""utils/proactor_loop.py — uvicorn 自定义 loop factory：Windows 强制 ProactorEventLoop。

2026-09-04 修复（大脑启动失败事件）：
uvicorn 0.44 官方 loops/asyncio.py 原文（已比对官方 wheel，非本机改坏）：
    if sys.platform == "win32" and not use_subprocess:
        return asyncio.ProactorEventLoop
    return asyncio.SelectorEventLoop
而 Config.use_subprocess = reload or workers>1。server.py 自 2026-09-02 默认
reload=True → 热加载子进程跑在 SelectorEventLoop 上 → Windows Selector 不支持
asyncio 子进程 → brain/claude_cli.py 的 create_subprocess_exec("claude", ...)
抛 NotImplementedError（无消息文本，前端只见"大脑启动失败: "冒号后空白）。
实测：reload → _WindowsSelectorEventLoop + NotImplementedError('')；
     无 reload → ProactorEventLoop + 子进程正常。

注意：uvicorn 0.44 经 asyncio.Runner(loop_factory=...) 起循环，
asyncio.set_event_loop_policy 完全无效，必须传 uvicorn.run(loop="模块:工厂")。
且自定义 loop 字符串走 config.py get_loop_factory 的 import_from_string 分支
直接返回、不经 use_subprocess 调用（内置工厂才会被调用），所以本工厂是
Runner 语义：无参调用 → 返回新事件循环实例（不是类、不接 use_subprocess）。
"""

from __future__ import annotations

import asyncio
import sys


def factory() -> asyncio.AbstractEventLoop:
    """uvicorn 自定义 loop factory（Runner 语义：无参调用，每次返回新实例）。

    Windows 恒 Proactor（支持子进程，brain/大盘快路径依赖）；其他平台回退
    平台默认。
    """
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop()
    return asyncio.new_event_loop()
