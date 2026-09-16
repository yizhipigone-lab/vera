"""open_t1 入场口径: 信号日 T → T+1 开盘价买入 (2026-08-20).

计划书: docs/plan/2026-08-20_回测次日开盘买入模式_计划书.md

规则 (用户口径):
- T 日收盘出的信号, 移到 T+1 日首个可交易 bar, 以该 bar 开盘价买入
- T+1 一字涨停 (日 OHLC 四价合一 且 >= 前收*(1+涨停幅度)*0.997) → 拒买
  (一字跌停/平盘不拒 — 跌停随便买; 涨停价开盘但盘中有成交也不拒)
- 信号日已是数据最后交易日 → 丢弃 (遵守 v3.5: 执行窗口=请求区间, 不外延取数)
- T+1 全天无可交易 bar (停牌) → 丢弃
- 分钟级 period: 日 OHLC 由当日 bar 聚合 (open=首bar open, high/low=当日极值,
  close=末bar close), 与 engine._filter_limit_up 的分钟级口径一致

纯函数, 不进 BacktestLoop; loop 侧只需要 buy_price_np=open 矩阵 (见
backtest/loop/entry.py 的 EntryEngine.buy_price_np)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from backtest._constants import detect_limit_up  # 2026-09-16 B3: 涨停判定单一真相源


@dataclass(frozen=True)
class T1ShiftResult:
    """open_t1 信号平移结果 + 统计 (统计进 result.entry_mode_info, 见 engine)。"""

    entries: pd.DataFrame          # 平移后的入场信号 (与原 entries 同 shape)
    n_signals: int                 # 原始信号数
    n_shifted: int                 # 成功平移数
    n_oneline_limit_up: int        # T+1 一字涨停拒买数
    n_no_t1_bar: int               # 无 T+1 数据丢弃数 (信号日=最后交易日)
    n_no_tradable_bar: int         # T+1 全天不可交易丢弃数


def shift_entries_to_next_open(
    entries: pd.DataFrame,
    open_df: pd.DataFrame,
    high_df: pd.DataFrame,
    low_df: pd.DataFrame,
    close_df: pd.DataFrame,
    limit_ratio_vec: np.ndarray,
    tradable_np: Optional[np.ndarray] = None,
) -> T1ShiftResult:
    """把 T 日入场信号平移到 T+1 首个可交易 bar, 并按一字涨停规则拒买。

    Args:
        entries: 原始入场信号 (bar × stock, bool)。T 日任意 bar 的信号都算
            (run() 路径信号恒在 T 日末 bar; run_cached 调用方可能在任意 bar)。
        open_df/high_df/low_df/close_df: 与 entries 同 shape 的四价矩阵。
            允许 NaN (停牌/缺口)。
        limit_ratio_vec: 每列涨停幅度 (engine._limit_ratio_vector 口径:
            主板 0.10 / 创业科创 0.20 / 北交所 0.30 / ST 0.05)。
        tradable_np: 可交易掩码 (bar × stock, bool), None 时只看开盘价有效性。

    Returns:
        T1ShiftResult。平移后 entries 里 True 的位置 = T+1 目标 bar;
        买入价由调用方取 open 矩阵同位置值 (buy_price_np=open.values)。
    """
    idx = entries.index
    cols = entries.columns
    day_id = idx.normalize()

    # ── 日级聚合 (分钟级 → 日 OHLC; 1d 恒等) ──
    day_close = close_df.groupby(day_id).last()
    day_open = open_df.groupby(day_id).first()
    day_high = high_df.groupby(day_id).max()
    day_low = low_df.groupby(day_id).min()
    uniq_days = day_close.index.sort_values()
    day_pos = {d: k for k, d in enumerate(uniq_days)}

    # ── T+1 一字涨停判定: 四价合一 且 达涨停价 (0.3% 容差与 _filter_limit_up 同口径) ──
    prev_close = day_close.shift(1)
    ratio_s = pd.Series(np.asarray(limit_ratio_vec, dtype=np.float64), index=cols)
    one_line = ((day_open == day_high) & (day_high == day_low)
                & (day_low == day_close))
    at_limit_up = detect_limit_up(day_close, prev_close, ratio_s)  # B3: 公式同 _filter_limit_up
    yiziban = (one_line & at_limit_up).fillna(False)

    # ── 每 bar → 所属交易日序号; 每日的 bar 位置列表 ──
    bar_day_pos = np.array([day_pos[d] for d in day_id.values])
    day_bars = [np.nonzero(bar_day_pos == k)[0] for k in range(len(uniq_days))]

    open_v = open_df.values
    new_vals = np.zeros(entries.values.shape, dtype=bool)
    n_signals = n_shifted = n_yz = n_no_t1 = n_no_trad = 0

    for bar_i, ci in np.argwhere(entries.values):
        bar_i, ci = int(bar_i), int(ci)
        n_signals += 1
        k = bar_day_pos[bar_i]
        if k + 1 >= len(uniq_days):
            n_no_t1 += 1
            continue
        k1 = k + 1
        if bool(yiziban.iloc[k1, ci]):
            n_yz += 1
            continue
        # T+1 日首个可交易且开盘有价的 bar (日内顺延; 全天没有才丢)
        target = None
        for b in day_bars[k1]:
            b = int(b)
            if (tradable_np is not None and ci < tradable_np.shape[1]
                    and not tradable_np[b, ci]):
                continue
            op = open_v[b, ci]
            if np.isnan(op) or op <= 0.0:
                continue
            target = b
            break
        if target is None:
            n_no_trad += 1
            continue
        new_vals[target, ci] = True
        n_shifted += 1

    return T1ShiftResult(
        entries=pd.DataFrame(new_vals, index=idx, columns=cols),
        n_signals=n_signals, n_shifted=n_shifted,
        n_oneline_limit_up=n_yz, n_no_t1_bar=n_no_t1,
        n_no_tradable_bar=n_no_trad,
    )
