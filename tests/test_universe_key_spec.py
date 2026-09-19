# -*- coding: utf-8 -*-
"""tests/test_universe_key_spec.py — universe 缺省 True 键的声明/实现一致性
(2026-09-19 架构修订批次 3.3)。

背景: 缓存 key 归一化要知道"哪些 universe 键缺省为 True"(它们的显式 False 与
缺键语义相反, 不能当假值剔掉 —— 2026-09-16 审计 P0-1: 两种池子撞同一 key,
回测池语义被偷换)。旧实现是**缓存层手工维护名单** → selector 新增同类键时
缓存层不知道, P0-1 复发。

现在名单唯一声明在 selector.UNIVERSE_TRUE_DEFAULT_KEYS。本测试用 AST 扫描
selector 里真实的 `xxx.get("<key>", True)` 调用, 断言两边一致 —— 有人新增
缺省 True 的 universe 键却忘了登记, 这里立刻红。
"""
from __future__ import annotations

import ast
from pathlib import Path

from selection.selector import UNIVERSE_TRUE_DEFAULT_KEYS

SELECTOR = Path(__file__).resolve().parents[1] / "selection" / "selector.py"


def _true_default_keys_in_code() -> set[str]:
    """扫描 selector.py 里 `.get("<key>", True)` 形式的键名 (AST, 不靠正则)。"""
    tree = ast.parse(SELECTOR.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "get":
            continue
        if len(node.args) != 2:
            continue
        key, default = node.args
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            continue
        if not (isinstance(default, ast.Constant) and default.value is True):
            continue
        # 只认 universe 配置的取值 (变量名 u / universe / universe_config 等)
        base = func.value
        name = getattr(base, "id", None) or getattr(base, "attr", None) or ""
        if "universe" in name or name == "u":
            found.add(key.value)
    return found


def test_declared_true_default_keys_match_code():
    """声明 == 实现 (新增缺省 True 键必须登记, 否则缓存 key 会撞车)。"""
    actual = _true_default_keys_in_code()
    assert actual == set(UNIVERSE_TRUE_DEFAULT_KEYS), (
        f"selector 里的缺省 True 键 {sorted(actual)} 与声明的 "
        f"{sorted(UNIVERSE_TRUE_DEFAULT_KEYS)} 不一致 —— 缓存层靠这份声明"
        "区分'显式 False'与'缺键', 漏登记会让两种池子共用一份缓存 (P0-1)")
    assert actual, "扫描不到任何缺省 True 键: 扫描逻辑或 selector 写法变了, 需同步"


def test_explicit_false_survives_normalization():
    """行为锁: 缺省 True 键的显式 False 保留在 key 里, True/缺键被剔除。"""
    from selection.selection_cache import normalize_universe
    with_false = normalize_universe({"type": "50", "exclude_quit": False})
    with_true = normalize_universe({"type": "50", "exclude_quit": True})
    without = normalize_universe({"type": "50"})
    assert with_false.get("exclude_quit") is False      # 显式 False 保留
    assert "exclude_quit" not in with_true              # 缺省值 = 缺键
    assert with_true == without                         # 两入口 key 收敛
