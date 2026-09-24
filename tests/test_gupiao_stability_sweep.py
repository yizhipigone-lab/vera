"""gupiao_stability_sweep 驱动单元测试 (2026-08-20, red-green 先写红)。

锁定两个口径/两窗/加宽网格的关键不变量:
- 网格加宽到 625 (含 -15% 硬止损 / 15% 激活 / 3% 回撤 / 60 天时间止损)
- stop_config = 止损优先 + 移动止盈条件单语义(real)
- mode→(entry_price_mode, filter_limit_up) 映射: close_t 走 T 日涨停过滤,
  open_t1 不预过滤(交给 T+1 一字板判定), 防两口径串味
"""
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "research"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import gupiao_stability_sweep as gs  # noqa: E402


def _combo():
    return {"cost": -0.08, "act": 0.05, "dd": 0.015, "ladder": "off",
            "levels": [], "time_days": 20, "cond_days": 0, "cond_profit": 0.0}


def test_grid_widened_625():
    g = gs.gen_grid()
    assert len(g) == 625
    assert min(c["cost"] for c in g) == -0.15
    assert max(c["cost"] for c in g) == -0.06
    assert max(c["act"] for c in g) == 0.15
    assert max(c["dd"] for c in g) == 0.03
    assert max(c["time_days"] for c in g) == 60
    assert all(c["ladder"] == "off" and not c["levels"] for c in g)
    assert all(c["cond_days"] == 0 for c in g)


def test_stop_config_stop_first_and_real():
    cfg = gs.stop_config(_combo())
    assert cfg["priority"] == "stop_first"
    assert cfg["trailing_stop"]["enabled"] is True
    assert cfg["trailing_stop"]["confirm"] == "real"
    assert cfg["ladder_tp"]["enabled"] is False
    assert cfg["cost_stop"]["enabled"] is True


def test_mode_run_kwargs_close_t_filters():
    assert gs.mode_run_kwargs("close_t") == {
        "entry_price_mode": "close_t", "filter_limit_up": True}


def test_mode_run_kwargs_open_t1_no_filter():
    assert gs.mode_run_kwargs("open_t1") == {
        "entry_price_mode": "open_t1", "filter_limit_up": False}


def test_mode_run_kwargs_invalid():
    import pytest
    with pytest.raises(ValueError):
        gs.mode_run_kwargs("bogus")
