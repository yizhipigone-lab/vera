"""5m 数据层降级 (计划书 2026-07-18, Phase A)。

TDX 5m 历史有上限且存在全市场缺口周 (如 2026-06-23~29)。缺 5m 的股-天
当前被 `_build_entry_signals` 丢弃信号 (commit 021aa6c)。本模块提供
"每股每天粒度降级": 某股某天 5m 缺失但 1d 有数据 → 用该股当天真实 1d OHLC
填满当天 48 根 5m bar, 保住信号; 停牌 (1d 也缺) 不填充, 保持不可交易。

关键不变量: 网格从交易日历合成, 每天恰好 48 根 bar (loop 的 T+1 `i//bpday`
与首日策略 `bar_index%bpday` 都假设 48 根/天)。

纯函数模块, 不做 I/O (交易日历/1d 拉取由 engine 注入)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from utils.logger import get_logger

logger = get_logger(__name__)

BARS_5M_PER_DAY = 48  # 9:35..11:30 (24) + 13:05..15:00 (24)

# 复权一致性容差 (LOW-1): 前复权浮点, 5m 日聚合 vs 1d 应在此容差内一致
_ADJUST_RTOL = 1e-4


def _bar_times_one_day() -> List[str]:
    """一天的 48 根 bar 时间 (HH:MM): 9:35..11:30 + 13:05..15:00。"""
    times = []
    t = pd.Timestamp("2000-01-01 09:35")
    for _ in range(24):
        times.append(t.strftime("%H:%M"))
        t += pd.Timedelta(minutes=5)
    t = pd.Timestamp("2000-01-01 13:05")
    for _ in range(24):
        times.append(t.strftime("%H:%M"))
        t += pd.Timedelta(minutes=5)
    return times


_BAR_TIMES = _bar_times_one_day()


def synthesize_5m_grid(trading_days: List[pd.Timestamp]) -> pd.DatetimeIndex:
    """从交易日历合成完整 5m 网格: 每个交易日 48 根 bar 时间戳。

    全市场缺口日 (TDX 零返回, index 里根本没行) 也借此获得行, 才能填充
    (审计 CRITICAL-1)。
    """
    if not trading_days:
        return pd.DatetimeIndex([])
    out = []
    for d in trading_days:
        ds = pd.Timestamp(d).strftime("%Y-%m-%d")
        out.extend(pd.Timestamp(f"{ds} {hm}") for hm in _BAR_TIMES)
    return pd.DatetimeIndex(out)


@dataclass(frozen=True)
class DegradeResult:
    """apply_5m_degradation 返回。degraded_np 为 bar 级 (n_bars, n_stocks) 布尔。"""

    close: pd.DataFrame
    high: Optional[pd.DataFrame]
    low: Optional[pd.DataFrame]
    open: Optional[pd.DataFrame]
    degraded_np: np.ndarray
    n_stock_days: int                # 降级股-天总数
    degraded_days: Dict[str, int]    # {YYYY-MM-DD: 当日降级股数}
    rejected_limit_up: int           # 1d 涨停被拒绝的降级股-天数 (MEDIUM-3)
    adjust_mismatches: int           # 完整 5m 日聚合 vs 1d 不一致数 (LOW-1)


def _detect_1d_limit_up(close_1d: pd.DataFrame, limit_ratio_vec: np.ndarray) -> pd.DataFrame:
    """1d 涨停判定: close >= 前收*(1+ratio)*0.997 (与 engine._filter_limit_up 同口径)。

    在 1d 自身日期轴上算 (前收可能早于网格首日), 首行无前收 → False。
    """
    cv = close_1d.values.astype(np.float64)
    prev = np.empty_like(cv)
    prev[0] = np.nan
    prev[1:] = cv[:-1]
    limit_up = cv >= prev * (1.0 + limit_ratio_vec) * 0.997
    return pd.DataFrame(limit_up, index=close_1d.index, columns=close_1d.columns)


def apply_5m_degradation(
    close_5m: pd.DataFrame,
    high_5m: Optional[pd.DataFrame],
    low_5m: Optional[pd.DataFrame],
    open_5m: Optional[pd.DataFrame],
    close_1d: pd.DataFrame,
    high_1d: Optional[pd.DataFrame],
    low_1d: Optional[pd.DataFrame],
    open_1d: Optional[pd.DataFrame],
    *,
    window_bounds: Optional[Dict[str, Tuple[pd.Timestamp, pd.Timestamp]]] = None,
    limit_ratio_vec: Optional[np.ndarray] = None,
) -> DegradeResult:
    """对完整日历网格上"5m 缺失但 1d 有数据"的股-天, 用 1d OHLC 填满 48 根 bar。

    判定 (全部满足才降级):
      - 该股当天 5m 有效 bar < 48 (部分缺失按"整日缺"处理, 明示假设 3)
      - 当天 1d OHLC 四字段齐全 (停牌日 1d 也缺 → 不填充, G2)
      - 股-天在 window_bounds 窗口内 (稀疏窗口"窗口内才可交易"语义; None=不限制)
      - 当天 1d 非涨停 (limit_ratio_vec 提供时; 涨停买不到, 拒绝并计数, MEDIUM-3)

    输入矩阵必须已 reindex 到完整日历网格 (行数 % 48 == 0, 每天恰好 48 根)。
    不修改输入 (不可变), 返回填充后的新矩阵。
    """
    n_bars, n_stocks = close_5m.shape
    if n_bars % BARS_5M_PER_DAY != 0:
        raise ValueError(
            f"apply_5m_degradation: 行数 {n_bars} 不是 {BARS_5M_PER_DAY} 的整数倍 — "
            "输入必须先 reindex 到 synthesize_5m_grid 的完整日历网格"
        )
    n_days = n_bars // BARS_5M_PER_DAY
    cols = close_5m.columns
    day_dates = close_5m.index.normalize().unique()

    # ── 1d 对齐到 (网格日期 × 5m 列) ──
    def _align_1d(df: Optional[pd.DataFrame]) -> Optional[np.ndarray]:
        if df is None:
            return None
        a = df.reindex(index=day_dates, columns=cols)
        return a.values.astype(np.float64)

    c1 = _align_1d(close_1d)
    h1 = _align_1d(high_1d)
    l1 = _align_1d(low_1d)
    o1 = _align_1d(open_1d)
    # 审计 LOW-1: None 守卫必须先判 (np.isnan(None) 直接 TypeError)
    if c1 is None:
        has_1d = np.zeros((n_days, n_stocks), dtype=bool)
    else:
        has_1d = ~np.isnan(c1)
    for a in (h1, l1, o1):
        if a is None:
            has_1d &= False
        else:
            has_1d &= ~np.isnan(a)

    # ── 5m 有效日判定 (reshape 需要每天恰好 48 根, 网格不变量) ──
    cv = close_5m.values.astype(np.float64).reshape(n_days, BARS_5M_PER_DAY, n_stocks)
    full_day = ~np.isnan(cv).any(axis=1)  # (n_days, n_stocks)

    # ── 窗口内判定 ──
    if window_bounds is None:
        in_window = np.ones((n_days, n_stocks), dtype=bool)
    else:
        in_window = np.zeros((n_days, n_stocks), dtype=bool)
        for j, code in enumerate(cols):
            b = window_bounds.get(code) or window_bounds.get(str(code))
            if b is None:
                continue
            s, e = pd.Timestamp(b[0]).normalize(), pd.Timestamp(b[1]).normalize()
            in_window[:, j] = (day_dates >= s) & (day_dates <= e)

    # ── 1d 涨停判定 (MEDIUM-3) ──
    if limit_ratio_vec is not None and close_1d is not None and not close_1d.empty:
        # 审计 HIGH-1 (2026-07-18): ratio_vec 按 5m 列序, 必须先按列名对齐 1d
        # (1d 拉取列序/列数无保证 — KlineCache 跳过整段无数据股)。
        # 缺列 → NaN → prev NaN → 判 False (安全)。
        close_1d_al = close_1d.reindex(columns=cols)
        lu_df = _detect_1d_limit_up(close_1d_al, np.asarray(limit_ratio_vec, dtype=np.float64))
        limit_up = lu_df.reindex(index=day_dates, columns=cols,
                                 fill_value=False).values.astype(bool)
    else:
        limit_up = np.zeros((n_days, n_stocks), dtype=bool)

    # ── 填充掩码 ──
    want = ~full_day & has_1d & in_window
    fill_mask = want & ~limit_up
    rejected = int((want & limit_up).sum())

    # ── 复权一致性检查 (LOW-1): 完整 5m 日聚合 OHLC vs 1d ──
    mismatches = 0
    check = full_day & has_1d
    if check.any() and h1 is not None and l1 is not None and o1 is not None:
        hv = high_5m.values.astype(np.float64).reshape(n_days, BARS_5M_PER_DAY, n_stocks)
        lv = low_5m.values.astype(np.float64).reshape(n_days, BARS_5M_PER_DAY, n_stocks)
        ov = open_5m.values.astype(np.float64).reshape(n_days, BARS_5M_PER_DAY, n_stocks)
        agg = {
            "open": (ov[:, 0, :], o1),
            "high": (hv.max(axis=1), h1),
            "low": (lv.min(axis=1), l1),
            "close": (cv[:, -1, :], c1),
        }
        for field, (a5, a1) in agg.items():
            bad = check & ~np.isclose(a5, a1, rtol=_ADJUST_RTOL, atol=1e-9)
            if bad.any():
                mismatches += int(bad.sum())
                di, si = np.nonzero(bad)
                logger.warning(
                    "degrade_5m 复权一致性: %s 字段 %d 处 5m 日聚合 != 1d "
                    "(首处 %s %s: 5m=%.4f 1d=%.4f, 复权口径漂移?)",
                    field, int(bad.sum()),
                    day_dates[di[0]].strftime("%Y-%m-%d"), cols[si[0]],
                    a5[di[0], si[0]], a1[di[0], si[0]],
                )

    # ── 执行填充 (拷贝, 不改输入) ──
    def _fill(df: Optional[pd.DataFrame], d1: Optional[np.ndarray]) -> Optional[pd.DataFrame]:
        if df is None:
            return None
        arr = df.values.astype(np.float64).reshape(n_days, BARS_5M_PER_DAY, n_stocks)
        if fill_mask.any() and d1 is not None:
            # np.where 广播: (n_days,1,n_stocks) → (n_days,48,n_stocks)
            arr = np.where(fill_mask[:, None, :], d1[:, None, :], arr)
        return pd.DataFrame(
            arr.reshape(n_bars, n_stocks), index=df.index, columns=df.columns)

    out_close = _fill(close_5m, c1)
    out_high = _fill(high_5m, h1)
    out_low = _fill(low_5m, l1)
    out_open = _fill(open_5m, o1)

    degraded_np = np.repeat(fill_mask, BARS_5M_PER_DAY, axis=0)
    degraded_days = {
        day_dates[i].strftime("%Y-%m-%d"): int(fill_mask[i].sum())
        for i in range(n_days) if fill_mask[i].any()
    }
    n_stock_days = int(fill_mask.sum())
    if n_stock_days:
        logger.info("degrade_5m: %d 个股-天用 1d OHLC 填充降级, 分布 %s",
                    n_stock_days, degraded_days)
    if rejected:
        logger.warning("degrade_5m: %d 个降级股-天因 1d 涨停被拒绝 (信号丢弃)", rejected)

    return DegradeResult(
        close=out_close, high=out_high, low=out_low, open=out_open,
        degraded_np=degraded_np, n_stock_days=n_stock_days,
        degraded_days=degraded_days, rejected_limit_up=rejected,
        adjust_mismatches=mismatches,
    )


def scan_degraded_positions(raw_trades: np.ndarray, degraded_np: np.ndarray) -> List[dict]:
    """事后扫描 raw_trades, 标记含降级 bar 的持仓 (不进 loop, 审计 HIGH-2)。

    部分出场 (阶梯分批卖) 产生多条同 entry_idx 的交易行, 按 (ci, entry_idx)
    聚合成"持仓"再判定: 持仓区间 [entry_idx, max(exit_idx)] 内 degraded_np
    有 True → 降级持仓。返回 list[dict], 每持仓一条。
    """
    if raw_trades is None or len(raw_trades) == 0:
        return []
    groups: Dict[Tuple[int, int], dict] = {}
    for row in raw_trades:
        ci, ei, xi = int(row[0]), int(row[1]), int(row[2])
        key = (ci, ei)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"ci": ci, "entry_idx": ei, "exit_idx": xi, "n_trade_rows": 0}
        g["exit_idx"] = max(g["exit_idx"], xi)
        g["n_trade_rows"] += 1
    out = []
    n_bars = degraded_np.shape[0]
    for g in groups.values():
        ci, ei, xi = g["ci"], g["entry_idx"], min(g["exit_idx"], n_bars - 1)
        n = 0
        if ci < degraded_np.shape[1] and ei <= xi:
            n = int(degraded_np[ei:xi + 1, ci].sum())
        out.append({**g, "is_degraded": n > 0, "n_degraded_bars": n})
    return out


def recompute_last_tradable_idx(tradable_np: np.ndarray) -> np.ndarray:
    """last_tradable_idx = 每列最后一个可交易 bar 的行号 (全不可交易列 = -1)。"""
    lti = np.full(tradable_np.shape[1], -1, dtype=np.int64)
    for ci in range(tradable_np.shape[1]):
        nz = np.nonzero(tradable_np[:, ci])[0]
        if nz.size:
            lti[ci] = int(nz[-1])
    return lti
