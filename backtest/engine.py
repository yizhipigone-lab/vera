"""VeraCore 回测引擎 — Numba JIT 加速，内置止盈止损判断。"""

import pandas as pd
import numpy as np
from typing import Dict, Optional, Any
from numba import njit

from backtest.stop_manager import StopManager
from backtest.metrics import MetricsCalculator
from core.data_fetcher import DataFetcher
from utils.logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════
# VeraCore Numba JIT 核心 — 内置止盈止损，不依赖外部 exit_np
# ═══════════════════════════════════════════════════════════════

@njit(cache=True)
def _simulate_core(
    price_np, entry_np,
    initial_capital, commission,
    min_buy_amount, max_buy_amount, lot_size, min_lots,
    cost_stop_enabled, cost_stop_threshold,
    trailing_enabled, trailing_activation, trailing_drawdown,
    ladder_enabled, ladder_profits, ladder_ratios, n_ladder,
    time_enabled, max_hold_days,
    cond_time_enabled, cond_time_days, cond_time_profit,
):
    n_dates = price_np.shape[0]
    n_stocks = price_np.shape[1]
    MAX_POS = 5000

    pos_code = np.full(MAX_POS, -1, dtype=np.int32)
    pos_shares = np.zeros(MAX_POS, dtype=np.float64)
    pos_entry_px = np.zeros(MAX_POS, dtype=np.float64)
    pos_entry_idx = np.full(MAX_POS, -1, dtype=np.int32)
    pos_high_px = np.zeros(MAX_POS, dtype=np.float64)      # 持仓期间最高价
    pos_ladder_done = np.zeros(MAX_POS, dtype=np.int32)     # 阶梯止盈已触发档位(bitmask)
    pos_count = 0

    cash = float(initial_capital)
    equity_arr = np.empty(n_dates, dtype=np.float64)
    max_trades = n_dates * n_stocks // 4 + 1000
    trades = np.empty((max_trades, 9), dtype=np.float64)
    trade_count = 0

    # reason codes: 3=cost_stop 4=trailing_stop 5=ladder_tp 6=time_stop 1=replace
    for i in range(n_dates):
        # ── 1. 卖出（内部止损判断）──
        p = 0
        while p < pos_count:
            ci = pos_code[p]
            if ci < 0:
                p += 1; continue
            xp = price_np[i, ci]
            if np.isnan(xp) or xp <= 0.0:
                p += 1; continue

            ep = pos_entry_px[p]
            pp = (xp - ep) / ep if ep > 0.0 else 0.0
            hp = max(pos_high_px[p], xp)
            pos_high_px[p] = hp
            hp_profit = (hp - ep) / ep if ep > 0.0 else 0.0
            hold_days = i - pos_entry_idx[p]

            triggered = -1  # reason code, -1 = none

            # 成本止损
            if cost_stop_enabled and pp <= cost_stop_threshold:
                triggered = 3
            # 移动止损
            if triggered < 0 and trailing_enabled and hp_profit >= trailing_activation:
                dd = (xp - hp) / hp if hp > 0.0 else 0.0
                if dd <= -trailing_drawdown:
                    triggered = 4
            # 阶梯止盈（任意档位触发，部分/全卖在下方处理）
            if triggered < 0 and ladder_enabled:
                mask = pos_ladder_done[p]
                for li in range(n_ladder):
                    if (mask >> li) & 1: continue
                    if pp >= ladder_profits[li]:
                        pos_ladder_done[p] = mask | (1 << li)
                        triggered = 5
                        break
            # 时间止盈（无条件）
            if triggered < 0 and time_enabled and hold_days >= max_hold_days:
                triggered = 6
            # 条件时间止盈：持仓N天后盈利≥X%清仓
            if triggered < 0 and cond_time_enabled and hold_days >= cond_time_days and pp >= cond_time_profit:
                triggered = 7

            if triggered >= 0:
                total_sh = pos_shares[p]
                # 阶梯止盈：根据触发档位的比例决定部分/全卖
                if triggered == 5:
                    # 找当前触发档位的比例
                    sell_ratio = 1.0
                    mask = pos_ladder_done[p]
                    for li in range(n_ladder):
                        if (mask >> li) & 1 and pp >= ladder_profits[li]:
                            sell_ratio = ladder_ratios[li]
                    if sell_ratio < 1.0:
                        # 部分卖出
                        sell_sh = int(total_sh * sell_ratio)
                        sell_sh = max((sell_sh // lot_size) * lot_size, lot_size)
                        if sell_sh < total_sh:
                            gross = sell_sh * xp * (1.0 - commission)
                            cash += gross
                            if trade_count < max_trades:
                                trades[trade_count, 0] = float(ci)
                                trades[trade_count, 1] = float(pos_entry_idx[p])
                                trades[trade_count, 2] = float(i)
                                trades[trade_count, 3] = ep
                                trades[trade_count, 4] = xp
                                trades[trade_count, 5] = float(sell_sh)
                                trades[trade_count, 6] = gross - sell_sh * ep
                                trades[trade_count, 7] = pp
                                trades[trade_count, 8] = 5.0
                                trade_count += 1
                            pos_shares[p] = total_sh - sell_sh
                            p += 1; continue  # 保留仓位，继续检查

                # 全卖（所有非部分卖出场景）
                gross = total_sh * xp * (1.0 - commission)
                cash += gross
                if trade_count < max_trades:
                    trades[trade_count, 0] = float(ci)
                    trades[trade_count, 1] = float(pos_entry_idx[p])
                    trades[trade_count, 2] = float(i)
                    trades[trade_count, 3] = ep
                    trades[trade_count, 4] = xp
                    trades[trade_count, 5] = total_sh
                    trades[trade_count, 6] = gross - total_sh * ep
                    trades[trade_count, 7] = pp
                    trades[trade_count, 8] = float(triggered)
                    trade_count += 1
                pos_count -= 1
                if p < pos_count:
                    pos_code[p] = pos_code[pos_count]
                    pos_shares[p] = pos_shares[pos_count]
                    pos_entry_px[p] = pos_entry_px[pos_count]
                    pos_high_px[p] = pos_high_px[pos_count]
                    pos_ladder_done[p] = pos_ladder_done[pos_count]
                continue
            p += 1

        # ── 2. 买入（同股先卖旧）──
        for ci in range(n_stocks):
            if entry_np[i, ci]:
                bp = price_np[i, ci]
                if np.isnan(bp) or bp <= 0.0: continue
                # 已持有同股票 → 卖出旧仓位
                for old_p in range(pos_count):
                    if pos_code[old_p] == ci:
                        os_sh = pos_shares[old_p]
                        os_ep = pos_entry_px[old_p]
                        os_ei = pos_entry_idx[old_p]
                        gross = os_sh * bp * (1.0 - commission)
                        cash += gross
                        os_pp = (bp - os_ep) / os_ep if os_ep > 0.0 else 0.0
                        if trade_count < max_trades:
                            trades[trade_count, 0] = float(ci)
                            trades[trade_count, 1] = float(os_ei)
                            trades[trade_count, 2] = float(i)
                            trades[trade_count, 3] = os_ep
                            trades[trade_count, 4] = bp
                            trades[trade_count, 5] = os_sh
                            trades[trade_count, 6] = gross - os_sh * os_ep
                            trades[trade_count, 7] = os_pp
                            trades[trade_count, 8] = 1.0  # 换股
                            trade_count += 1
                        pos_count -= 1
                        if old_p < pos_count:
                            pos_code[old_p] = pos_code[pos_count]
                            pos_shares[old_p] = pos_shares[pos_count]
                            pos_entry_px[old_p] = pos_entry_px[pos_count]
                            pos_high_px[old_p] = pos_high_px[pos_count]
                            pos_ladder_done[old_p] = pos_ladder_done[pos_count]
                        break
                # 买入新仓位
                buy_amount = min(cash, max_buy_amount)
                if buy_amount < min_buy_amount: continue
                raw_sh = int(buy_amount / bp)
                sh = (raw_sh // lot_size) * lot_size
                if sh < lot_size * min_lots: continue
                cost = sh * bp * (1.0 + commission)
                if cost <= cash and pos_count < MAX_POS:
                    cash -= cost
                    pos_code[pos_count] = ci
                    pos_shares[pos_count] = float(sh)
                    pos_entry_px[pos_count] = bp
                    pos_entry_idx[pos_count] = i
                    pos_high_px[pos_count] = bp
                    pos_ladder_done[pos_count] = 0
                    pos_count += 1

        # ── 3. 计算权益 ──
        pv = 0.0
        for p in range(pos_count):
            ci = pos_code[p]
            if ci >= 0:
                px = price_np[i, ci]
                if not np.isnan(px): pv += pos_shares[p] * px
        equity_arr[i] = cash + pv

    # ── 4. 最终权益（期末不平仓，按市值计入）──
    last = n_dates - 1
    pv = 0.0
    for p in range(pos_count):
        ci = pos_code[p]
        if ci >= 0:
            px = price_np[last, ci]
            if not np.isnan(px): pv += pos_shares[p] * px
    equity_arr[last] = cash + pv
    return equity_arr, trades[:trade_count]


# ═══════════════════════════════════════════════════════════════
# BacktestEngine — Python 包装层
# ═══════════════════════════════════════════════════════════════

class BacktestEngine:

    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        self.initial_capital = float(config.get("initial_capital", 100000.0))
        self.commission = float(config.get("commission", 0.0003))
        self.slippage = float(config.get("slippage", 0.001))
        ps = config.get("position_sizing", {})
        self.min_buy_amount = float(ps.get("min_buy_amount", 2000.0))
        self.max_buy_amount = float(ps.get("max_buy_amount", 10000.0))
        self.lot_size = int(ps.get("lot_size", 100))
        self.min_lots = int(ps.get("min_lots", 1))

    def run(self, selections, start_time="", end_time="", stop_config=None):
        if selections.empty: return self._empty_result()

        stop = stop_config or {}
        cost = stop.get("cost_stop", {})
        trail = stop.get("trailing_stop", {})
        ladder = stop.get("ladder_tp", {})
        time_s = stop.get("time_stop", {})
        cond_t = stop.get("cond_time_stop", {})

        codes = selections["stock_code"].unique().tolist()
        close = self._fetch_prices(codes, start_time, end_time)
        if close.empty: return self._empty_result()
        close = self._ensure_index(close)

        entries = self._build_entry_signals(selections, close)
        cols = close.columns.intersection(entries.columns)
        close = close[cols].ffill().bfill()
        entries = entries.reindex(index=close.index, columns=cols, fill_value=False)

        # 准备阶梯止盈数组
        levels = ladder.get("levels", [])
        lv = sorted(levels, key=lambda x: x.get("profit", 0))
        ladder_profits = np.array([lv[i]["profit"] for i in range(len(lv))], dtype=np.float64)
        ladder_ratios = np.array([lv[i]["sell_ratio"] for i in range(len(lv))], dtype=np.float64)

        logger.info("VeraCore: 资金=%s 每笔%s~%s元 %s股/手",
                     f"{self.initial_capital:,.0f}", f"{self.min_buy_amount:,.0f}",
                     f"{self.max_buy_amount:,.0f}", self.lot_size)

        t0 = pd.Timestamp.now()
        equity_arr, raw_trades = _simulate_core(
            close.values.astype(np.float64), entries.values,
            float(self.initial_capital), float(self.commission),
            float(self.min_buy_amount), float(self.max_buy_amount),
            int(self.lot_size), int(self.min_lots),
            cost.get("enabled", True), float(cost.get("threshold", -0.08)),
            trail.get("enabled", True), float(trail.get("activation", 0.05)),
            float(trail.get("drawdown", 0.03)),
            ladder.get("enabled", True), ladder_profits, ladder_ratios, len(lv),
            time_s.get("enabled", True), int(time_s.get("max_hold_days", 20)),
            cond_t.get("enabled", False), int(cond_t.get("days", 7)), float(cond_t.get("profit", 0.01)),
        )
        elapsed = (pd.Timestamp.now() - t0).total_seconds()
        logger.info("VeraCore: %s笔交易 %.2fs", len(raw_trades), elapsed)

        # 构建输出
        dates = close.index
        equity_curve = pd.DataFrame({"date": dates, "equity": equity_arr})
        equity_curve.set_index("date", inplace=True)
        peak = equity_curve["equity"].expanding().max()
        equity_curve["drawdown"] = (equity_curve["equity"] - peak) / peak
        equity_curve.reset_index(inplace=True)

        trades_df = self._build_trades(raw_trades, close.columns, dates)
        trades_df["entry_date"] = pd.to_datetime(trades_df["entry_date"])
        trades_df["exit_date"] = pd.to_datetime(trades_df["exit_date"])

        # 使用 StopManager 仅生成 exit_info 用于增强标签
        sm = StopManager(stop_config)
        _, exit_info = sm.compute_exit_signals(close, entries)

        # 叠加 StopManager 的退出原因（三重匹配）
        col_map = {c: i for i, c in enumerate(close.columns)}
        for idx, t in trades_df.iterrows():
            code = t["stock_code"]
            ed = t["entry_date"]
            xd = t["exit_date"]
            if t["exit_reason"] in ("换股卖出",):
                if not exit_info.empty:
                    m = exit_info[
                        (exit_info["stock_code"] == code) &
                        (pd.to_datetime(exit_info["entry_date"]) == ed) &
                        (pd.to_datetime(exit_info["exit_date"]) == xd)
                    ]
                    if not m.empty:
                        trades_df.at[idx, "exit_reason"] = m.iloc[0]["exit_reason"]

        metrics = MetricsCalculator.compute_all(equity_curve, trades_df, self.initial_capital)
        self._log_results(metrics)

        return {
            "equity_curve": equity_curve, "trades": trades_df, "metrics": metrics,
            "stop_config_summary": sm.get_config_summary(),
            "selections": selections, "stock_count": len(cols),
        }

    def _build_trades(self, raw, columns, dates):
        if len(raw) == 0: return pd.DataFrame()
        reason_map = {1.0: "换股卖出", 3.0: "成本止损", 4.0: "移动止损",
                      5.0: "阶梯止盈", 6.0: "时间止损", 7.0: "条件时间止盈"}
        col_map = {c: i for i, c in enumerate(columns)}
        inv_col = {i: c for c, i in col_map.items()}
        records = []
        for row in raw:
            ci = int(row[0]); code = inv_col.get(ci, str(ci))
            ei = int(row[1]); xi = int(row[2])
            ed = dates[ei] if 0 <= ei < len(dates) else dates[0]
            xd = dates[xi] if 0 <= xi < len(dates) else dates[-1]
            ep = round(float(row[3]), 4); xp = round(float(row[4]), 4)
            sh = int(row[5])
            records.append({
                "stock_code": code, "entry_date": ed, "exit_date": xd,
                "entry_price": ep, "exit_price": xp, "shares": sh,
                "entry_amount": round(ep * sh, 2), "exit_amount": round(xp * sh, 2),
                "pnl": round(float(row[6]), 2), "return": round(float(row[7]), 4),
                "profit_pct": round(float(row[7]), 4),
                "exit_reason": reason_map.get(row[8], "换股卖出"),
            })
        return pd.DataFrame(records)

    def _fetch_prices(self, codes, start, end):
        return DataFetcher.get_close_price(codes, start, end, dividend_type="front")

    def _ensure_index(self, df):
        if not isinstance(df.index, pd.DatetimeIndex): df.index = pd.to_datetime(df.index)
        return df.sort_index()

    def _build_entry_signals(self, selections, prices):
        entries = pd.DataFrame(False, index=prices.index, columns=prices.columns)
        for _, row in selections.iterrows():
            code = row["stock_code"]; dt = pd.to_datetime(row["select_date"])
            if code not in entries.columns: continue
            if dt in entries.index: entries.loc[dt, code] = True
            else:
                m = entries.index >= dt
                if m.any(): entries.loc[entries.index[m][0], code] = True
        return entries

    def _log_results(self, m):
        logger.info("-" * 40)
        logger.info("累计:%+.2f%% 年化:%+.2f%% 回撤:%+.2f%% 夏普:%.2f",
                     m.get('cumulative_return',0)*100, m.get('annualized_return',0)*100,
                     m.get('max_drawdown',0)*100, m.get('sharpe_ratio',0))
        logger.info("胜率:%.1f%% 交易:%s", m.get('win_rate',0)*100, m.get('total_trades',0))
        logger.info("-" * 40)

    def _empty_result(self):
        return {"equity_curve": pd.DataFrame(columns=["date","equity","drawdown"]),
                "trades": pd.DataFrame(), "metrics": {}, "stop_config_summary": "",
                "selections": pd.DataFrame(), "stock_count": 0}
