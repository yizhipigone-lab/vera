# -*- coding: utf-8 -*-
"""前端配置模型 + YAML 映射 (2026-08-01 批次5 C4c 从 server.py 抽出, 纯移动不改行为)。

- StrategyConfig: /api/run 与 /api/config/* 共用的请求模型 (extra="allow" 透传前端扩展字段)
- _config_to_yaml_dict: 前端配置 → 策略 YAML dict (Pipeline 临时 yaml 的输入)
"""
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict

from backtest.stop_config import (
    DEFAULT_TRAILING_ACTIVATION,
    DEFAULT_TRAILING_DRAWDOWN,
)

# StrategyConfig.get 的兜底默认值 (None 时回退), 模块级常量避免每次调用重建
_GET_DEFAULTS = {
    "initial_capital": 1000000.0, "commission": 0.0003, "slippage": 0.001,
    "min_buy_amount": 2000.0, "max_buy_amount": 20000.0,
    "lot_size": 100, "min_lots": 1,
    "cost_stop_threshold": -0.12, "trailing_activation": DEFAULT_TRAILING_ACTIVATION,
    "trailing_drawdown": DEFAULT_TRAILING_DRAWDOWN, "max_hold_days": 20,
    "cond_time_days": 7, "cond_time_profit": 0.02,
    "first_day_target": 0.03,
}


class StrategyConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    strategy_name: str = ""
    formula_name: str = "UPN"
    formula_arg: str = "3"
    universe_type: str = "50"
    exclude_st: bool = True
    start_time: str = "20240101"
    end_time: str = "20250630"
    period: str = "1d"
    dividend_type: int = 1
    initial_capital: Optional[float] = None
    commission: Optional[float] = None
    slippage: Optional[float] = None
    max_positions: int = 999
    min_buy_amount: Optional[float] = None
    max_buy_amount: Optional[float] = None
    lot_size: Optional[int] = None
    min_lots: Optional[int] = None
    cost_stop_enabled: bool = True
    cost_stop_threshold: Optional[float] = None
    trailing_enabled: bool = True
    trailing_activation: Optional[float] = None
    trailing_drawdown: Optional[float] = None
    ladder_enabled: bool = True
    ladder_levels: str = "6:30,15:30"
    time_enabled: bool = True
    max_hold_days: Optional[int] = None
    cond_time_enabled: bool = False
    cond_time_days: Optional[int] = None
    cond_time_profit: Optional[float] = None
    first_day_enabled: bool = False
    first_day_target: Optional[float] = None
    benchmark_indices: str = "shanghai,hs300,zz500,chuangyeban,kechuang50,zhongzhengA500"
    # P-v3.4: ETF 开关 (混合选股 / 仅ETF)
    include_etf: bool = False
    etf_only: bool = False
    # P-v3.4: 行业板块 (逗号分隔代码, 如 "881319.SH,881326.SH")
    sectors: str = ""
    # 2026-07-19: 因子过滤(按公式存终审规则勾选状态)
    # 形如 {"QUANTQQ": {"enabled": true, "rules": ["dist_ma20:top10"]}}
    factor_filter: Optional[Dict[str, Any]] = None

    def get(self, key: str, default=None):
        """安全获取字段值，None 时返回默认值。"""
        val = getattr(self, key, None)
        if val is None:
            return _GET_DEFAULTS.get(key, default)
        return val


def _config_to_yaml_dict(cfg: StrategyConfig) -> dict:
    """将前端配置转为策略 YAML 字典。"""
    # 解析阶梯止盈
    ladder_levels = []
    if cfg.ladder_enabled and cfg.ladder_levels:
        for item in cfg.ladder_levels.split(","):
            parts = item.strip().split(":")
            if len(parts) == 2:
                ladder_levels.append({
                    "profit": float(parts[0]),
                    "sell_ratio": float(parts[1]),
                })

    # 选股始终用日线，回测层可用5m/1m
    if cfg.period == "5m":
        sel_period, bt_period = "1d", "5m"
    elif cfg.period == "1m":
        sel_period, bt_period = "1d", "1m"
    else:
        sel_period = bt_period = cfg.period

    return {
        "strategy": {"name": cfg.strategy_name or "回测"},
        "selection": {
            "formula_name": cfg.formula_name,
            "formula_arg": cfg.formula_arg,
            "universe": {
                "type": cfg.universe_type,
                "exclude_st": cfg.exclude_st,
                # P-v3.4: ETF 开关
                "include_etf": bool(cfg.include_etf),
                "etf_only": bool(cfg.etf_only),
                # P-v3.4: 行业板块代码列表 (逗号分隔字符串 → list)
                "sectors": [s.strip() for s in cfg.sectors.split(",") if s.strip()],
            },
            "period": sel_period,
            "dividend_type": cfg.dividend_type,
        },
        "time_range": {"start": cfg.start_time, "end": cfg.end_time},
        "backtest": {
            "initial_capital": cfg.get("initial_capital", 1000000.0),
            "commission": cfg.get("commission", 0.0003),
            "slippage": cfg.get("slippage", 0.001),
            "period": bt_period,
            # 2026-07-21 用户决策: web 回测默认开启 5m 数据层降级
            # (没有 5M 线的时段降级为日线, 回测区间完整覆盖请求起点)
            "degrade_5m": bool(getattr(cfg, "degrade_5m", True)),
            "position_sizing": {
                "max_positions": cfg.max_positions,
                "min_buy_amount": cfg.get("min_buy_amount", 2000.0),
                "max_buy_amount": cfg.get("max_buy_amount", 20000.0),
                "lot_size": cfg.get("lot_size", 100),
                "min_lots": cfg.get("min_lots", 1),
            },
        },
        "stop_loss": {
            # 2026-07-05: 优先级开关 (前端 cfgPriority radio 透传, 默认 trailing_first)
            "priority": str(cfg.get("priority", "trailing_first")),
            "cost_stop": {"enabled": cfg.cost_stop_enabled, "threshold": cfg.get("cost_stop_threshold", -0.12)},
            "trailing_stop": {
                "enabled": cfg.trailing_enabled,
                "activation": cfg.get(
                    "trailing_activation", DEFAULT_TRAILING_ACTIVATION
                ),
                "drawdown": cfg.get(
                    "trailing_drawdown", DEFAULT_TRAILING_DRAWDOWN
                ),
            },
            "ladder_tp": {"enabled": cfg.ladder_enabled, "levels": ladder_levels},
            "time_stop": {"enabled": cfg.time_enabled, "max_hold_days": cfg.get("max_hold_days", 20)},
            "cond_time_stop": {"enabled": cfg.cond_time_enabled, "days": cfg.get("cond_time_days", 7), "profit": cfg.get("cond_time_profit")},
            "first_day": {"enabled": cfg.first_day_enabled, "target": cfg.get("first_day_target", 0.03)},
            # P-v3.4: 公式卖出 (formula_sell) — 前端配置透传到 engine.run(stop_config=...)
            "formula_sell": {
                "enabled": bool(cfg.get("formula_sell_enabled", False)),
                "formula_name": str(cfg.get("formula_sell_name", "")),
                "formula_arg": "",
                "sell_ratio": float(cfg.get("formula_sell_ratio", 1.0)),
                "priority": 0,
            },
        },
        "benchmark": {"indices": [s.strip() for s in cfg.benchmark_indices.split(",") if s.strip()]},
        # 2026-07-19: 因子过滤(按公式存; pipeline 在选股后/回测前应用)
        "factor_filter": cfg.factor_filter or {},
    }
