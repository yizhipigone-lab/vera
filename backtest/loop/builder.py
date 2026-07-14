"""BacktestLoop 构造器 — 把 _simulate_core_v3 的 39 参数映射成 BacktestLoop 对象图。

候选 A 阶段 2 — stage 4 兼容壳 + parity 测试共用此桥。
capability gating（HA1）: enabled=False 的策略不进 dispatcher dict。
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from .state import BacktestParams, PositionBook
from .strategies import (
    CostStopStrategy, LadderTpStrategy, TrailingStrategy,
    TimeStopStrategy, CondTimeStrategy, FirstDayStrategy,
)
from .exit_engine import ExitDispatcher, Priority
from .absolute import FormulaSellStrategy
from .entry import EntryEngine
from .equity import EquityTracker
from .loop import BacktestLoop


def build_backtest_loop(
    initial_capital: float, commission: float,
    min_buy_amount: float, max_buy_amount: float, lot_size: int, min_lots: int,
    cost_stop_enabled: bool, cost_stop_threshold: float,
    trailing_enabled: bool, trailing_activation: float, trailing_drawdown: float,
    ladder_enabled: bool, ladder_profits: np.ndarray, ladder_ratios: np.ndarray,
    n_ladder: int,
    time_enabled: bool, max_hold_days: int,
    cond_time_enabled: bool, cond_time_days: int, cond_time_profit: float,
    first_day_enabled: bool = False, first_day_target: float = 0.03,
    bpday: int = 1, slippage: float = 0.0, stamp_tax: float = 0.0,
    max_position_pct: float = 1.0,
    ladder_tp_first: bool = False, trailing_first: bool = False,
    formula_exit_np: Optional[np.ndarray] = None,
    formula_exit_ratio: float = 1.0, formula_exit_lag_bars: int = 1,
) -> BacktestLoop:
    """从 _simulate_core_v3 的参数构造 BacktestLoop。

    签名顺序对齐 _simulate_core_v3 的 positional 参数（engine.py）。
    """
    # M7: ladder 数组入口归一化 float64, 防外部传 float32/list 导致触发边界漂移
    ladder_profits = np.asarray(ladder_profits, dtype=np.float64)
    ladder_ratios = np.asarray(ladder_ratios, dtype=np.float64)
    params = BacktestParams(
        initial_capital=initial_capital, commission=commission,
        slippage=slippage, stamp_tax=stamp_tax,
        min_buy_amount=min_buy_amount, max_buy_amount=max_buy_amount,
        lot_size=lot_size, min_lots=min_lots,
        bpday=bpday, max_position_pct=max_position_pct,
    )

    # ── capability gating: 按 enabled 过滤策略 ──
    strategies = {}
    if cost_stop_enabled:
        strategies["cost_stop"] = CostStopStrategy(threshold=cost_stop_threshold)
    if ladder_enabled:
        strategies["ladder_tp"] = LadderTpStrategy()
    if trailing_enabled:
        strategies["trailing"] = TrailingStrategy(
            activation=trailing_activation, drawdown=trailing_drawdown)
    if time_enabled:
        strategies["time_stop"] = TimeStopStrategy(max_hold_days=max_hold_days)
    if cond_time_enabled:
        strategies["cond_time"] = CondTimeStrategy(
            days=cond_time_days, profit=cond_time_profit)
    if first_day_enabled:
        strategies["first_day"] = FirstDayStrategy(target=first_day_target)

    # ── priority ──
    if trailing_first:
        priority = Priority.TRAILING_FIRST
    elif ladder_tp_first:
        priority = Priority.LADDER_TP_FIRST
    else:
        priority = Priority.STOP_FIRST

    dispatcher = ExitDispatcher(strategies, priority)

    # ── absolutes (formula_sell) ──
    absolutes = []
    if formula_exit_np is not None:
        absolutes.append(FormulaSellStrategy(
            formula_exit_np, ratio=formula_exit_ratio,
            lag_bars=formula_exit_lag_bars))

    n_dates_hint = 0  # BacktestLoop.run 内按 price_np 实际 shape 建 TradeBuffer/EquityTracker
    return BacktestLoop(
        params=params,
        dispatcher=dispatcher,
        absolutes=absolutes,
        entry_engine=EntryEngine(params),
        equity_tracker=EquityTracker(n_dates_hint),
        position_book=PositionBook(),
        ladder_profits=ladder_profits,
        ladder_ratios=ladder_ratios,
        n_ladder=n_ladder,
    )
