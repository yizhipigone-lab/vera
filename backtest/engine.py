"""VeraCore 回测引擎 — 纯Python，内置OHLC止盈止损判断。"""

import pandas as pd
import numpy as np
from typing import Dict, Optional, Any
from backtest.stop_manager import StopManager
from backtest.metrics import MetricsCalculator
from backtest.ladder_tp import compute_ladder_trigger, compute_ladder_sell_ratio
from core.data_fetcher import DataFetcher
from core.stock_filter import get_cached_info
from utils.logger import get_logger

logger = get_logger(__name__)

# ENGINE VERSION: increment to bust Python .pyc cache
ENGINE_VERSION = "v3.3-limit-up-filter-20260605"


# ═══════════════════════════════════════════════════════════════
# VeraCore 核心回测循环 — 内置OHLC止盈止损判断
# 优先级: 成本止损 > 阶梯止盈 > 移动止损/止盈 > 时间止损
# 执行价格:
#   成本止损 → stop_price (ep*(1+threshold)) 简化模式
#   阶梯止盈 → ladder_price (ep*(1+profit))  简化模式
#   移动止损 → Close (回撤检测也改用Close)
#   其他     → Close
# ═══════════════════════════════════════════════════════════════

def _simulate_core_v3(
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
):
    n_dates = price_np.shape[0]
    n_stocks = price_np.shape[1]
    MAX_POS = 5000

    pos_code = np.full(MAX_POS, -1, dtype=np.int32)
    pos_shares = np.zeros(MAX_POS, dtype=np.float64)
    pos_entry_px = np.zeros(MAX_POS, dtype=np.float64)
    pos_entry_idx = np.full(MAX_POS, -1, dtype=np.int32)
    pos_high_px = np.zeros(MAX_POS, dtype=np.float64)      # 持仓期间最高收盘价
    pos_high_hi = np.zeros(MAX_POS, dtype=np.float64)      # 持仓期间最高价(来自high_np)
    pos_ladder_done = np.zeros(MAX_POS, dtype=np.int32)     # 阶梯止盈已触发档位(bitmask)
    pos_count = 0

    cash = float(initial_capital)
    equity_arr = np.empty(n_dates, dtype=np.float64)
    max_trades = n_dates * n_stocks // 4 + 1000
    trades = np.empty((max_trades, 9), dtype=np.float64)
    trade_count = 0

    # P1-8: trades 数组动态扩容（替换原静默截断）
    def _grow_trades():
        nonlocal trades, max_trades
        new_max = max_trades * 2
        new_trades = np.empty((new_max, 9), dtype=np.float64)
        new_trades[:trade_count] = trades[:trade_count]
        trades = new_trades
        max_trades = new_max
        logger.warning("trades 数组扩容至 %d (n_dates=%d n_stocks=%d)", max_trades, n_dates, n_stocks)

    # reason codes: 3=cost_stop 4=trailing_stop 5=ladder_tp 6=time_stop 7=cond_time 8=trailing_tp 9=time_tp 10=first_day 1=replace
    for i in range(n_dates):
        # ── 1. 卖出（内部止损判断）──
        p = 0
        while p < pos_count:
            ci = pos_code[p]
            if ci < 0:
                p += 1; continue
            xp = price_np[i, ci]
            # P1-3: 停牌/退市处理（tradable_np 来自原始未 ffill 价）
            if tradable_np is not None and ci < tradable_np.shape[1] and not tradable_np[i, ci]:
                if last_tradable_idx is not None and last_tradable_idx[ci] >= 0 and i > last_tradable_idx[ci]:
                    # 退市：之后再无可交易 bar → 强制平仓（按 ffill 最后已知价）
                    total_sh = pos_shares[p]
                    ep_d = pos_entry_px[p]
                    sell_price = xp if xp > 0 else ep_d
                    sell_eff = sell_price * (1.0 - slippage)
                    gross = total_sh * sell_eff * (1.0 - commission - stamp_tax)
                    cash += gross
                    if trade_count >= max_trades:
                        _grow_trades()
                    trades[trade_count, 0] = float(ci)
                    trades[trade_count, 1] = float(pos_entry_idx[p])
                    trades[trade_count, 2] = float(i)
                    trades[trade_count, 3] = ep_d
                    trades[trade_count, 4] = sell_price
                    trades[trade_count, 5] = total_sh
                    trades[trade_count, 6] = gross - total_sh * ep_d
                    trades[trade_count, 7] = (sell_price - ep_d) / ep_d if ep_d > 0.0 else 0.0
                    trades[trade_count, 8] = 11.0  # 退市
                    trade_count += 1
                    pos_count -= 1
                    if p < pos_count:
                        pos_code[p] = pos_code[pos_count]
                        pos_shares[p] = pos_shares[pos_count]
                        pos_entry_px[p] = pos_entry_px[pos_count]
                        pos_entry_idx[p] = pos_entry_idx[pos_count]
                        pos_high_px[p] = pos_high_px[pos_count]
                        pos_high_hi[p] = pos_high_hi[pos_count]
                        pos_ladder_done[p] = pos_ladder_done[pos_count]
                    continue
                # 临时停牌：跳过卖出检查，按 ffill 价 mark-to-market（equity 段处理）
                p += 1; continue
            if np.isnan(xp) or xp <= 0.0:
                p += 1; continue

            # T+1：当日买入不可当日卖出（A股交易规则）
            if (i // bpday) == (pos_entry_idx[p] // bpday):
                # 同在一天，只更新最高价，不检查卖出
                if high_np is not None:
                    hi = high_np[i, ci]
                    if hi > pos_high_hi[p]:
                        pos_high_hi[p] = hi
                if xp > pos_high_px[p]:
                    pos_high_px[p] = xp
                p += 1; continue

            ep = pos_entry_px[p]
            pp = (xp - ep) / ep if ep > 0.0 else 0.0
            hp = max(pos_high_px[p], xp)
            pos_high_px[p] = hp
            hp_profit = (hp - ep) / ep if ep > 0.0 else 0.0

            # 获取当前bar的OHLC价格（用于检测日内触发）
            hi = high_np[i, ci] if high_np is not None else xp
            lo = low_np[i, ci] if low_np is not None else xp
            hi_pp = (hi - ep) / ep if ep > 0.0 else 0.0
            lo_pp = (lo - ep) / ep if ep > 0.0 else 0.0

            # 跟踪实际最高价（用于首日规则和移动止损峰值）
            if high_np is not None:
                if hi > pos_high_hi[p]:
                    pos_high_hi[p] = hi
            peak_hi = pos_high_hi[p] if pos_high_hi[p] > 0 else ep
            peak_hi_profit = (peak_hi - ep) / ep if ep > 0.0 else 0.0

            hold_days = i - pos_entry_idx[p]

            triggered = -1  # reason code, -1 = none

            ladder_sell_ratio = 0.0  # 本 bar 阶梯止盈的卖出比例（默认 0 = 不触发）

            # 成本止损：检查Low是否跌破止损线（最高优先级）
            if cost_stop_enabled and lo_pp <= cost_stop_threshold:
                triggered = 3
            # 阶梯止盈：检查High是否触及目标档位（主动策略优先）
            # BUG-5 修复: 旧实现命中即 break，同 bar 多档只置位一档；
            #   现改为循环置位所有满足的档位，并由 sell_ratio 函数区分新旧触发
            if triggered < 0 and ladder_enabled:
                prev_mask = int(pos_ladder_done[p])
                new_mask = compute_ladder_trigger(prev_mask, hi_pp, ladder_profits[:n_ladder])
                if new_mask != prev_mask:
                    pos_ladder_done[p] = new_mask
                    ladder_sell_ratio = compute_ladder_sell_ratio(
                        prev_mask, new_mask,
                        ladder_profits[:n_ladder], ladder_ratios[:n_ladder],
                    )
                    triggered = 5
            # 移动止损/止盈：High激活 + Close回撤触发（次级保护）
            if triggered < 0 and trailing_enabled and peak_hi_profit >= trailing_activation:
                dd = (xp - peak_hi) / peak_hi if peak_hi > 0.0 else 0.0
                if dd <= -trailing_drawdown:
                    triggered = 8 if pp > 0 else 4  # 8=移动止盈 4=移动止损
            # 时间止损/止盈 (根据盈亏区分)
            if triggered < 0 and time_enabled and hold_days >= max_hold_days:
                triggered = 9 if pp > 0 else 6  # 9=时间止盈 6=时间止损
            # 条件时间止盈：持仓N天后，当前bar的High达到盈利目标%清仓
            if triggered < 0 and cond_time_enabled and hold_days >= cond_time_days and hi_pp >= cond_time_profit:
                triggered = 7
            # 首日未达标：第一可交易日收盘时，持仓期间最高价涨幅<目标 → 强制卖出
            if triggered < 0 and first_day_enabled:
                current_day = i // bpday
                entry_day = pos_entry_idx[p] // bpday
                # 第一个可交易日的最后一根bar（T+1下即入场次日收盘）
                if current_day == entry_day + 1 and (i % bpday) == bpday - 1:
                    day_high = pos_high_hi[p] if pos_high_hi[p] > 0 else (high_np[i, ci] if high_np is not None else xp)
                    if day_high > 0 and ep > 0:
                        day1_return = (day_high - ep) / ep
                        if day1_return < first_day_target:
                            triggered = 10  # 首日未达标

            if triggered >= 0:
                total_sh = pos_shares[p]
                # ── 计算执行价格（根据触发类型）──
                # 成本止损: stop_price 简化模式 (ep * (1 + threshold))
                # 阶梯止盈: ladder_price 简化模式 (ep * (1 + profit))
                # 其他: Close 价格
                if triggered == 3:
                    # 硬止损【简化模式】：使用stop_price执行
                    # P1-1: 跌停保护 — 跳空低开时取 min(stop_price, open)
                    stop_price = ep * (1.0 + cost_stop_threshold)
                    if open_np is not None:
                        op = open_np[i, ci]
                        if not np.isnan(op) and op < stop_price:
                            stop_price = op
                    sell_price = stop_price
                    actual_ret = (sell_price - ep) / ep if ep > 0.0 else 0.0
                elif triggered == 5:
                    # 阶梯止盈【简化模式】：使用ladder_price执行
                    # BUG-5 修复: 旧实现取"已触发且 hi_pp 满足"的最后一档 profit
                    #   （与 sell_ratio 同一 bug），现改为取最大值。
                    # 卖出价取 hi 实际到达过的最高档位的 profit（保守估计成交价）。
                    tp_profit = 0.0
                    cur_mask = int(pos_ladder_done[p])  # 触发后已写回 new_mask
                    for li in range(n_ladder):
                        if (cur_mask >> li) & 1 and hi_pp >= ladder_profits[li]:
                            if ladder_profits[li] > tp_profit:
                                tp_profit = ladder_profits[li]
                    sell_price = ep * (1.0 + tp_profit)
                    actual_ret = tp_profit
                else:
                    # 移动止损/止盈、时间止损/止盈等使用Close
                    sell_price = xp
                    actual_ret = (sell_price - ep) / ep if ep > 0.0 else 0.0

                # 阶梯止盈：根据本次新触发档位的比例决定部分/全卖
                # BUG-5 修复: 旧实现 sell_ratio = ladder_ratios[li] 每次覆盖，
                #   最终只取最后一档；现改为累加"本 bar 新触发"档位的比例
                if triggered == 5:
                    sell_ratio = ladder_sell_ratio
                    if sell_ratio < 1.0:
                        # 部分卖出
                        sell_sh = int(total_sh * sell_ratio)
                        sell_sh = max((sell_sh // lot_size) * lot_size, lot_size)
                        if sell_sh < total_sh:
                            # C1 修复: 卖出叠加滑点 + 印花税 (eff_*=0 时等价于旧版)
                            sell_eff = sell_price * (1.0 - slippage)
                            gross = sell_sh * sell_eff * (1.0 - commission - stamp_tax)
                            cash += gross
                            if trade_count >= max_trades:
                                _grow_trades()
                            trades[trade_count, 0] = float(ci)
                            trades[trade_count, 1] = float(pos_entry_idx[p])
                            trades[trade_count, 2] = float(i)
                            trades[trade_count, 3] = ep
                            trades[trade_count, 4] = sell_price
                            trades[trade_count, 5] = float(sell_sh)
                            trades[trade_count, 6] = gross - sell_sh * ep
                            trades[trade_count, 7] = actual_ret
                            trades[trade_count, 8] = 5.0
                            trade_count += 1
                            pos_shares[p] = total_sh - sell_sh
                            p += 1; continue  # 保留仓位，继续检查

                # 全卖（所有非部分卖出场景）
                # C1 修复: 卖出叠加滑点 + 印花税 (eff_*=0 时等价于旧版)
                sell_eff = sell_price * (1.0 - slippage)
                gross = total_sh * sell_eff * (1.0 - commission - stamp_tax)
                cash += gross
                if trade_count >= max_trades:
                    _grow_trades()
                trades[trade_count, 0] = float(ci)
                trades[trade_count, 1] = float(pos_entry_idx[p])
                trades[trade_count, 2] = float(i)
                trades[trade_count, 3] = ep
                trades[trade_count, 4] = sell_price
                trades[trade_count, 5] = total_sh
                trades[trade_count, 6] = gross - total_sh * ep
                trades[trade_count, 7] = actual_ret
                trades[trade_count, 8] = float(triggered)
                trade_count += 1
                pos_count -= 1
                if p < pos_count:
                    pos_code[p] = pos_code[pos_count]
                    pos_shares[p] = pos_shares[pos_count]
                    pos_entry_px[p] = pos_entry_px[pos_count]
                    pos_entry_idx[p] = pos_entry_idx[pos_count]
                    pos_high_px[p] = pos_high_px[pos_count]
                    pos_high_hi[p] = pos_high_hi[pos_count]
                    pos_ladder_done[p] = pos_ladder_done[pos_count]
                continue
            p += 1

        # ── 2. 买入（同股先卖旧）──
        for ci in range(n_stocks):
            # P1-2: T+1 开盘买入 — 有 open_np 时，信号日次日开盘成交
            #   entry_np[i, ci]=True → 信号日 i，次日 i+1 开盘买入
            #   无 open_np 时（run_cached 批量路径）→ 旧行为：信号日收盘成交
            if open_np is not None:
                # T+1: 在 bar i 检查 i-1 的信号，以 open[i] 买入
                if i < 1 or not entry_np[i - 1, ci]:
                    continue
                bp = open_np[i, ci]
                if np.isnan(bp) or bp <= 0.0:
                    # 次日停牌，无法成交，跳过该信号
                    continue
                entry_i = i  # 实际买入 bar = i（信号日 i-1 的次日）
            else:
                # 旧路径：信号日收盘成交（run_cached / 无 Open 数据）
                if not entry_np[i, ci]:
                    continue
                bp = price_np[i, ci]
                if np.isnan(bp) or bp <= 0.0:
                    continue
                entry_i = i
            # 已持有同股票 → 卖出旧仓位
            for old_p in range(pos_count):
                if pos_code[old_p] == ci:
                    os_sh = pos_shares[old_p]
                    os_ep = pos_entry_px[old_p]
                    os_ei = pos_entry_idx[old_p]
                    gross = os_sh * bp * (1.0 - commission)
                    cash += gross
                    os_pp = (bp - os_ep) / os_ep if os_ep > 0.0 else 0.0
                    if trade_count >= max_trades:
                        _grow_trades()
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
                        pos_entry_idx[old_p] = pos_entry_idx[pos_count]
                        pos_high_px[old_p] = pos_high_px[pos_count]
                        pos_high_hi[old_p] = pos_high_hi[pos_count]
                        pos_ladder_done[old_p] = pos_ladder_done[pos_count]
                    break
            # 买入新仓位
            buy_amount = min(cash, max_buy_amount)
            if buy_amount < min_buy_amount: continue
            raw_sh = int(buy_amount / bp)
            sh = (raw_sh // lot_size) * lot_size
            if sh < lot_size * min_lots: continue
            # C1 修复: 买入叠加滑点 (eff_slippage=0 时等价于旧版)
            bp_eff = bp * (1.0 + slippage)
            cost = sh * bp_eff * (1.0 + commission)
            if cost <= cash and pos_count < MAX_POS:
                cash -= cost
                pos_code[pos_count] = ci
                pos_shares[pos_count] = float(sh)
                pos_entry_px[pos_count] = bp
                pos_entry_idx[pos_count] = entry_i  # P1-2: T+1 时 entry_i=i（信号日次日）
                pos_high_px[pos_count] = bp
                pos_high_hi[pos_count] = bp
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

    BARS_PER_DAY = {"1d": 1, "1w": 1, "5m": 48}
    # P1-4: periods_per_year 用独立映射，避免 1w 被 *252 高估 4.8 倍（1w 应为 52 周/年）
    PERIODS_PER_YEAR = {"1d": 252, "1w": 52, "5m": 48 * 252}

    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        self.initial_capital = float(config.get("initial_capital", 1000000.0))
        self.commission = float(config.get("commission", 0.0003))
        self.slippage = float(config.get("slippage", 0.001))
        self.stamp_tax = float(config.get("stamp_tax", 0.0005))  # A股卖出单边
        # P0-5: 默认 True — 含印花税+滑点的真实成本（老脚本可显式传 False 复现零成本基线）
        self.realistic_costs = bool(config.get("enable_realistic_costs", True))
        self.period = config.get("period", "1d")
        self.bars_per_day = self.BARS_PER_DAY.get(self.period, 1)
        ps = config.get("position_sizing", {})
        self.min_buy_amount = float(ps.get("min_buy_amount", 2000.0))
        self.max_buy_amount = float(ps.get("max_buy_amount", 20000.0))
        self.lot_size = int(ps.get("lot_size", 100))
        self.min_lots = int(ps.get("min_lots", 1))

        # C1 修复: 实际生效的费率 (兼容层)
        # 关闭时用 0 覆盖, 确保绝对不破坏老脚本行为
        if not self.realistic_costs:
            self.eff_commission = self.commission
            self.eff_slippage = 0.0
            self.eff_stamp_tax = 0.0
        else:
            self.eff_commission = self.commission
            self.eff_slippage = self.slippage
            self.eff_stamp_tax = self.stamp_tax

    def run(self, selections, start_time="", end_time="", stop_config=None):
        if selections.empty: return self._empty_result()

        stop = stop_config or {}
        cost = stop.get("cost_stop", {})
        trail = stop.get("trailing_stop", {})
        ladder = stop.get("ladder_tp", {})
        time_s = stop.get("time_stop", {})
        cond_t = stop.get("cond_time_stop", {})
        first_day = stop.get("first_day", {})

        codes = selections["stock_code"].unique().tolist()

        # 始终获取完整OHLC数据（不再仅首日规则）
        # P0-1: fill_data=False — 让停牌以 NaN 显式暴露，避免 TDX 源头前向填充掩盖前视偏差
        kline = DataFetcher.get_kline(codes, start_time, end_time, dividend_type="front", period=self.period, fill_data=False)
        if not kline or "Close" not in kline:
            return self._empty_result()

        close = self._ensure_index(kline["Close"])
        high_df_raw = kline.get("High")
        low_df_raw = kline.get("Low")
        open_df_raw = kline.get("Open")  # P1-1/P1-2: Open 列
        high_df = self._ensure_index(high_df_raw) if high_df_raw is not None else None
        low_df = self._ensure_index(low_df_raw) if low_df_raw is not None else None
        open_df = self._ensure_index(open_df_raw) if open_df_raw is not None else None

        entries = self._build_entry_signals(selections, close)
        # 统一列对齐：close ∩ entries ∩ high ∩ low（open 不参与交集，缺失则回退 close）
        cols = sorted(close.columns.intersection(entries.columns))
        if high_df is not None:
            cols = sorted(set(cols) & set(high_df.columns))
        if low_df is not None:
            cols = sorted(set(cols) & set(low_df.columns))

        # P1-3: 保留原始价（含停牌 NaN）用于退市检测；close 用 ffill 做 mark-to-market
        close_raw = close.reindex(index=close.index, columns=cols)
        close = close_raw.ffill()
        entries = entries.reindex(index=close.index, columns=cols, fill_value=False)
        idx = close.index
        high_np = high_df.reindex(index=idx, columns=cols).ffill().values.astype(np.float64) if high_df is not None else None
        low_np = low_df.reindex(index=idx, columns=cols).ffill().values.astype(np.float64) if low_df is not None else None
        # P1-1/P1-2: Open 不做 ffill — 停牌日 open=NaN 应保留，让 T+1 买入自然跳过
        open_np = open_df.reindex(index=idx, columns=cols).values.astype(np.float64) if open_df is not None else None

        # tradable 掩码 + 退市检测（基于原始未 ffill 价）
        tradable_np = close_raw.notna().values.astype(np.bool_)
        last_tradable_idx = np.full(close.shape[1], -1, dtype=np.int64)
        for _ci in range(close.shape[1]):
            _idxs = np.where(tradable_np[:, _ci])[0]
            if _idxs.size:
                last_tradable_idx[_ci] = int(_idxs[-1])

        # 准备阶梯止盈数组
        levels = ladder.get("levels", [])
        lv = sorted(levels, key=lambda x: x.get("profit", 0))
        ladder_profits = np.array([lv[i]["profit"] for i in range(len(lv))], dtype=np.float64)
        ladder_ratios = np.array([lv[i]["sell_ratio"] for i in range(len(lv))], dtype=np.float64)

        cond_profit_pct = cond_t.get("profit", 0.01)
        logger.info("VeraCore %s: 资金=%s 每笔%s~%s元 %s股/手 时间=%s天 条件=%s天/%.1f%% %s stocks",
                     ENGINE_VERSION,
                     f"{self.initial_capital:,.0f}", f"{self.min_buy_amount:,.0f}",
                     f"{self.max_buy_amount:,.0f}", self.lot_size,
                     time_s.get("max_hold_days", "?"), cond_t.get("days", "?"), cond_profit_pct * 100,
                     len(codes))

        mhd = int(time_s.get("max_hold_days", 20))
        bpday = self.bars_per_day
        mhd_scaled = mhd * bpday
        ctd = int(cond_t.get("days", 7))
        ctd_scaled = ctd * bpday
        fd_bars = bpday - 1 if bpday > 1 else 1
        logger.info("ENGINE_DEBUG max_hold_days=%d(scaled=%d) time_enabled=%s period=%s bpday=%d",
                     mhd, mhd_scaled, time_s.get("enabled", True), self.period, bpday)
        t0 = pd.Timestamp.now()
        entries = self._filter_limit_up(entries, close)
        equity_arr, raw_trades = _simulate_core_v3(
            close.values.astype(np.float64), entries.values,
            float(self.initial_capital),
            float(self.eff_commission),
            float(self.min_buy_amount), float(self.max_buy_amount),
            int(self.lot_size), int(self.min_lots),
            cost.get("enabled", True), float(cost.get("threshold", -0.12)),
            trail.get("enabled", True), float(trail.get("activation", 0.08)),
            float(trail.get("drawdown", 0.05)),
            ladder.get("enabled", True), ladder_profits, ladder_ratios, len(lv),
            time_s.get("enabled", True), mhd_scaled,
            cond_t.get("enabled", False), ctd_scaled, float(cond_t.get("profit", 0.01)),
            first_day_enabled=first_day.get("enabled", False),
            first_day_target=float(first_day.get("target", 0.03)),
            first_day_n_bars=fd_bars,
            high_np=high_np,
            low_np=low_np,
            bpday=bpday,
            slippage=float(self.eff_slippage),
            stamp_tax=float(self.eff_stamp_tax),
            tradable_np=tradable_np,
            last_tradable_idx=last_tradable_idx,
            open_np=open_np,
        )
        elapsed = (pd.Timestamp.now() - t0).total_seconds()
        # DEBUG: check raw bar differences from Numba output directly
        raw_holds = [int(row[2]) - int(row[1]) for row in raw_trades]
        logger.info("VeraCore: %s笔交易 %.2fs RAW_MAX_HOLD=%d", len(raw_trades), elapsed, max(raw_holds) if raw_holds else 0)

        # 构建输出
        dates = close.index
        equity_curve = pd.DataFrame({"date": dates, "equity": equity_arr})
        equity_curve.set_index("date", inplace=True)
        peak = equity_curve["equity"].expanding().max()
        equity_curve["drawdown"] = (equity_curve["equity"] - peak) / peak
        equity_curve.reset_index(inplace=True)

        trades_df = self._build_trades(raw_trades, close.columns, dates, bpday)
        trades_df["entry_date"] = pd.to_datetime(trades_df["entry_date"])
        trades_df["exit_date"] = pd.to_datetime(trades_df["exit_date"])

        # 使用 StopManager 仅生成 exit_info 用于增强标签
        sm = StopManager(stop_config)
        high_prices_sm = pd.DataFrame(high_np, index=close.index, columns=close.columns) if high_np is not None else None
        low_prices_sm = pd.DataFrame(low_np, index=close.index, columns=close.columns) if low_np is not None else None
        _, exit_info = sm.compute_exit_signals(close, entries, high_prices_sm, low_prices_sm)

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

        metrics = MetricsCalculator.compute_all(equity_curve, trades_df, self.initial_capital,
                                                  periods_per_year=self.PERIODS_PER_YEAR.get(self.period, self.bars_per_day * 252))
        self._log_results(metrics)

        return {
            "equity_curve": equity_curve, "trades": trades_df, "metrics": metrics,
            "stop_config_summary": sm.get_config_summary(),
            "selections": selections, "stock_count": len(cols),
        }

    def run_cached(self, close, entries, high_np, low_np, stop_config, selections,
                   ladder_profits, ladder_ratios, n_ladder, skip_sm=False):
        """用预取数据运行回测，跳过K线获取（用于批量优化）"""
        stop = stop_config or {}
        cost = stop.get("cost_stop", {})
        trail = stop.get("trailing_stop", {})
        time_s = stop.get("time_stop", {})
        cond_t = stop.get("cond_time_stop", {})
        first_day = stop.get("first_day", {})

        mhd = int(time_s.get("max_hold_days", 20))
        bpday = self.bars_per_day
        mhd_scaled = mhd * bpday
        ctd = int(cond_t.get("days", 7))
        ctd_scaled = ctd * bpday
        fd_bars = bpday - 1 if bpday > 1 else 1

        entries = self._filter_limit_up(entries, close)
        equity_arr, raw_trades = _simulate_core_v3(
            close.values.astype(np.float64), entries.values,
            float(self.initial_capital), float(self.eff_commission),
            float(self.min_buy_amount), float(self.max_buy_amount),
            int(self.lot_size), int(self.min_lots),
            cost.get("enabled", True), float(cost.get("threshold", -0.12)),
            trail.get("enabled", True), float(trail.get("activation", 0.08)),
            float(trail.get("drawdown", 0.05)),
            stop.get("ladder_tp", {}).get("enabled", True), ladder_profits, ladder_ratios, n_ladder,
            time_s.get("enabled", True), mhd_scaled,
            cond_t.get("enabled", False), ctd_scaled, float(cond_t.get("profit", 0.01)),
            first_day_enabled=first_day.get("enabled", False),
            first_day_target=float(first_day.get("target", 0.03)),
            first_day_n_bars=fd_bars,
            high_np=high_np, low_np=low_np, bpday=bpday,
            slippage=float(self.eff_slippage), stamp_tax=float(self.eff_stamp_tax),
        )

        dates = close.index
        equity_curve = pd.DataFrame({"date": dates, "equity": equity_arr})
        equity_curve.set_index("date", inplace=True)
        peak = equity_curve["equity"].expanding().max()
        equity_curve["drawdown"] = (equity_curve["equity"] - peak) / peak
        equity_curve.reset_index(inplace=True)

        trades_df = self._build_trades(raw_trades, close.columns, dates, bpday)
        trades_df["entry_date"] = pd.to_datetime(trades_df["entry_date"])
        trades_df["exit_date"] = pd.to_datetime(trades_df["exit_date"])

        # StopManager for labels (skip in batch mode for speed)
        if not skip_sm:
            sm = StopManager(stop_config)
            high_sm = pd.DataFrame(high_np, index=close.index, columns=close.columns) if high_np is not None else None
            low_sm = pd.DataFrame(low_np, index=close.index, columns=close.columns) if low_np is not None else None
            _, exit_info = sm.compute_exit_signals(close, entries, high_sm, low_sm)

            for idx, t in trades_df.iterrows():
                if t["exit_reason"] in ("换股卖出",):
                    if not exit_info.empty:
                        m = exit_info[
                            (exit_info["stock_code"] == t["stock_code"]) &
                            (pd.to_datetime(exit_info["entry_date"]) == t["entry_date"]) &
                            (pd.to_datetime(exit_info["exit_date"]) == t["exit_date"])
                        ]
                        if not m.empty:
                            trades_df.at[idx, "exit_reason"] = m.iloc[0]["exit_reason"]

        metrics = MetricsCalculator.compute_all(equity_curve, trades_df, self.initial_capital,
                                                  periods_per_year=self.PERIODS_PER_YEAR.get(self.period, self.bars_per_day * 252))
        # C2 修复: 返回真实 equity_curve (以前只返回 cumret, 强制调用方用 trades 重建, 有前视偏差)
        return {
            "metrics": metrics,
            "trades": trades_df,
            "cumulative_return": metrics.get("cumulative_return", 0),
            "equity_curve": equity_curve,
        }

    def _build_trades(self, raw, columns, dates, bpday=1):
        if len(raw) == 0: return pd.DataFrame()
        reason_map = {1.0: "换股卖出", 3.0: "成本止损",
                      4.0: "移动止损", 8.0: "移动止盈",
                      5.0: "阶梯止盈",
                      6.0: "时间止损", 9.0: "时间止盈",
                      7.0: "cond_time_stop",
                      10.0: "首日未达标",
                      11.0: "退市"}
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
                "hold_days": max(1, (xi - ei) // bpday) if bpday > 1 else (xi - ei),
            })
        return pd.DataFrame(records)

    def _fetch_prices(self, codes, start, end):
        return DataFetcher.get_close_price(codes, start, end, dividend_type="front", period=self.period)

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
                # 寻找信号日之后的第一个bar
                m = entries.index >= dt
                if m.any():
                    first_bar = entries.index[m][0]
                    # 尾盘买入：取信号日最后一根bar（1d不变，5m→15:00）
                    day_mask = entries.index.normalize() == first_bar.normalize()
                    if day_mask.any():
                        entries.loc[entries.index[day_mask][-1], code] = True
                    else:
                        entries.loc[first_bar, code] = True
        return entries

    def _filter_limit_up(self, entries, close):
        """过滤涨停板买入信号: A股涨停日无法买入。将涨停日的entry设为False。"""
        if not isinstance(entries, pd.DataFrame):
            return entries
        result = entries.copy()
        prev_close = close.shift(1)
        for col in entries.columns:
            limit_ratio = 0.10  # 默认主板
            col_str = str(col)
            if col_str.startswith('688'): limit_ratio = 0.20
            elif col_str.startswith('300') or col_str.startswith('301'): limit_ratio = 0.20
            else:
                # P0-3: 用 TDX IsSTGP 真实判定 ST（原 'ST' in col_str 对纯代码恒 False）
                info = get_cached_info(col_str)
                if str(info.get('IsSTGP', '0')) == '1':
                    limit_ratio = 0.05
            limit_price = prev_close[col] * (1.0 + limit_ratio)
            # 接近涨停价(0.3%容差)则取消买入信号
            is_limit_up = close[col] >= limit_price * 0.997
            result.loc[is_limit_up, col] = False
        filtered = (entries.sum().sum() - result.sum().sum())
        if filtered > 0:
            logger.info(f"涨停过滤: 移除 {int(filtered)} 个涨停买入信号")
        return result

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
