"""2026-08-01 批次 3b C2: engine._simulate_core_v3 测试兼容壳退役后的共用直调入口。

原壳 (backtest/engine.py, 已删) 只做两件事: build_backtest_loop(核心参数) +
loop.run(矩阵参数)。本函数等价展开, 签名与原壳逐参数一致 (含默认值与位置顺序),
使各测试文件的既有调用 (含 39 位置参数调用) 只需改名, 不需要重排参数。

注: first_day_n_bars 为历史半死参数 (壳接收但从未引用, FirstDayStrategy 用
bpday-1), 此处同样接收但忽略, 仅为兼容既有测试调用签名; 勿在此接新逻辑。
"""

from __future__ import annotations

from backtest.loop import build_backtest_loop


def run_loop_direct(
    price_np, entry_np,
    initial_capital, commission,
    min_buy_amount, max_buy_amount, lot_size, min_lots,
    cost_stop_enabled, cost_stop_threshold,
    trailing_enabled, trailing_activation, trailing_drawdown,
    ladder_enabled, ladder_profits, ladder_ratios, n_ladder,
    time_enabled, max_hold_days,
    cond_time_enabled, cond_time_days, cond_time_profit,
    first_day_enabled=False, first_day_target=0.03,
    first_day_n_bars=1, high_np=None, low_np=None, bpday=1,
    slippage=0.0, stamp_tax=0.0,
    tradable_np=None, last_tradable_idx=None,
    open_np=None,
    formula_exit_np=None, formula_exit_ratio=1.0, formula_exit_lag_bars=1,
    ladder_tp_first=False,
    trailing_first=False,
    max_position_pct=1.0,
    atr_enabled=False, atr_matrix=None, atr_multiplier=3.0,
    trailing_gap_protection=False,
):
    """直调 BacktestLoop, 返回 (equity_arr, raw_trades) — 原 _simulate_core_v3 壳的等价展开。"""
    loop = build_backtest_loop(
        initial_capital, commission,
        min_buy_amount, max_buy_amount, lot_size, min_lots,
        cost_stop_enabled, cost_stop_threshold,
        trailing_enabled, trailing_activation, trailing_drawdown,
        ladder_enabled, ladder_profits, ladder_ratios, n_ladder,
        time_enabled, max_hold_days,
        cond_time_enabled, cond_time_days, cond_time_profit,
        first_day_enabled, first_day_target,
        bpday, slippage, stamp_tax, max_position_pct,
        ladder_tp_first, trailing_first,
        formula_exit_np, formula_exit_ratio, formula_exit_lag_bars,
        atr_enabled=atr_enabled, atr_matrix=atr_matrix, atr_multiplier=atr_multiplier,
        trailing_gap_protection=trailing_gap_protection,
    )
    return loop.run(price_np, entry_np, high_np, low_np, open_np,
                    tradable_np, last_tradable_idx, formula_exit_np)
