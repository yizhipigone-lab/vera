# -*- coding: utf-8 -*-
"""配置源统一 — resolve_strategy_yaml 解析顺序测试(2026-07-19)"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.config_loader import resolve_strategy_yaml  # noqa: E402


def test_explicit_arg_wins(tmp_path):
    cur = tmp_path / "current.yaml"
    cur.write_text("a: 1")
    assert resolve_strategy_yaml("explicit.yaml", str(cur), "fb.yaml") == "explicit.yaml"


def test_current_yaml_preferred_over_fallback(tmp_path):
    cur = tmp_path / "current.yaml"
    cur.write_text("a: 1")
    assert resolve_strategy_yaml(None, str(cur), "fb.yaml") == str(cur)


def test_fallback_when_no_current(tmp_path):
    cur = tmp_path / "nonexistent.yaml"
    assert resolve_strategy_yaml(None, str(cur), "fb.yaml") == "fb.yaml"


def test_default_resolution_points_at_repo_files():
    # 不传注入路径时, 结果必须是 config/ 下的策略文件之一
    out = resolve_strategy_yaml(None)
    assert out.endswith(".yaml") and "config" in out.replace("\\", "/")
