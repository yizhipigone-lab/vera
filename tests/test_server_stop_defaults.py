"""Web 请求模型的移动止损止盈默认值契约。"""

from server import StrategyConfig, _config_to_yaml_dict
from utils.config_loader import ConfigLoader


def _yaml_trailing_defaults() -> dict:
    return ConfigLoader.load_defaults()["stop_loss"]["trailing_stop"]


def _request_trailing_config(**overrides) -> dict:
    cfg = StrategyConfig(**overrides)
    return _config_to_yaml_dict(cfg)["stop_loss"]["trailing_stop"]


def test_request_defaults_match_default_yaml():
    expected = _yaml_trailing_defaults()
    actual = _request_trailing_config()
    assert actual["activation"] == expected["activation"] == 0.035
    assert actual["drawdown"] == expected["drawdown"] == 0.01


def test_explicit_none_uses_default_yaml_values():
    expected = _yaml_trailing_defaults()
    actual = _request_trailing_config(
        trailing_activation=None,
        trailing_drawdown=None,
    )
    assert actual["activation"] == expected["activation"]
    assert actual["drawdown"] == expected["drawdown"]


def test_explicit_legacy_values_are_preserved():
    actual = _request_trailing_config(
        trailing_activation=0.08,
        trailing_drawdown=0.05,
    )
    assert actual["activation"] == 0.08
    assert actual["drawdown"] == 0.05


def test_explicit_zero_values_are_preserved():
    actual = _request_trailing_config(
        trailing_activation=0.0,
        trailing_drawdown=0.0,
    )
    assert actual["activation"] == 0.0
    assert actual["drawdown"] == 0.0


# ── ladder_levels 单位归一化 (2026-08-06 审计 HIGH#5) ──
# 默认值 "6:30,15:30" 是百分数, engine 期望小数 (stop_config.py:52);
# 修复前解析成 profit=6.0 (=600%) 永远达不到 → 阶梯止盈静默失效。
import pytest


def _ladder_levels(**overrides):
    cfg = StrategyConfig(**overrides)
    return _config_to_yaml_dict(cfg)["stop_loss"]["ladder_tp"]["levels"]


def test_ladder_default_percent_normalized():
    """默认百分数字符串 "6:30,15:30" → 小数 0.06/0.30, 0.15/0.30。"""
    levels = _ladder_levels()
    assert len(levels) == 2
    assert levels[0]["profit"] == pytest.approx(0.06)
    assert levels[0]["sell_ratio"] == pytest.approx(0.30)
    assert levels[1]["profit"] == pytest.approx(0.15)


def test_ladder_decimal_passthrough():
    """直调 API/yaml 传小数格式 → 原样保留, 不被误 /100。"""
    levels = _ladder_levels(ladder_levels="0.08:0.5,0.20:0.5")
    assert levels[0]["profit"] == pytest.approx(0.08)
    assert levels[0]["sell_ratio"] == pytest.approx(0.5)


def test_ladder_boundary_one_stays_decimal():
    """边界: profit/ratio 恰为 1 (=100%) 视为小数不 /100;
    百分数 100 → 1.0。ratio=1.0 即清仓档语义, 不得破坏。"""
    levels = _ladder_levels(ladder_levels="1:1")
    assert levels[0]["profit"] == pytest.approx(1.0)
    assert levels[0]["sell_ratio"] == pytest.approx(1.0)
    levels = _ladder_levels(ladder_levels="100:100")
    assert levels[0]["profit"] == pytest.approx(1.0)
    assert levels[0]["sell_ratio"] == pytest.approx(1.0)
