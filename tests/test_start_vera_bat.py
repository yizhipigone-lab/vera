# -*- coding: utf-8 -*-
"""tests/test_start_vera_bat.py — 一键启动脚本的结构与行为回归 (2026-09-20 审计 P0-1)。

背景: 2026-09-19 加的"等 QMT 就绪"块里 echo 带未转义圆括号, cmd **解析期**即中止
整个脚本 —— 交易与调度都不启动, 而当时只做了字节级检查 (GBK/CRLF) 与探针单跑,
没真跑过这个 bat, 于是漏了。本测试把"真跑一遍(桩化)"固化成机器判据:

  ① 字节不变量: GBK 可解码 / CRLF / 无 BOM / 无裸 LF;
  ② 结构不变量: `goto :X` 的目标标签都存在; 括号块内的 echo/rem 不带裸括号;
  ③ 行为 (三场景桩化执行): 探针 0=就绪 / 1=未就绪(重试) / 2=配置错误 ——
     三种都要**跑到 [3/3] 且调度桩起来**; 只有就绪场景才启动交易;
     绝不允许出现 "was unexpected at this time" 或 "cannot find the batch label"。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BAT = ROOT / "start_vera.bat"
PROBE_LINE = ('  "%PYDIR%\\python.exe" tools\\qmt_ready_check.py '
              '--config config\\trade.yaml 2>nul')
TRADE_STUB = '[STUB-START] start "VERA-Trade-8081"'
SCHED_STUB = '[STUB-START] start "VERA-Scheduler"'


@pytest.fixture(scope="module")
def bat_text() -> str:
    raw = BAT.read_bytes()
    return raw.decode("gbk")


def test_byte_invariants(bat_text):
    raw = BAT.read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n"), "存在裸 LF (cmd 会解析错乱)"
    assert not raw.startswith(b"\xef\xbb\xbf"), "不许有 UTF-8 BOM"
    assert "chcp 65001" in bat_text          # 子窗口中文日志
    assert "qmt_ready_check.py" in bat_text  # 探针仍接线


def test_labels_and_parens(bat_text):
    labels = set(re.findall(r"^\s*:([a-z_]+)", bat_text, re.M))
    gotos = set(re.findall(r"goto :([a-z_]+)", bat_text))
    assert gotos <= labels, f"goto 指向不存在的标签: {sorted(gotos - labels)}"
    # 括号块内的 echo/rem 不许出现裸圆括号 (P0-1 的病根)
    depth, bad = 0, []
    for i, ln in enumerate(bat_text.splitlines(), 1):
        s = ln.strip()
        if depth > 0 and s.startswith(("echo", "rem")) and ("(" in ln or ")" in ln):
            bad.append((i, s))
        if not s.startswith(("echo", "rem")):
            depth += ln.count("(") - ln.count(")")
            assert depth >= 0, f"第 {i} 行括号不配平: {s}"
    assert not bad, f"块内 echo/rem 带裸括号 (会截断解析): {bad}"


def _stub_bat(scenario: str) -> pathlib.Path:
    """生成桩化 bat: 假 python.exe + 可注入退出码的探针桩 + start/pause 桩。"""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="vera_bat_test_"))
    (tmp / "python.exe").write_bytes(b"")
    probe = ("@echo off\r\n"
             + ("if exist \"%~dp0.count\" (exit /b 0)\r\n"
                "echo x> \"%~dp0.count\"\r\n"
                "exit /b 1\r\n" if scenario == "retry" else
                f"exit /b {0 if scenario == 'ready' else 2}\r\n"))
    (tmp / "probe_stub.bat").write_bytes(probe.encode("gbk"))

    src = BAT.read_bytes().decode("gbk")
    lines = []
    for ln in src.splitlines():
        s = ln.strip().lower()
        if s.startswith("start "):
            lines.append("echo [STUB-START] " + ln.strip())
        elif s.startswith("pause"):
            lines.append("echo [STUB-PAUSE]")
        elif s.startswith("if exist") and "dsh-runtime" in s:
            lines.append("echo [STUB-DSH]")
        elif s.startswith('set "pydir='):
            lines.append(f'set "PYDIR={tmp}"')
        elif ln.strip() == PROBE_LINE.strip():
            lines.append('  call "%PYDIR%\\probe_stub.bat"')
        elif "timeout /t 20" in s:
            lines.append("rem (skipped: 测试不真等 20 秒)")
        else:
            lines.append(ln)
    bat = tmp / "start_stub.bat"
    bat.write_bytes(("\r\n".join(lines) + "\r\n").encode("gbk"))
    return bat


@pytest.mark.parametrize("scenario,want_trade", [
    ("ready", True), ("retry", True), ("config_error", False)])
def test_stubbed_execution(scenario, want_trade):
    bat = _stub_bat(scenario)
    r = subprocess.run(["cmd", "/c", str(bat)], cwd=str(bat.parent),
                       capture_output=True)
    out = r.stdout.decode("gbk", "replace")
    err = r.stderr.decode("gbk", "replace")
    both = out + err
    assert "was unexpected at this time" not in both, f"解析期中止:\n{out[-400:]}"
    assert "cannot find the batch label" not in both, f"标签丢失:\n{both[-400:]}"
    assert "[3/3]" in out, f"没跑到 [3/3] (脚本被中断):\n{out[-400:]}"
    assert SCHED_STUB in out, "调度进程桩没起来"
    assert (TRADE_STUB in out) is want_trade, (
        f"{scenario} 场景交易进程启动与否不符合预期")
    if scenario == "config_error":
        assert "CONFIG-ERROR" in out, "配置错误没有走专门分支"
