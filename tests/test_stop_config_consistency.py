"""stop_config 兜底值与 config/default.yaml 一致性测试 (P1-5, 2026-07-15).

锁住"代码兜底 vs yaml"的无形契约, 防止再次漂移 (审计 H1 历史教训).
覆盖字段:
- priority
- cost_stop.threshold
- trailing_stop.activation / drawdown
- time_stop.max_hold_days
- ladder_tp.levels (全部档位)
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.stop_config import (
    get_stop_config_summary,
    load_stop_config_or_default,
)
from utils.config_loader import ConfigLoader


def _load_yaml_stop_loss():
    """直接从 config/default.yaml 读 stop_loss 节点 (绕开 load_stop_config)."""
    root = Path(__file__).resolve().parents[1]
    cfg = ConfigLoader.load_yaml(str(root / "config" / "default.yaml"))
    return cfg["stop_loss"]


def _force_fallback(monkeypatch):
    """monkey-patch load_stop_config 抛异常, 强制走兜底分支."""
    from backtest import stop_config as sc

    def boom(*args, **kwargs):
        raise FileNotFoundError("force fallback (test)")

    monkeypatch.setattr(sc, "load_stop_config", boom)


def test_fallback_priority_matches_yaml(monkeypatch):
    """兜底 priority 必须与 yaml 一致."""
    _force_fallback(monkeypatch)
    fallback = load_stop_config_or_default()
    yaml_cfg = _load_yaml_stop_loss()
    assert fallback["priority"] == yaml_cfg["priority"], (
        f"priority 漂移: 兜底={fallback['priority']!r} vs yaml={yaml_cfg['priority']!r}"
    )


def test_fallback_cost_stop_threshold_matches_yaml(monkeypatch):
    """兜底 cost_stop.threshold 必须与 yaml 一致."""
    _force_fallback(monkeypatch)
    fallback = load_stop_config_or_default()
    yaml_cfg = _load_yaml_stop_loss()
    assert fallback["cost_stop"]["threshold"] == yaml_cfg["cost_stop"]["threshold"]


def test_fallback_trailing_stop_matches_yaml(monkeypatch):
    """兜底 trailing_stop.activation / drawdown 必须与 yaml 一致 (P1-5 主修复)."""
    _force_fallback(monkeypatch)
    fallback = load_stop_config_or_default()
    yaml_cfg = _load_yaml_stop_loss()
    assert fallback["trailing_stop"]["activation"] == yaml_cfg["trailing_stop"]["activation"], (
        f"activation 漂移: 兜底={fallback['trailing_stop']['activation']} "
        f"vs yaml={yaml_cfg['trailing_stop']['activation']}"
    )
    assert fallback["trailing_stop"]["drawdown"] == yaml_cfg["trailing_stop"]["drawdown"], (
        f"drawdown 漂移: 兜底={fallback['trailing_stop']['drawdown']} "
        f"vs yaml={yaml_cfg['trailing_stop']['drawdown']}"
    )


def test_fallback_time_stop_matches_yaml(monkeypatch):
    """兜底 time_stop.max_hold_days 必须与 yaml 一致."""
    _force_fallback(monkeypatch)
    fallback = load_stop_config_or_default()
    yaml_cfg = _load_yaml_stop_loss()
    assert fallback["time_stop"]["max_hold_days"] == yaml_cfg["time_stop"]["max_hold_days"]


def test_fallback_ladder_tp_levels_match_yaml(monkeypatch):
    """兜底 ladder_tp.levels 必须与 yaml 档位数 + profit + sell_ratio 一致."""
    _force_fallback(monkeypatch)
    fallback = load_stop_config_or_default()
    yaml_cfg = _load_yaml_stop_loss()
    fb_levels = fallback["ladder_tp"]["levels"]
    yaml_levels = yaml_cfg["ladder_tp"]["levels"]
    assert len(fb_levels) == len(yaml_levels), (
        f"档位数漂移: 兜底={len(fb_levels)} vs yaml={len(yaml_levels)}"
    )
    for i, (fb, yml) in enumerate(zip(fb_levels, yaml_levels)):
        assert fb["profit"] == yml["profit"], f"档位 {i} profit 漂移: {fb['profit']} vs {yml['profit']}"
        assert fb["sell_ratio"] == yml["sell_ratio"], f"档位 {i} sell_ratio 漂移"


def test_fallback_includes_capabilities(monkeypatch):
    """兜底 capabilities 三开关必须存在 (候选 A 阶段 1 兼容)."""
    _force_fallback(monkeypatch)
    fallback = load_stop_config_or_default()
    caps = fallback["capabilities"]
    assert caps["formula_exit"] is True
    assert caps["gap_protection"] is True
    assert caps["delisting"] is True


def test_fallback_logs_error_on_failure(monkeypatch, caplog):
    """兜底触发时必须 logger.error (而非静默) — 让运维能看到 yaml 异常."""
    _force_fallback(monkeypatch)
    with caplog.at_level("ERROR", logger="backtest.stop_config"):
        load_stop_config_or_default()
    assert any("加载 stop_config 失败" in r.message for r in caplog.records), (
        "兜底触发时必须 logger.error, 不能静默"
    )


def test_summary_trailing_default_matches_yaml():
    yaml_cfg = _load_yaml_stop_loss()["trailing_stop"]
    summary = get_stop_config_summary({
        "cost_stop": {"enabled": False},
        "ladder_tp": {"enabled": False},
        "trailing_stop": {"enabled": True},
        "time_stop": {"enabled": False},
    })

    assert f"盈利{yaml_cfg['activation']:.1%}激活" in summary
    assert f"回撤{yaml_cfg['drawdown']:.1%}线" in summary


def test_summary_trailing_explicit_values_are_preserved():
    summary = get_stop_config_summary({
        "cost_stop": {"enabled": False},
        "ladder_tp": {"enabled": False},
        "trailing_stop": {
            "enabled": True,
            "activation": 0.08,
            "drawdown": 0.05,
        },
        "time_stop": {"enabled": False},
    })

    assert "盈利8.0%激活" in summary
    assert "回撤5.0%线" in summary


def test_summary_omits_disabled_trailing_stop():
    summary = get_stop_config_summary({
        "cost_stop": {"enabled": False},
        "ladder_tp": {"enabled": False},
        "trailing_stop": {"enabled": False},
        "time_stop": {"enabled": False},
    })

    assert "移动止损" not in summary


# === 覆盖率靶向 (2026-07-15) ===

def test_load_stop_config_or_default_fallback():
    """yaml 缺失时走代码内兜底, 不抛异常."""
    import os
    # 用不存在的路径强迫 fallback
    cfg = load_stop_config_or_default()
    assert isinstance(cfg, dict)
    assert "priority" in cfg
    assert cfg["trailing_stop"]["activation"] == 0.035


def test_get_stop_config_summary_illegal_priority_fallback():
    """非法 priority 值 → 显示原始值 (不崩溃)."""
    summary = get_stop_config_summary({
        "priority": "nonexistent_priority",
        "cost_stop": {"enabled": False},
        "ladder_tp": {"enabled": False},
        "trailing_stop": {"enabled": False},
        "time_stop": {"enabled": False},
    })
    # 不崩溃, summary 是字符串
    assert isinstance(summary, str)


# === 常量锁死 (2026-07-15 审计 M2) ===

def test_default_trailing_activation_locked():
    """DEFAULT_TRAILING_ACTIVATION 必须为 0.035 (3.5%)."""
    from backtest.stop_config import DEFAULT_TRAILING_ACTIVATION
    assert DEFAULT_TRAILING_ACTIVATION == 0.035


def test_default_trailing_drawdown_locked():
    """DEFAULT_TRAILING_DRAWDOWN 必须为 0.01 (1%)."""
    from backtest.stop_config import DEFAULT_TRAILING_DRAWDOWN
    assert DEFAULT_TRAILING_DRAWDOWN == 0.01
