"""VeraCore 回测引擎 — Numba JIT 加速的多股票模拟器。

止损止盈优先级: 成本止损 > 移动止损 > 阶梯止盈 > 时间止损
每日先卖后买，模拟 A 股 T+0 资金可用规则。
"""

import pandas as pd
import numpy as np
from typing import Dict, Optional, Any
from numba import njit, prange

from backtest.stop_manager import StopManager
from backtest.metrics import MetricsCalculator
from core.data_fetcher import DataFetcher
from utils.logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════
# Numba JIT 核心 — 运行在原始 NumPy 数组上，无 pandas 依赖
# ═══════════════════════════════════════════════════════════════

@njit(cache=True)
def _simulate_core(
    price_np,          # (n_dates, n_stocks) float64
    entry_np,          # (n_dates, n_stocks) bool
    exit_np,           # (n_dates, n_stocks) bool
    initial_capital,   # float64
    commission,        # float64
    max_position_pct,  # float64
):
    """
    VeraCore 回测核心。纯 Numba JIT，速度接近 C。

    Returns:
        equity_arr: (n_dates,) float64 每日权益
        trade_records: list of (stock_idx, entry_idx, exit_idx, entry_px, exit_px, shares, pnl, profit_pct, reason_code)
    """
    n_dates = price_np.shape[0]
    n_stocks = price_np.shape[1]

    # 持仓追踪: 使用并行数组而非 dict
    MAX_POS = 5000  # 最大同时持仓数
    pos_code = np.full(MAX_POS, -1, dtype=np.int32)      # stock index
    pos_shares = np.zeros(MAX_POS, dtype=np.float64)
    pos_entry_px = np.zeros(MAX_POS, dtype=np.float64)
    pos_entry_idx = np.full(MAX_POS, -1, dtype=np.int32)  # bar index of entry
    pos_count = 0  # 当前持仓数

    cash = float(initial_capital)
    equity_arr = np.empty(n_dates, dtype=np.float64)

    # 预分配交易记录 (每笔交易 8 个字段)
    max_trades = n_dates * n_stocks // 4 + 1000
    trades = np.empty((max_trades, 9), dtype=np.float64)
    trade_count = 0

    for i in range(n_dates):
        # ── 1. 卖出 (先卖释放资金) ──
        p = 0
        while p < pos_count:
            ci = pos_code[p]
            if ci >= 0 and exit_np[i, ci]:
                shares = pos_shares[p]
                ep = pos_entry_px[p]
                xp = price_np[i, ci]

                if not (np.isnan(xp) or xp <= 0.0):
                    gross = shares * xp * (1.0 - commission)
                    cash += gross
                    pnl = gross - shares * ep
                    pp = (xp - ep) / ep if ep > 0.0 else 0.0

                    # 记录交易: (ci, entry_bar, exit_bar=i, ep, xp, shares, pnl, pp, reason=1)
                    if trade_count < max_trades:
                        trades[trade_count, 0] = float(ci)
                        trades[trade_count, 1] = float(pos_entry_idx[p])
                        trades[trade_count, 2] = float(i)
                        trades[trade_count, 3] = ep
                        trades[trade_count, 4] = xp
                        trades[trade_count, 5] = shares
                        trades[trade_count, 6] = pnl
                        trades[trade_count, 7] = pp
                        trades[trade_count, 8] = 1.0  # reason_code: 1=signal
                        trade_count += 1

                    # 移除持仓 (swap with last)
                    pos_count -= 1
                    if p < pos_count:
                        pos_code[p] = pos_code[pos_count]
                        pos_shares[p] = pos_shares[pos_count]
                        pos_entry_px[p] = pos_entry_px[pos_count]
                        pos_entry_idx[p] = pos_entry_idx[pos_count]
                    pos_code[pos_count] = -1
                    continue  # 不递增 p，因为当前位置已被交换
            p += 1

        # ── 2. 买入 ──
        for ci in range(n_stocks):
            if entry_np[i, ci]:
                bp = price_np[i, ci]
                if np.isnan(bp) or bp <= 0.0:
                    continue
                max_cost = cash * max_position_pct
                shares = int(max_cost / bp)
                if shares <= 0:
                    continue
                cost = shares * bp * (1.0 + commission)
                if cost <= cash and pos_count < MAX_POS:
                    cash -= cost
                    pos_code[pos_count] = ci
                    pos_shares[pos_count] = float(shares)
                    pos_entry_px[pos_count] = bp
                    pos_entry_idx[pos_count] = i
                    pos_count += 1

        # ── 3. 计算权益 ──
        pos_value = 0.0
        for p in range(pos_count):
            ci = pos_code[p]
            if ci >= 0:
                px = price_np[i, ci]
                if not np.isnan(px):
                    pos_value += pos_shares[p] * px
        equity_arr[i] = cash + pos_value

    # ── 4. 期末强平 ──
    last = n_dates - 1
    p = 0
    while p < pos_count:
        ci = pos_code[p]
        if ci >= 0:
            fp = price_np[last, ci]
            if not (np.isnan(fp) or fp <= 0.0):
                shares = pos_shares[p]
                ep = pos_entry_px[p]
                gross = shares * fp * (1.0 - commission)
                cash += gross
                pp = (fp - ep) / ep if ep > 0.0 else 0.0
                if trade_count < max_trades:
                    trades[trade_count, 0] = float(ci)
                    trades[trade_count, 1] = float(pos_entry_idx[p])
                    trades[trade_count, 2] = float(last)
                    trades[trade_count, 3] = ep
                    trades[trade_count, 4] = fp
                    trades[trade_count, 5] = shares
                    trades[trade_count, 6] = gross - shares * ep
                    trades[trade_count, 7] = pp
                    trades[trade_count, 8] = 2.0  # reason_code: 2=end_of_data
                    trade_count += 1
        pos_count -= 1
        if p < pos_count:
            pos_code[p] = pos_code[pos_count]
            pos_shares[p] = pos_shares[pos_count]
            pos_entry_px[p] = pos_entry_px[pos_count]
            pos_entry_idx[p] = pos_entry_idx[pos_count]
        p += 1

    equity_arr[last] = cash
    return equity_arr, trades[:trade_count]


# ═══════════════════════════════════════════════════════════════
# BacktestEngine — Python 包装层
# ═══════════════════════════════════════════════════════════════

class BacktestEngine:
    """VeraCore 回测引擎 — Numba 加速 + 止损止盈优先级。"""

    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        self.initial_capital = float(config.get("initial_capital", 100000.0))
        self.commission = float(config.get("commission", 0.0003))
        self.slippage = float(config.get("slippage", 0.001))
        self.max_position_pct = float(
            config.get("position_sizing", {}).get("max_position_pct", 0.15)
        )

    def run(self, selections, start_time="", end_time="", stop_config=None):
        if selections.empty:
            return self._empty_result()

        logger.info("=" * 60)
        logger.info(f"VeraCore 回测: 资金={self.initial_capital:,.0f} "
                     f"费率={self.commission:.4f} 仓位上限={self.max_position_pct:.0%}")

        # 1. 获取价格
        codes = selections["stock_code"].unique().tolist()
        close = self._fetch_prices(codes, start_time, end_time)
        if close.empty:
            return self._empty_result()
        close = self._ensure_index(close)

        # 2. 构建信号
        entries = self._build_entry_signals(selections, close)
        logger.info(f"买入信号: {entries.sum().sum()}")

        sm = StopManager(stop_config)
        exits, exit_info = sm.compute_exit_signals(close, entries)
        logger.info(f"卖出信号: {exits.sum().sum()}")

        # 3. 对齐
        cols = close.columns.intersection(entries.columns).intersection(exits.columns)
        close = close[cols].ffill().bfill()
        entries = entries.reindex(index=close.index, columns=close.columns, fill_value=False)
        exits = exits.reindex(index=close.index, columns=close.columns, fill_value=False)

        # 4. VeraCore 核心 (Numba JIT)
        col_map = {c: i for i, c in enumerate(close.columns)}
        dates = close.index

        t0 = pd.Timestamp.now()
        equity_arr, raw_trades = _simulate_core(
            close.values.astype(np.float64),
            entries.values,
            exits.values,
            float(self.initial_capital),
            float(self.commission),
            float(self.max_position_pct),
        )
        elapsed = (pd.Timestamp.now() - t0).total_seconds()
        logger.info(f"VeraCore 完成: {len(raw_trades)} 笔交易, {elapsed:.2f}s")

        # 5. 构建输出
        equity_curve = pd.DataFrame({
            "date": dates,
            "equity": equity_arr,
        })
        equity_curve.set_index("date", inplace=True)
        peak = equity_curve["equity"].expanding().max()
        equity_curve["drawdown"] = (equity_curve["equity"] - peak) / peak
        equity_curve.reset_index(inplace=True)

        # 6. 交易记录
        trades_df = self._build_trades(raw_trades, close.columns, dates, exit_info, col_map)
        trades_df["entry_date"] = pd.to_datetime(trades_df["entry_date"])
        trades_df["exit_date"] = pd.to_datetime(trades_df["exit_date"])

        metrics = MetricsCalculator.compute_all(equity_curve, trades_df, self.initial_capital)
        self._log_results(metrics)

        return {
            "equity_curve": equity_curve,
            "trades": trades_df,
            "metrics": metrics,
            "stop_config_summary": sm.get_config_summary(),
            "selections": selections,
            "stock_count": len(cols),
        }

    def _build_trades(self, raw, columns, dates, exit_info, col_map):
        if len(raw) == 0:
            return pd.DataFrame()
        records = []
        reason_map = {1.0: "signal", 2.0: "end_of_data",
                      3.0: "cost_stop", 4.0: "trailing_stop",
                      5.0: "ladder_tp", 6.0: "time_stop"}
        inv_col = {i: c for c, i in col_map.items()}
        for row in raw:
            ci = int(row[0])
            code = inv_col.get(ci, str(ci))
            entry_i = int(row[1])
            exit_i = int(row[2])
            entry_dt = dates[entry_i] if 0 <= entry_i < len(dates) else dates[0]
            exit_dt = dates[exit_i] if 0 <= exit_i < len(dates) else dates[-1]
            reason = reason_map.get(row[8], "signal")

            # Overlay exit reason from StopManager
            if not exit_info.empty:
                m = exit_info[
                    (exit_info["stock_code"] == code) &
                    (pd.to_datetime(exit_info["exit_date"]) == exit_dt)
                ]
                if not m.empty:
                    reason = m.iloc[0]["exit_reason"]

            records.append({
                "stock_code": code,
                "entry_date": entry_dt,
                "exit_date": exit_dt,
                "entry_price": round(float(row[3]), 4),
                "exit_price": round(float(row[4]), 4),
                "shares": int(row[5]),
                "pnl": round(float(row[6]), 2),
                "return": round(float(row[7]), 4),
                "profit_pct": round(float(row[7]), 4),
                "exit_reason": reason,
            })
        return pd.DataFrame(records)

    def _fetch_prices(self, codes, start, end):
        return DataFetcher.get_close_price(codes, start, end, dividend_type="front")

    def _ensure_index(self, df):
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        return df.sort_index()

    def _build_entry_signals(self, selections, prices):
        entries = pd.DataFrame(False, index=prices.index, columns=prices.columns)
        for _, row in selections.iterrows():
            code = row["stock_code"]
            dt = pd.to_datetime(row["select_date"])
            if code not in entries.columns:
                continue
            if dt in entries.index:
                entries.loc[dt, code] = True
            else:
                m = entries.index >= dt
                if m.any():
                    entries.loc[entries.index[m][0], code] = True
        return entries

    def _log_results(self, m):
        logger.info("─" * 40)
        logger.info(f"累计: {m.get('cumulative_return',0):>+.2%}  年化: {m.get('annualized_return',0):>+.2%}")
        logger.info(f"回撤: {m.get('max_drawdown',0):>+.2%}  夏普: {m.get('sharpe_ratio',0):.2f}")
        logger.info(f"胜率: {m.get('win_rate',0):.1%}  交易: {m.get('total_trades',0)}")
        logger.info("─" * 40)

    def _empty_result(self):
        return {
            "equity_curve": pd.DataFrame(columns=["date", "equity", "drawdown"]),
            "trades": pd.DataFrame(),
            "metrics": {},
            "stop_config_summary": "",
            "selections": pd.DataFrame(),
            "stock_count": 0,
        }
