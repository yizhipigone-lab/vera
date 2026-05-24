"""YAML 配置加载器 — 加载、校验、合并默认值与策略配置。"""
import yaml
from pathlib import Path
from typing import Any, Dict, Optional


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并，override 中的值覆盖 base 中的同名键。"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class ConfigLoader:
    """加载 YAML 配置文件，支持默认值 + 策略覆盖合并。"""

    @staticmethod
    def load_yaml(path: str) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data is None:
            data = {}
        return data

    @classmethod
    def load_strategy(cls, strategy_path: str, default_path: Optional[str] = None) -> dict:
        """
        加载策略配置，自动合并默认值。

        Args:
            strategy_path: 策略 YAML 文件路径
            default_path: 默认配置路径，默认为 config/default.yaml
        Returns:
            合并后的完整配置字典
        """
        if default_path is None:
            default_path = str(Path(__file__).resolve().parents[1] / "config" / "default.yaml")

        defaults = cls.load_yaml(default_path)
        strategy = cls.load_yaml(strategy_path)
        return _deep_merge(defaults, strategy)

    @classmethod
    def load_defaults(cls) -> dict:
        default_path = str(Path(__file__).resolve().parents[1] / "config" / "default.yaml")
        return cls.load_yaml(default_path)

    @staticmethod
    def validate_stop_config(config: dict) -> list[str]:
        """校验止损止盈配置的合法性，返回警告列表。"""
        warnings = []
        stop = config.get("stop_loss", {})

        ladder = stop.get("ladder_tp", {})
        if ladder.get("enabled"):
            levels = ladder.get("levels", [])
            total_ratio = sum(lv.get("sell_ratio", 0) for lv in levels)
            if abs(total_ratio - 1.0) > 0.01:
                warnings.append(f"阶梯止盈卖出比例合计为 {total_ratio:.0%}，建议为 100%")
            profits = [lv.get("profit", 0) for lv in levels]
            if profits != sorted(profits):
                warnings.append("阶梯止盈档位建议按盈利比例升序排列")

        return warnings
