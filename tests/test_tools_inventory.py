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


# ---------------------------------------------------------------------------
# 2026-09-20 审计 P2-9: 三类系统误判 (实测把在用的脚本列进了"孤儿")
# ---------------------------------------------------------------------------

def test_nested_bat_reference_counts_as_production(tmp_path):
    """`.bat` 必须**递归**扫 (原先只扫仓库顶层)。

    实测: `tools/gs_top10_prep_parallel.py` 只被 `tools/gs_launch_prep.bat` 引用,
    旧判据把它列进孤儿名单。
    """
    _mk(tmp_path, "tools/gs_top10_prep_parallel.py")
    _mk(tmp_path, "tools/gs_launch_prep.bat",
        "python tools\\gs_top10_prep_parallel.py\n")
    res = classify(tmp_path)
    assert res["levels"]["production"] == ["tools/gs_top10_prep_parallel.py"]


def test_bat_under_data_counts_as_production(tmp_path):
    """data/ 里的批量启动器也是真引用来源 (原先 data/ 被整体跳过)。"""
    _mk(tmp_path, "tools/farmed.py")
    _mk(tmp_path, "data/formula_farm/runs/run_1.bat",
        "python tools\\farmed.py\n")
    res = classify(tmp_path)
    assert res["levels"]["production"] == ["tools/farmed.py"]
    # 但 data/ 里的普通产物文本仍不算引用 (防"随便落盘一份报告就洗白")
    _mk(tmp_path, "tools/ghost.py")
    _mk(tmp_path, "data/report.json", '{"ref": "tools/ghost.py"}')
    res = classify(tmp_path)
    assert "tools/ghost.py" in res["levels"]["orphan"]


def test_tool_to_tool_reference_counts_as_production(tmp_path):
    """工具链互引 (tools/ 自己也是引用来源) 算生产引用。

    实测: `tools/formula_pipeline/verify_parse.py` 被同目录 run_pipeline.py 调用,
    旧判据 (PROD_DIRS 不含 tools) 把它列进孤儿名单。
    """
    _mk(tmp_path, "tools/formula_pipeline/verify_parse.py")
    _mk(tmp_path, "tools/formula_pipeline/run_pipeline.py",
        "import verify_parse\n")
    res = classify(tmp_path)
    assert res["levels"]["production"] == [
        "tools/formula_pipeline/verify_parse.py"]


def test_root_markdown_mention_is_docs_not_production(tmp_path):
    """根级 .md (CHANGELOG/README) 提到一句**只能算文档引用**, 不是生产引用。

    实测: `tools/quantqq_5m_sweep_2010.py` 就是这样被"洗白"成生产引用的 ——
    它其实只是被 CHANGELOG 记了一笔。
    """
    _mk(tmp_path, "tools/only_in_changelog.py")
    _mk(tmp_path, "CHANGELOG.md", "改了 tools/only_in_changelog.py\n")
    res = classify(tmp_path)
    assert res["levels"]["production"] == []
    assert res["levels"]["docs_only"] == ["tools/only_in_changelog.py"]
