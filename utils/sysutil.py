"""utils/sysutil.py — 小型系统工具：项目根路径 / 控制台编码 / 按路径动态加载模块。

收敛全仓散布的同款小轮子（brain/、notes_gen/、evolution/ 等各有手写副本）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def project_root() -> Path:
    """VERA 项目根目录（utils/ 的上一级）。"""
    return Path(__file__).resolve().parent.parent


def ensure_utf8_stdout() -> None:
    """Windows GBK 控制台打印 emoji/特殊字符防炸（brain/__main__.py 先例）。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_json_or_empty(path: Path) -> dict:
    """读 JSON 文件为 dict；不存在/损坏/任何异常返 {}（brain 松耦合惯例）。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_module_from_path(name: str, path: Path):
    """按文件路径动态加载模块（先注册 sys.modules 再 exec，已加载则复用）。"""
    mod = sys.modules.get(name)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # 先注册再 exec，支持模块内自引用
    spec.loader.exec_module(mod)
    return mod


def close_subprocess_pipes(proc) -> None:
    """显式关闭 asyncio 子进程的 stdin/stdout/stderr 管道 transport。

    Windows Proactor 事件循环下, create_subprocess_exec 的管道 transport 若未显式
    close 就随 asyncio.run() 一起销毁, 垃圾回收时 _ProactorBasePipeTransport.__del__
    会去读已关闭的管道 (fileno 抛 ValueError), 刷屏无害但很脏的
    "Exception ignored ... I/O operation on closed pipe"。逐个 close() 后 __del__
    见 _sock=None 直接跳过, 噪音消失。brain/sentiment_judge、eval_judge、claude_cli
    三处同款子进程模式共用 (2026-08-17)。
    """
    for stream in (getattr(proc, "stdin", None),
                   getattr(proc, "stdout", None),
                   getattr(proc, "stderr", None)):
        if stream is None:
            continue
        transport = getattr(stream, "_transport", None)
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
