"""YAML 配置加载器 — 加载、校验、合并默认值与策略配置。"""
import os
import shutil
import tempfile

import yaml
from pathlib import Path
from typing import Any, Dict, Optional


# 前端保存的用户配置文件（运行时生成，与 default.yaml 合并加载）
_CURRENT_PATH = Path(__file__).resolve().parents[1] / "config" / "current.yaml"
_CURRENT_HEADER = (
    "# VERA 前端保存的用户配置（自动生成，手动改会被下次保存覆盖）\n"
    "# 加载时与 config/default.yaml 深度合并；此处未列字段走默认值。\n"
)


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并，override 中的值覆盖 base 中的同名键。"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def resolve_strategy_yaml(arg: Optional[str] = None,
                          current: Optional[str] = None,
                          fallback: Optional[str] = None) -> str:
    """CLI --strategy-yaml 统一解析(2026-07-19 配置源统一,防页面/实验室两套事实源漂移):
    显式指定 > config/current.yaml(前端保存,存在即用) > config/default.yaml(兜底)。
    2026-07-20 用户拍板: 兜底用 default.yaml(与公式无关的通用基线)。
    current/fallback 可注入(测试用)。"""
    if arg:
        return arg
    cur = Path(current) if current else _CURRENT_PATH
    fb = Path(fallback) if fallback else (
        Path(__file__).resolve().parents[1] / "config" / "default.yaml")
    return str(cur) if cur.exists() else str(fb)


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

    # ====== 前端用户配置（current.yaml）存取 — 2026-07-10 ======
    # 单一覆盖文件模式：每次保存覆盖 current.yaml，覆盖前先【复制】.bak 防误覆盖。
    # path 参数可选（默认 _CURRENT_PATH），单测传 tmp 路径避免污染项目 config/。

    @classmethod
    def current_exists(cls, path: Optional[str] = None) -> bool:
        """current.yaml 是否存在。"""
        target = Path(path) if path else _CURRENT_PATH
        return target.exists()

    @classmethod
    def load_current(cls, path: Optional[str] = None) -> Optional[dict]:
        """读 current.yaml 并与 default.yaml 深度合并。文件不存在返回 None。

        复用 load_strategy 的 default 兜底合并，语义与 strategy_*.yaml 一致。
        """
        target = Path(path) if path else _CURRENT_PATH
        if not target.exists():
            return None
        return cls.load_strategy(str(target), None)

    @classmethod
    def save_current(cls, config: dict, path: Optional[str] = None) -> Path:
        """原子写入 current.yaml，覆盖前先【复制】备份成 .bak。返回落盘路径。

        - shutil.copy2 复制、不是 os.replace 移动：保证写新版失败时 current.yaml
          始终是完整旧版（移动会让原件消失，写失败时文件就没了）。
        - 原子写：同目录 tempfile + os.replace（复刻 formula_exit.py:206-236 范式）。
        - 失败时清理 tmp 并 raise（用户主动保存不可静默丢）。
        """
        target = Path(path) if path else _CURRENT_PATH
        target.parent.mkdir(parents=True, exist_ok=True)

        # 覆盖前备份（复制不移动；.bak 只保留最近一次，非历史栈）
        if target.exists():
            shutil.copy2(target, target.parent / (target.name + ".bak"))

        fd, tmp = tempfile.mkstemp(
            dir=str(target.parent), prefix="." + target.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(_CURRENT_HEADER)
                yaml.safe_dump(
                    config, f,
                    allow_unicode=True, sort_keys=False, default_flow_style=False,
                )
            os.replace(tmp, target)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return target

    @classmethod
    def delete_current(cls, path: Optional[str] = None) -> bool:
        """幂等删除 current.yaml，返回删前是否存在。"""
        target = Path(path) if path else _CURRENT_PATH
        existed = target.exists()
        if existed:
            target.unlink()
        return existed
