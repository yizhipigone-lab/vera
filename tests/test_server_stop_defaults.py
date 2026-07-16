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
