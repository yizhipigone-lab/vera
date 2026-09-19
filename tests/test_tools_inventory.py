# -*- coding: utf-8 -*-
"""tests/test_tools_inventory.py — tools/ 三级分类工具 (批次 3.4, 2026-09-19)。

用临时目录构造"生产引用 / 仅文档引用 / 零引用"三种情形, 锁分类判据。
"""
from __future__ import annotations

from pathlib import Path

from tools.tools_inventory import classify


def _mk(root: Path, rel: str, text: str = "x") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_classify_three_levels(tmp_path):
    _mk(tmp_path, "tools/used_by_prod.py")
    _mk(tmp_path, "tools/used_by_docs.py")
    _mk(tmp_path, "tools/nobody.py")
    _mk(tmp_path, "tools/__init__.py")          # 不算脚本
    # 生产引用: 测试文件里出现文件名
    _mk(tmp_path, "tests/test_x.py", "from tools.used_by_prod import f\n")
    # 文档引用: research 报告里提到
    _mk(tmp_path, "research/report.md", "跑 tools/used_by_docs.py 得到结论\n")

    res = classify(tmp_path)
    levels = res["levels"]
    assert res["total"] == 3, " __init__.py 不该计入"
    assert levels["production"] == ["tools/used_by_prod.py"]
    assert levels["docs_only"] == ["tools/used_by_docs.py"]
    assert levels["orphan"] == ["tools/nobody.py"]
    assert res["counts"] == {"production": 1, "docs_only": 1, "orphan": 1}


def test_self_reference_is_not_a_reference(tmp_path):
    """脚本引用自己 (如自己 docstring 写用法) 不算"被引用"。"""
    _mk(tmp_path, "tools/selfref.py", "用法: python tools/selfref.py\n")
    res = classify(tmp_path)
    assert res["levels"]["orphan"] == ["tools/selfref.py"]


def test_runtime_dirs_are_not_ref_sources(tmp_path):
    """运行时产物目录 (output/data/logs) 里的字符串不算引用 —— 否则
    随便一份报告落盘就把脚本"洗白"成被引用。"""
    _mk(tmp_path, "tools/ghost.py")
    _mk(tmp_path, "output/run.log", "tools/ghost.py\n")
    _mk(tmp_path, "data/some.json", '{"ref": "tools/ghost.py"}')
    res = classify(tmp_path)
    assert res["levels"]["orphan"] == ["tools/ghost.py"]


def test_bat_reference_counts_as_production(tmp_path):
    """.bat 拉起脚本 = 被程序用, 算生产引用 (顶层 .bat 在扫描范围内)。"""
    _mk(tmp_path, "tools/launched.py")
    _mk(tmp_path, "start_thing.bat", "python tools\\launched.py\n")
    res = classify(tmp_path)
    assert res["levels"]["production"] == ["tools/launched.py"]
