"""trade/analysis.py 纯函数单测 (2026-08-19 深模块治理)。

按计划书 Task 3: 先写失败测试 (模块尚不存在 → ModuleNotFoundError),
再写实现, 最后确认 PASS。
"""
import datetime

import pytest

from trade.analysis import calc_drawdowns, deep_merge, diff_dicts, hold_days


def test_calc_drawdowns():
    """从净值序列算逐日回撤 (peak 为历史最高)。"""
    assert calc_drawdowns([100.0, 110.0, 99.0]) == pytest.approx(
        [0.0, 0.0, 99.0 / 110.0 - 1.0])
    assert calc_drawdowns([]) == []
    assert calc_drawdowns([100.0]) == [0.0]


def test_diff_dicts_returns_dotted_paths():
    old = {"a": 1, "b": {"c": 2, "d": 3}, "e": 5}
    new = {"a": 1, "b": {"c": 2, "d": 4}, "f": 6}
    changed = diff_dicts(old, new)
    assert "b.d" in changed
    assert "e" in changed and "f" in changed
    assert "a" not in changed and "b.c" not in changed


def test_deep_merge_replaces_non_dict():
    base = {"a": 1, "b": {"c": 2}, "levels": [1, 2]}
    override = {"b": {"d": 3}, "levels": [9]}
    out = deep_merge(base, override)
    assert out["a"] == 1
    assert out["b"] == {"c": 2, "d": 3}   # dict 递归合并
    assert out["levels"] == [9]           # 非 dict 整体替换


def test_hold_days_with_fixed_calendar(monkeypatch):
    import trade.analysis as an
    # 日历 = 8/10(一)、8/11(二)、8/12(三) 三天
    monkeypatch.setattr(an, "_trading_days",
                        lambda: ["20260810", "20260811", "20260812"])
    entry = datetime.datetime(2026, 8, 10).timestamp()
    # 8/10 买入, 算到 8/12 → 2 天 (8/11, 8/12)
    assert hold_days(entry, datetime.datetime(2026, 8, 12).timestamp()) == 2
    assert hold_days(None) is None
