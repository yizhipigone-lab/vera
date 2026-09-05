"""tests/test_proactor_loop.py — 大脑启动失败修复 (2026-09-04) 守卫测试。

锁三件事：
1. utils/proactor_loop.factory 在 Windows 恒返 ProactorEventLoop（含
   use_subprocess=True —— 即 uvicorn reload/workers 模式；uvicorn 0.44 官方
   工厂此时返 SelectorEventLoop 不支持子进程，正是本次故障根因）。
2. server.py 两个 uvicorn.run 分支（reload / --no-reload）都接线 loop
   factory，防单入口漂移。
3. brain/claude_cli.py 流式分支「大脑启动失败」报错必须带异常类型名 ——
   空消息异常（如 NotImplementedError）不带类型名会打出冒号后空白。
"""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_factory_无参调用返回新实例():
    """uvicorn 自定义 loop 字符串走 config.py 的 import_from_string 直接返回,
    不经 use_subprocess 调用 —— 所以 factory 必须是 Runner 语义: 无参 → 新实例。"""
    from utils.proactor_loop import factory
    loop = factory()
    try:
        assert isinstance(loop, asyncio.AbstractEventLoop)
        assert not loop.is_running()
    finally:
        loop.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 专属修复")
def test_factory_Windows恒Proactor实例():
    from utils.proactor_loop import factory
    loop1, loop2 = factory(), factory()
    try:
        assert isinstance(loop1, asyncio.ProactorEventLoop)
        assert isinstance(loop2, asyncio.ProactorEventLoop)
        assert loop1 is not loop2  # 每次调用必须是新实例 (Runner 独占语义)
    finally:
        loop1.close()
        loop2.close()


@pytest.mark.skipif(sys.platform == "win32", reason="非 Windows 回退路径")
def test_factory_非Windows回退平台默认():
    from utils.proactor_loop import factory
    loop = factory()
    try:
        assert isinstance(loop, asyncio.AbstractEventLoop)
        assert not isinstance(loop, asyncio.ProactorEventLoop)
    finally:
        loop.close()


def test_server_两个uvicorn分支都接线loop_factory():
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "utils.proactor_loop:factory" in src, \
        "server.py 未定义 loop factory 接线"
    assert src.count("loop=_LOOP_FACTORY") >= 2, \
        "server.py 两个 uvicorn.run 分支（reload / --no-reload）都必须传 loop="


def test_大脑启动失败报错带异常类型名():
    src = (ROOT / "brain" / "claude_cli.py").read_text(encoding="utf-8")
    assert "大脑启动失败: {type(e).__name__}" in src, \
        "空消息异常（如 NotImplementedError）不带类型名会打出「大脑启动失败: 」空白"
