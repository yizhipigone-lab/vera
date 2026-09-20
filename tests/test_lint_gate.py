# -*- coding: utf-8 -*-
"""tests/test_lint_gate.py — CI 门禁常驻断言 (2026-09-20 脆弱期 P0 修复 item 7)。

为什么要有这个文件：
CI 的 lint 步骤 (`ruff check . --select F821,F822,F823`) 从 2026-09-04 起就是红的
(GitHub Actions run #19/#20/#25/#26 全 failure), 红了半个多月没人看 —— 期间
0561824 还把一个真 bug (trade_main.py:906 F821 Undefined name `logger`) 提交了
进去。**一个一直红的门禁比没有门禁更糟**: 它训练人忽略它, 然后它就不再保护
任何东西。本测试把这条门禁搬进 pytest: 本地跑全量时它一起跑, 红即是门禁红。

ruff 不在环境时 skip (CI 的 Install deps 步骤显式装 ruff, 不会 skip)。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# 与 .github/workflows/ci.yml 的 Lint (correctness gate) 步骤**逐字一致**,
# 改一边必须改另一边 (本测试就是防"门禁配置与本地行为漂移"的锁)。
GATE_ARGS = ["-m", "ruff", "check", ".", "--select", "F821,F822,F823"]


def _ruff_available() -> bool:
    try:
        subprocess.run([sys.executable, "-m", "ruff", "--version"],
                       capture_output=True, timeout=30)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _ruff_available(), reason="ruff 未安装")
def test_lint_gate_f821_f822_f823():
    """F821/F822/F823 (未定义名/未定义导出) 必须零违规 —— 与 CI 门禁同一命令。"""
    r = subprocess.run([sys.executable, *GATE_ARGS],
                       cwd=ROOT, capture_output=True, timeout=300,
                       # Windows 默认 locale 是 GBK, ruff 输出含 UTF-8 中文
                       # (如"未定义名"), 不显式指定会在解码期炸成
                       # UnicodeDecodeError —— 那是测试的错, 不是门禁的红。
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, (
        f"lint 门禁红 (exit={r.returncode})。这 3 条规则只查'名字不存在'级错误, "
        f"没有风格噪音, 没有借口:\n{r.stdout}\n{r.stderr}")
