"""5m 降级影响报告 (计划书 2026-07-18 §3/§4.5, Phase B)。

对降级交易量化"同日止盈止损先后顺序未知"的影响上下范围:

- **模糊日判定**: 出场日 (降级日) 当天 high 够得着任一止盈触发价 且 low 破任一
  止损触发价 → 真实 5m 里的先后未知。
- **peak_hi / ladder_done 重放** (审计 MEDIUM-1): 触发价路径依赖, raw_trades
  只有 9 列, 须从填充后矩阵重放 entry→模糊日。
  口径 (写死): 先止损分支里模糊日自己的 high 不计入 peak_hi (先破低时当日
  high 尚未发生); 先止盈分支计入。
- **两档重算**: 乐观 = max(可达触发价, 实际价), 悲观 = min(...), 金额差累计。
- **close 价策略偏差** (审计 HIGH-1): 时间止损/条件时间止盈/首日未达标按
  bar.close 成交, 降级日 = 1d 收盘价。真 5m 早盘价不可观测 (数据已缺),
  可计算的界 = [当日 1d 最低价, 1d 最高价] vs 成交价的金额差。

纯函数, 不做 I/O。ATR 止损不在重放范围 (需 ATR 矩阵, 另议)。
"""
from __future__ import annotations

import warnings
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from backtest.metrics import MetricsCalculator
from utils.logger import get_logger

logger = get_logger(__name__)

# 按 bar.close 成交的策略 reason (time_stop.py:27 / cond_time.py:28 /
# first_day.py:41 / absolute.py:47 formula_sell)
CLOSE_BASED_REASONS = frozenset({6.0, 7.0, 9.0, 10.0, 12.0})


def _zero_report() -> dict:
    zero3 = {"pessimistic": 0.0, "actual": 0.0, "optimistic": 0.0}
    return {
        "impact_amount": dict(zero3),
        "return_range": dict(zero3),
        "ambiguous_trades": 0,
        "close_based_stop_bias": {
            "count": 0, "amount_pessimistic": 0.0, "amount_optimistic": 0.0},
    }


def compute_impact_report(
    raw_trades: np.ndarray,
    degraded_np: np.ndarray,
    high_np: Optional[np.ndarray],
    low_np: Optional[np.ndarray],
    bpday: int,
    *,
    cost_enabled: bool = False,
    cost_threshold: float = -0.12,
    trailing_enabled: bool = False,
    trailing_activation: float = 0.05,
    trailing_drawdown: float = 0.10,
    ladder_enabled: bool = False,
    ladder_profits: Tuple[float, ...] = (),
    initial_capital: float = 1_000_000.0,
    equity_arr: Optional[np.ndarray] = None,
    periods_per_year: int = 252,
) -> dict:
    """计算降级影响报告。返回 dict 并入 result.degradation。

    raw_trades 列: (ci, entry_idx, exit_idx, ep, xp, shares, pnl, ret, reason)。
    金额口径: 相对实际回测结果的调整区间 (actual 恒 0, 悲观 <= 0 <= 乐观)。
    """
    rep = _zero_report()
    if raw_trades is None or len(raw_trades) == 0:
        return rep
    if high_np is None or low_np is None:
        logger.warning("degrade_report: high/low 缺失, 影响区间无法计算 (仅笔数统计)")
        return rep

    n_bars = degraded_np.shape[0]
    n_days = n_bars // bpday
    hi = high_np[:n_days * bpday]
    lo = low_np[:n_days * bpday]
    with warnings.catch_warnings():
        # 全 NaN 列 (停牌) 的 nanmax/nanmin 告警无害, 压住
        warnings.simplefilter("ignore", RuntimeWarning)
        day_high = np.nanmax(hi.reshape(n_days, bpday, -1), axis=1)
        day_low = np.nanmin(lo.reshape(n_days, bpday, -1), axis=1)

    # 按 (ci, entry_idx) 聚合持仓 (部分出场拆多行)
    groups: Dict[Tuple[int, int], List[np.ndarray]] = {}
    for row in raw_trades:
        groups.setdefault((int(row[0]), int(row[1])), []).append(row)

    amt_p = amt_o = bias_p = bias_o = 0.0
    ambiguous = bias_count = 0
    deltas_p: List[Tuple[int, float]] = []
    deltas_o: List[Tuple[int, float]] = []

    for (ci, ei), rows in groups.items():
        if ci >= degraded_np.shape[1]:
            continue
        ep = float(rows[0][3])
        if ep <= 0:
            continue
        entry_day = ei // bpday
        level_prices = sorted(ep * (1.0 + p) for p in ladder_profits) if ladder_enabled else []

        for row in rows:
            xi, xp, sh, reason = int(row[2]), float(row[4]), float(row[5]), float(row[8])
            d = xi // bpday
            if d >= n_days or sh <= 0:
                continue
            if not degraded_np[d * bpday:(d + 1) * bpday, ci].any():
                continue  # 出场日非降级日 → 确定, 跳过
            dh, dl = day_high[d, ci], day_low[d, ci]
            if np.isnan(dh) or np.isnan(dl):
                continue

            # ── close 价策略偏差 (单列统计, 不走模糊日通道) ──
            if reason in CLOSE_BASED_REASONS:
                bias_count += 1
                dp = (dl - xp) * sh
                do = (dh - xp) * sh
                bias_p += dp
                bias_o += do
                deltas_p.append((xi, dp))
                deltas_o.append((xi, do))
                continue

            # ── peak_hi 重放: entry → 模糊日 (不含当日) ──
            end = d * bpday
            seg = high_np[ei:end, ci] if end > ei else high_np[ei:ei + 1, ci]
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                peak_prev = float(np.nanmax(seg))
            if np.isnan(peak_prev):
                peak_prev = ep

            plausible: List[float] = []
            # 成本止损 (静态价)
            if cost_enabled:
                sp = ep * (1.0 + cost_threshold)
                if dl <= sp:
                    plausible.append(sp)
            # 移动止损止盈: 悲观线 (当日 high 不计 peak) + 乐观线 (计入)
            if trailing_enabled:
                if peak_prev / ep - 1.0 >= trailing_activation:
                    line_p = peak_prev * (1.0 - trailing_drawdown)
                    if dl <= line_p:
                        plausible.append(line_p)
                peak_o = max(peak_prev, dh)
                if peak_o / ep - 1.0 >= trailing_activation:
                    line_o = peak_o * (1.0 - trailing_drawdown)
                    if dl <= line_o:
                        plausible.append(line_o)
            # 阶梯止盈: ladder_done 重放, 只判剩余档
            if level_prices:
                done = set()
                for day in range(entry_day, d):
                    hd = day_high[day, ci]
                    if np.isnan(hd):
                        continue
                    for lp in level_prices:
                        if hd >= lp:
                            done.add(lp)
                for lp in level_prices:
                    if lp not in done and dh >= lp:
                        plausible.append(lp)

            plausible = [p for p in plausible if p > 0 and not np.isnan(p)]
            if len({round(p, 10) for p in plausible}) < 2:
                continue  # 非模糊日 (只够得着一侧) → 确定
            ambiguous += 1
            pess = min(min(plausible), xp)
            opt = max(max(plausible), xp)
            dp = (pess - xp) * sh
            do = (opt - xp) * sh
            amt_p += dp
            amt_o += do
            deltas_p.append((xi, dp))
            deltas_o.append((xi, do))

    tot_p = round(amt_p + bias_p, 2)
    tot_o = round(amt_o + bias_o, 2)
    rep["ambiguous_trades"] = ambiguous
    rep["close_based_stop_bias"] = {
        "count": bias_count,
        "amount_pessimistic": round(bias_p, 2),
        "amount_optimistic": round(bias_o, 2),
    }
    rep["impact_amount"] = {"pessimistic": tot_p, "actual": 0.0, "optimistic": tot_o}
    cap = float(initial_capital) if initial_capital else 1.0
    rep["return_range"] = {
        "pessimistic": round(tot_p / cap, 6),
        "actual": 0.0,
        "optimistic": round(tot_o / cap, 6),
    }

    # ── 夏普/最大回撤三档: 反事实 delta 自出场 bar 起累计进权益曲线 ──
    if equity_arr is not None and len(equity_arr) > 1:
        base = equity_arr.astype(np.float64)
        curve_p = base.copy()
        curve_o = base.copy()
        for xi, dv in deltas_p:
            if 0 <= xi < len(curve_p):
                curve_p[xi:] += dv
        for xi, dv in deltas_o:
            if 0 <= xi < len(curve_o):
                curve_o[xi:] += dv
        empty_trades = pd.DataFrame()

        def _m(curve):
            return MetricsCalculator.compute_all(
                pd.DataFrame({"equity": curve}), empty_trades,
                initial_capital=float(initial_capital),
                periods_per_year=periods_per_year)

        m_p, m_a, m_o = _m(curve_p), _m(base), _m(curve_o)
        rep["sharpe_range"] = {
            "pessimistic": m_p.get("sharpe_ratio", 0.0),
            "actual": m_a.get("sharpe_ratio", 0.0),
            "optimistic": m_o.get("sharpe_ratio", 0.0),
        }
        rep["max_drawdown_range"] = {
            "pessimistic": m_p.get("max_drawdown", 0.0),
            "actual": m_a.get("max_drawdown", 0.0),
            "optimistic": m_o.get("max_drawdown", 0.0),
        }

    if ambiguous or bias_count:
        logger.info(
            "degrade_report: 模糊日交易 %d 笔, close 价策略 %d 笔, "
            "影响金额 [%.0f, 0, +%.0f]",
            ambiguous, bias_count, tot_p, tot_o)
    return rep
