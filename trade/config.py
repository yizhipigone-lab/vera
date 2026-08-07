"""trade/config.py — 实盘交易配置数据定义 (刻意简单)。

设计意图:
    配置只有 frozen dataclass + 一个加载函数, 不做深合并、不做热加载。
    缺字段走默认值, 类型/值域不对 fail-fast —— 实盘系统宁可启动失败,
    不可带着漂移的默认值下单 (回测侧"默认值漂移"事故的教训)。

    2026-07-26 用户裁决: 止盈止损参数结构**复刻回测 stop_loss**
    (config/default.yaml 同形嵌套, 默认值与之一致), 禁止第二份参数定义
    (铁律 4)。priority 合法集引用 backtest.stop_config.VALID_PRIORITIES
    单一真相源, 不自己再写一份。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from backtest.stop_config import VALID_PRIORITIES

# ═══════════════════════════════════════════════════════════════
# 止盈止损段 — 与回测 stop_loss 同形 (用户裁决①: 复刻完整内容)
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class CostStopConfig:
    """成本止损 [backtest/loop/strategies/cost_stop.py]。threshold 负值。"""
    enabled: bool = True
    threshold: float = -0.12


@dataclass(frozen=True)
class TrailingStopConfig:
    """移动止盈 [backtest/loop/strategies/trailing.py]: 峰值涨幅先过
    activation 激活线, 现价跌破 峰值×(1-drawdown) 触发。"""
    enabled: bool = True
    activation: float = 0.035
    drawdown: float = 0.01


@dataclass(frozen=True)
class LadderTpConfig:
    """阶梯止盈 [backtest/loop/strategies/ladder_tp.py]。
    levels 元素 (profit 涨幅, sell_ratio 卖出比例), 加载时按 profit 升序
    归一化 (回测档位 bitmask 语义依赖顺序)。"""
    enabled: bool = True
    levels: tuple = ((0.06, 0.30), (0.15, 0.30))


@dataclass(frozen=True)
class TimeStopConfig:
    """时间止损 [backtest/loop/strategies/time_stop.py]: 到点即走,
    无收益门槛 (2026-07-26 裁决后删除旧 trade 私设的 min_gain)。"""
    enabled: bool = True
    max_hold_days: int = 20


@dataclass(frozen=True)
class CondTimeStopConfig:
    """条件时间止盈 [backtest/loop/strategies/cond_time.py]:
    持仓 ≥ days 且当日最高涨幅 ≥ profit 即清仓。"""
    enabled: bool = False
    days: int = 5
    profit: float = 0.03


@dataclass(frozen=True)
class FirstDayConfig:
    """首日未达标 [backtest/loop/strategies/first_day.py]:
    入场次日 (首个可交易日) 日内最高涨幅 < target 即卖。"""
    enabled: bool = False
    target: float = 0.05


@dataclass(frozen=True)
class StopConfig:
    """止盈止损总段。priority 三档枚举与回测一致
    [backtest/loop/exit_engine.py:22-27]。
    TODO P2: formula_sell (实盘盘中跑公式引擎, 重, 不做)。"""
    priority: str = "trailing_first"
    cost_stop: CostStopConfig = field(default_factory=CostStopConfig)
    trailing_stop: TrailingStopConfig = field(default_factory=TrailingStopConfig)
    ladder_tp: LadderTpConfig = field(default_factory=LadderTpConfig)
    time_stop: TimeStopConfig = field(default_factory=TimeStopConfig)
    cond_time_stop: CondTimeStopConfig = field(default_factory=CondTimeStopConfig)
    first_day: FirstDayConfig = field(default_factory=FirstDayConfig)


@dataclass(frozen=True)
class PositionSizingConfig:
    """买入 sizing 口径, 对齐 config/default.yaml 的 backtest.position_sizing。
    max_positions 是实盘新增 (回测无此字段): 人工买入的持仓数上限,
    替代被砍掉的单票集中度闸 (用户裁决②: 风控做减法)。"""
    min_buy_amount: float = 2000.0
    max_buy_amount: float = 20000.0
    lot_size: int = 100
    max_positions: int = 10


@dataclass(frozen=True)
class AutoBuyConfig:
    """尾盘自动选股买入 (2026-07-27 MVP)。
    enabled 默认 False: 未显式配置不开 (fail-safe), trade.yaml 显式开。
    universe 与回测 selection.universe 同形 (type "50" 等)。"""
    enabled: bool = False
    time: str = "14:52"                    # 每日触发时刻 (HH:MM)
    formula_name: str = "QUANTQQ"
    formula_arg: str = ""
    amount_per_stock: float = 10000.0      # 单票金额上限
    max_buys_per_day: int = 5              # 每日买入笔数上限
    universe: dict = field(default_factory=lambda: {
        "type": "50", "exclude_st": True, "exclude_new_listings_days": 60})


@dataclass(frozen=True)
class FeishuConfig:
    """飞书 webhook 通知 (2026-07-31): enabled=总开关 (设置面板可热关)。
    webhook URL 走环境变量 FEISHU_WEBHOOK_URL, 不入 yaml (半密钥);
    URL 缺失时通知器为 no-op (启动告警一次), 交易照常。
    daily_report_level (2026-08-07): 盘后日报详尽档, full=全明细 / summary=
    简报 (只资产+交易摘要, 不出仓位变动/卖出明细), 设置面板可热切。"""
    enabled: bool = True
    daily_report_level: str = "full"


@dataclass(frozen=True)
class TradeConfig:
    """实盘交易配置。所有字段都有默认值, yaml 只覆写关心的部分。

    force_market_after: 该时刻后未成交的必卖单改对手最优 (逃生通道)。
    reconcile_times: 每日定时对账时点 (启动/收盘对账另算)。
    """

    account_id: str = ""
    qmt_path: str = ""                     # miniQMT userdata_mini 路径
    db_path: str = "data/trade/trade.db"
    raw_log_path: str = "data/trade/raw_reports.jsonl"
    kill_flag_path: str = "data/trade/KILL"
    fake_sdk: bool = False                 # True 用 FakeGateway (仿真/测试环境)
    exclude_etf: bool = True               # ETF 不纳入自动管理 (2026-07-27
                                           # ETF 误卖事件裁决③, 口径见 book.is_etf)
    daily_loss_limit: float = 0.05         # 日亏软熔断阈值 (相对盘前基准)
    stop: StopConfig = field(default_factory=StopConfig)
    position_sizing: PositionSizingConfig = field(
        default_factory=PositionSizingConfig)
    auto_buy: AutoBuyConfig = field(default_factory=AutoBuyConfig)
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    monitor_scan_interval_sec: int = 60    # 监控腿轮询间隔
    sync_interval_sec: int = 180           # 增量同步间隔 (成交补记+委托回写,
                                           # 2026-07-30: 回调丢失的主动补偿网)
    tick_heartbeat_sec: int = 15           # 开盘时段无 tick 判不健康的秒数
    force_market_after: str = "14:57"
    reconcile_times: tuple = ("09:35", "11:30", "14:55", "15:05")
    quote_stale_sec: int = 60              # 行情快照超此秒数视为陈旧 (审计M6)


# ═══════════════════════════════════════════════════════════════
# 加载与校验 (审计L6标准: 类型 + 值域都查)
# ═══════════════════════════════════════════════════════════════

_FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "account_id": (str,),
    "qmt_path": (str,),
    "db_path": (str,),
    "raw_log_path": (str,),
    "kill_flag_path": (str,),
    "fake_sdk": (bool,),
    "exclude_etf": (bool,),
    "daily_loss_limit": (int, float),
    "stop": (dict,),
    "position_sizing": (dict,),
    "auto_buy": (dict,),
    "feishu": (dict,),
    "monitor_scan_interval_sec": (int,),
    "sync_interval_sec": (int,),
    "tick_heartbeat_sec": (int,),
    "force_market_after": (str,),
    "reconcile_times": (list, tuple),
    "quote_stale_sec": (int,),
}

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# stop 段每条的参数校验表: (字段, 类型, 是否允许布尔混入数值)
_RULE_FIELDS: dict[str, dict[str, tuple[type, ...]]] = {
    "cost_stop": {"enabled": (bool,), "threshold": (int, float)},
    "trailing_stop": {"enabled": (bool,), "activation": (int, float),
                      "drawdown": (int, float)},
    "ladder_tp": {"enabled": (bool,), "levels": (list,)},
    "time_stop": {"enabled": (bool,), "max_hold_days": (int,)},
    "cond_time_stop": {"enabled": (bool,), "days": (int,),
                       "profit": (int, float)},
    "first_day": {"enabled": (bool,), "target": (int, float)},
}


def _fail(msg: str) -> None:
    raise ValueError(msg)


def _check_hhmm(key: str, value: str) -> None:
    if not _HHMM_RE.match(value):
        _fail(f"trade 配置字段 {key} 必须是合法 HH:MM, 实际 {value!r}")


def _num(key: str, value: Any) -> float:
    """数值字段校验: bool 不算数, 字符串不放行。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"trade 配置字段 {key} 必须是数值, 实际 {value!r}")
    return float(value)


def _coerce_stop(data: dict) -> StopConfig:
    """stop 段嵌套校验 + 归一化。结构/键名与回测 stop_loss 同形,
    写错键名 = 未知规则或未知参数, fail-fast。"""
    unknown = set(data) - (set(_RULE_FIELDS) | {"priority"})
    if unknown:
        _fail(f"trade stop 配置存在未知规则: {sorted(unknown)}")
    kwargs: dict[str, Any] = {}

    priority = data.get("priority", StopConfig.priority)
    if priority not in VALID_PRIORITIES:
        _fail(f"stop.priority 必须是 {sorted(VALID_PRIORITIES)}, 实际 {priority!r}")
    kwargs["priority"] = priority

    def _rule(name: str, cls, coerce_params):
        raw = data.get(name)
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise TypeError(f"stop.{name} 必须是映射, 实际 {raw!r}")
        unknown_p = set(raw) - set(_RULE_FIELDS[name])
        if unknown_p:
            _fail(f"stop.{name} 存在未知参数: {sorted(unknown_p)}")
        params = {}
        if "enabled" in raw:
            if not isinstance(raw["enabled"], bool):
                raise TypeError(f"stop.{name}.enabled 必须是 bool")
            params["enabled"] = raw["enabled"]
        params.update(coerce_params(raw))
        return cls(**params)

    kwargs["cost_stop"] = _rule("cost_stop", CostStopConfig, lambda r: (
        {"threshold": _num("stop.cost_stop.threshold", r["threshold"])}
        if "threshold" in r else {}))
    t = kwargs["cost_stop"].threshold
    if not (-1.0 < t < 0.0):
        _fail(f"stop.cost_stop.threshold 必须在 (-1, 0) (负值口径), 实际 {t}")

    def _trailing(r):
        out = {}
        if "activation" in r:
            out["activation"] = _num("stop.trailing_stop.activation", r["activation"])
        if "drawdown" in r:
            out["drawdown"] = _num("stop.trailing_stop.drawdown", r["drawdown"])
        return out
    kwargs["trailing_stop"] = _rule("trailing_stop", TrailingStopConfig, _trailing)
    for f in ("activation", "drawdown"):
        v = getattr(kwargs["trailing_stop"], f)
        if not (0.0 < v <= 1.0):
            _fail(f"stop.trailing_stop.{f} 必须在 (0, 1], 实际 {v}")

    def _ladder(r):
        if "levels" not in r:
            return {}
        levels = []
        for lv in r["levels"]:
            if not isinstance(lv, dict) or set(lv) != {"profit", "sell_ratio"}:
                _fail(f"stop.ladder_tp.levels 元素必须是 "
                      f"{{profit, sell_ratio}} 映射 (回测同形), 实际 {lv!r}")
            p = _num("stop.ladder_tp.levels.profit", lv["profit"])
            s = _num("stop.ladder_tp.levels.sell_ratio", lv["sell_ratio"])
            if not (0.0 < p <= 1.0):
                _fail(f"阶梯档位 profit 必须在 (0, 1]: {lv!r}")
            if not (0.0 < s <= 1.0):
                _fail(f"阶梯档位 sell_ratio 必须在 (0, 1]: {lv!r}")
            levels.append((p, s))
        # 按 profit 升序归一化: 回测档位 bitmask 语义依赖顺序
        return {"levels": tuple(sorted(levels))}
    kwargs["ladder_tp"] = _rule("ladder_tp", LadderTpConfig, _ladder)

    def _days_int(key):
        def _c(r):
            if key not in r:
                return {}
            v = r[key]
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                _fail(f"{key} 必须是 ≥1 的整数, 实际 {v!r}")
            return {key: v}
        return _c
    kwargs["time_stop"] = _rule("time_stop", TimeStopConfig,
                                _days_int("max_hold_days"))

    def _cond_time(r):
        out = _days_int("days")(r)
        if "profit" in r:
            p = _num("stop.cond_time_stop.profit", r["profit"])
            if not (0.0 < p <= 1.0):
                _fail(f"stop.cond_time_stop.profit 必须在 (0, 1], 实际 {p}")
            out["profit"] = p
        return out
    kwargs["cond_time_stop"] = _rule("cond_time_stop", CondTimeStopConfig,
                                     _cond_time)

    def _first_day(r):
        if "target" not in r:
            return {}
        v = _num("stop.first_day.target", r["target"])
        if not (0.0 < v <= 1.0):
            _fail(f"stop.first_day.target 必须在 (0, 1], 实际 {v}")
        return {"target": v}
    kwargs["first_day"] = _rule("first_day", FirstDayConfig, _first_day)

    return StopConfig(**kwargs)


def _coerce_sizing(data: dict) -> PositionSizingConfig:
    unknown = set(data) - {"min_buy_amount", "max_buy_amount",
                           "lot_size", "max_positions"}
    if unknown:
        _fail(f"trade position_sizing 存在未知字段: {sorted(unknown)}")
    kwargs: dict[str, Any] = {}
    for k in ("min_buy_amount", "max_buy_amount"):
        if k in data:
            v = _num(f"position_sizing.{k}", data[k])
            if v <= 0:
                _fail(f"position_sizing.{k} 必须 > 0, 实际 {v}")
            kwargs[k] = v
    for k in ("lot_size", "max_positions"):
        if k in data:
            v = data[k]
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                _fail(f"position_sizing.{k} 必须是 ≥1 的整数, 实际 {v!r}")
            kwargs[k] = v
    cfg = PositionSizingConfig(**kwargs)
    if cfg.max_buy_amount < cfg.min_buy_amount:
        _fail(f"position_sizing.max_buy_amount ({cfg.max_buy_amount}) "
              f"< min_buy_amount ({cfg.min_buy_amount})")
    return cfg


def _coerce_auto_buy(data: dict) -> AutoBuyConfig:
    unknown = set(data) - {"enabled", "time", "formula_name", "formula_arg",
                           "amount_per_stock", "max_buys_per_day", "universe"}
    if unknown:
        _fail(f"trade auto_buy 存在未知字段: {sorted(unknown)}")
    kwargs: dict[str, Any] = {}
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise TypeError("auto_buy.enabled 必须是 bool")
        kwargs["enabled"] = data["enabled"]
    if "time" in data:
        if not isinstance(data["time"], str):
            raise TypeError("auto_buy.time 必须是 str")
        _check_hhmm("auto_buy.time", data["time"])
        kwargs["time"] = data["time"]
    for k in ("formula_name", "formula_arg"):
        if k in data:
            if not isinstance(data[k], str):
                raise TypeError(f"auto_buy.{k} 必须是 str")
            kwargs[k] = data[k]
    if "formula_name" in kwargs and not kwargs["formula_name"]:
        _fail("auto_buy.formula_name 不能为空 (公式都没给选什么股)")
    if "amount_per_stock" in data:
        v = _num("auto_buy.amount_per_stock", data["amount_per_stock"])
        if v <= 0:
            _fail(f"auto_buy.amount_per_stock 必须 > 0, 实际 {v}")
        kwargs["amount_per_stock"] = v
    if "max_buys_per_day" in data:
        v = data["max_buys_per_day"]
        if isinstance(v, bool) or not isinstance(v, int) or not (1 <= v <= 100):
            _fail(f"auto_buy.max_buys_per_day 必须是 1-100 的整数, 实际 {v!r}")
        kwargs["max_buys_per_day"] = v
    if "universe" in data:
        if not isinstance(data["universe"], dict):
            raise TypeError("auto_buy.universe 必须是映射 (与回测 selection.universe 同形)")
        kwargs["universe"] = dict(data["universe"])
    return AutoBuyConfig(**kwargs)


_FEISHU_LEVELS = ("full", "summary")


def _coerce_feishu(data: dict) -> FeishuConfig:
    """飞书通知段校验。enabled 布尔 + daily_report_level 详尽档;
    webhook URL 不入配置。"""
    if not isinstance(data, dict):
        raise TypeError(f"trade feishu 必须是映射, 实际 {data!r}")
    unknown = set(data) - {"enabled", "daily_report_level"}
    if unknown:
        _fail(f"trade feishu 存在未知字段: {sorted(unknown)}")
    kwargs: dict = {}
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise TypeError("feishu.enabled 必须是 bool")
        kwargs["enabled"] = data["enabled"]
    if "daily_report_level" in data:
        lvl = data["daily_report_level"]
        if not isinstance(lvl, str) or lvl not in _FEISHU_LEVELS:
            _fail(f"feishu.daily_report_level 只能是 {list(_FEISHU_LEVELS)}, 实际 {lvl!r}")
        kwargs["daily_report_level"] = lvl
    return FeishuConfig(**kwargs)


def _coerce(key: str, value: Any) -> Any:
    """顶层字段类型 + 值域校验, 然后归一化。不符直接抛, 不猜不修。"""
    expected = _FIELD_TYPES[key]
    if expected == (bool,):
        if not isinstance(value, bool):
            raise TypeError(f"trade 配置字段 {key} 类型错误: 期望 bool, 实际 {value!r}")
        return value
    if isinstance(value, bool) or not isinstance(value, expected):
        raise TypeError(
            f"trade 配置字段 {key} 类型错误: 期望 {expected}, 实际 {type(value)} ({value!r})"
        )
    if key == "stop":
        return _coerce_stop(value)
    if key == "position_sizing":
        return _coerce_sizing(value)
    if key == "auto_buy":
        return _coerce_auto_buy(value)
    if key == "feishu":
        return _coerce_feishu(value)
    if key == "reconcile_times":
        times = tuple(str(t) for t in value)
        for t in times:
            _check_hhmm(key, t)
        return times
    if key == "force_market_after":
        _check_hhmm(key, value)
        return value
    if key == "daily_loss_limit":
        v = float(value)
        if not (0.0 < v <= 1.0):
            _fail(f"trade 配置字段 {key} 必须在 (0, 1] 区间, 实际 {v!r}")
        return v
    return value


def trade_config_from_dict(data: dict) -> TradeConfig:
    """dict → TradeConfig 的全量校验 (未知字段/类型/值域 fail-fast)。
    load_trade_config (yaml 文件) 与 api PUT /api/trade/config (JSON body)
    共用这一份校验逻辑 —— 判断只有一处, 不写第二份。"""
    unknown = set(data) - set(_FIELD_TYPES)
    if unknown:
        _fail(f"trade 配置存在未知字段: {sorted(unknown)}")
    kwargs = {k: _coerce(k, v) for k, v in data.items()}
    return TradeConfig(**kwargs)


def load_trade_config(path: str | Path) -> TradeConfig:
    """从 yaml 加载交易配置。文件不存在抛 FileNotFoundError;
    未知字段抛 ValueError (防手滑写错键名静默走默认值);
    缺字段用 TradeConfig 默认值; 类型/值域不对 fail-fast。"""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    # 允许 yaml 顶层套一层 trade: 与 VERA 现有配置风格 (分节) 对齐
    if "trade" in data and isinstance(data["trade"], dict):
        data = data["trade"]

    return trade_config_from_dict(data)


def trade_config_to_dict(cfg: TradeConfig) -> dict:
    """TradeConfig → 结构化 dict (GET /api/trade/config 出参 +
    yaml 写回共用, 与入参校验同形 —— 往返无损)。"""
    s = cfg.stop
    ps = cfg.position_sizing
    return {
        "account_id": cfg.account_id,
        "qmt_path": cfg.qmt_path,
        "db_path": cfg.db_path,
        "raw_log_path": cfg.raw_log_path,
        "kill_flag_path": cfg.kill_flag_path,
        "fake_sdk": cfg.fake_sdk,
        "exclude_etf": cfg.exclude_etf,
        "daily_loss_limit": cfg.daily_loss_limit,
        "stop": {
            "priority": s.priority,
            "cost_stop": {"enabled": s.cost_stop.enabled,
                          "threshold": s.cost_stop.threshold},
            "trailing_stop": {"enabled": s.trailing_stop.enabled,
                              "activation": s.trailing_stop.activation,
                              "drawdown": s.trailing_stop.drawdown},
            "ladder_tp": {"enabled": s.ladder_tp.enabled,
                          "levels": [{"profit": p, "sell_ratio": r}
                                     for p, r in s.ladder_tp.levels]},
            "time_stop": {"enabled": s.time_stop.enabled,
                          "max_hold_days": s.time_stop.max_hold_days},
            "cond_time_stop": {"enabled": s.cond_time_stop.enabled,
                               "days": s.cond_time_stop.days,
                               "profit": s.cond_time_stop.profit},
            "first_day": {"enabled": s.first_day.enabled,
                          "target": s.first_day.target},
        },
        "position_sizing": {
            "min_buy_amount": ps.min_buy_amount,
            "max_buy_amount": ps.max_buy_amount,
            "lot_size": ps.lot_size,
            "max_positions": ps.max_positions,
        },
        "auto_buy": {
            "enabled": cfg.auto_buy.enabled,
            "time": cfg.auto_buy.time,
            "formula_name": cfg.auto_buy.formula_name,
            "formula_arg": cfg.auto_buy.formula_arg,
            "amount_per_stock": cfg.auto_buy.amount_per_stock,
            "max_buys_per_day": cfg.auto_buy.max_buys_per_day,
            "universe": dict(cfg.auto_buy.universe),
        },
        "feishu": {"enabled": cfg.feishu.enabled,
                   "daily_report_level": cfg.feishu.daily_report_level},
        "monitor_scan_interval_sec": cfg.monitor_scan_interval_sec,
        "sync_interval_sec": cfg.sync_interval_sec,
        "tick_heartbeat_sec": cfg.tick_heartbeat_sec,
        "force_market_after": cfg.force_market_after,
        "reconcile_times": list(cfg.reconcile_times),
        "quote_stale_sec": cfg.quote_stale_sec,
    }


_PANEL_HEADER = (
    "# 本文件可能被设置面板重写 (PUT /api/trade/config), 手写注释会被覆盖\n"
)


def save_trade_config(cfg: TradeConfig, path: str | Path) -> None:
    """写回 config yaml (设置面板保存)。整段重写 trade: 节 —
    注释保不住, 文件头生成一行提示 (任务书裁决: 注释丢就丢了)。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        f.write(_PANEL_HEADER)
        yaml.safe_dump({"trade": trade_config_to_dict(cfg)}, f,
                       allow_unicode=True, sort_keys=False,
                       default_flow_style=False)

