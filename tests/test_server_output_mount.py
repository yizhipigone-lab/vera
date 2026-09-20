# -*- coding: utf-8 -*-
"""tests/test_server_output_mount.py — server.py 模块级 mount 容忍 output/ 不存在
(2026-09-20 CI 修红)。

实证: CI checkout 后 output/ 不存在 (gitignore), server.py:88 模块级
`app.mount("/output", StaticFiles(...))` 在 starlette 默认 check_dir=True 下
直接 RuntimeError, pytest 收集期炸 (exit 2, Test 步骤 7~9 秒挂)。
修法: mount 加 check_dir=False (starlette 官方参数: 目录不存在时不炸,
请求时才 404 —— 生产行为不变, 本地 output/ 存在)。
"""
from pathlib import Path


def test_staticfiles_check_dir_false_容忍目录不存在(tmp_path):
    """starlette 语义锁: check_dir=False 时目录不存在不抛。"""
    from starlette.staticfiles import StaticFiles
    StaticFiles(directory=str(tmp_path / "不存在"), check_dir=False)


def test_server_output_mount_带check_dir_false():
    """server.py 的 /output mount 必须带 check_dir=False。"""
    src = Path("server.py").read_text(encoding="utf-8")
    lines = [l for l in src.splitlines() if 'app.mount("/output"' in l]
    assert lines, "server.py 里没找到 /output 的 mount 行 (结构变了要同步本测试)"
    assert "check_dir=False" in lines[0], (
        "/output mount 缺 check_dir=False —— output/ 被 gitignore, "
        "CI checkout 后目录不存在, starlette 默认 check_dir=True 会在 "
        "收集期炸 RuntimeError")
