"""多维度止损止盈管理器 — 叠加模式，基于 OHLC 日内价格检测。

四个机制全部独立检查（基于High/Low检测日内触发，收盘价执行）：

    1. 成本止损    — Low触及止损线，全仓卖出
    2. 阶梯止盈    — High触及 Z₁%→卖P₁%, Z₂%→卖P₂%, ...（分批）
    3. 移动止损    — High盈利>激活% 且 盘中Low触及回撤线, 按回撤线价全仓卖出 (2026-07-05 v3)
    4. 时间止盈    — 持仓天数 ≥ N 天，全仓卖出

默认优先级 (priority=stop_first, 历史): 成本止损 > 阶梯止盈 > 移动 > 时间 (成本止损为最高安全优先级, 阶梯为主动止盈策略)
priority=ladder_tp_first 模式: 阶梯止盈 > 成本止损 > 移动 > 时间
  (详见 config/default.yaml['stop_loss']['priority'] 与 StopManager.ladder_tp_first)
退出原因记录所有触发的条件，以 + 连接（如 cost_stop+trailing_stop）。

所有参数从配置读取，无需修改代码。
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional
from enum import IntEnum

from utils.logger import get_logger

logger = get_logger(__name__)


class StopPriority(IntEnum):
    FORMULA_SELL = 0    # 最高优先级 (P-v3.4: 公式卖出)
    COST_STOP = 1
    TRAILING_STOP = 2
    LADDER_TP = 3
    TIME_STOP = 4


class ExitReason:
    FORMULA_SELL = "formula_sell"   # P-v3.4
    COST_STOP = "cost_stop"
    TRAILING_STOP = "trailing_stop"
    LADDER_TP = "ladder_tp"
    TIME_STOP = "time_stop"


class StopManager:
    """
    多维度止损止盈管理器。

    Parameters:
        config: stop_loss 配置字典

    Example config:
        stop_loss:
          cost_stop:
            enabled: true
            threshold: -0.08
          trailing_stop:
            enabled: true
            activation: 0.05
            drawdown: 0.03
          ladder_tp:
            enabled: true
            levels:
              - profit: 0.10
                sell_ratio: 0.33
              - profit: 0.20
                sell_ratio: 0.50
              - profit: 0.30
                sell_ratio: 1.00
          time_stop:
            enabled: true
            max_hold_days: 20
    """

    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        # 成本止损
        cost_cfg = config.get("cost_stop", {})
        self.cost_stop_enabled = cost_cfg.get("enabled", True)
        self.cost_stop_threshold = cost_cfg.get("threshold", -0.12)

        # 移动止损
        trail_cfg = config.get("trailing_stop", {})
        self.trailing_enabled = trail_cfg.get("enabled", True)
        self.trailing_activation = trail_cfg.get("activation", 0.08)
        self.trailing_drawdown = trail_cfg.get("drawdown", 0.05)

        # 阶梯止盈
        ladder_cfg = config.get("ladder_tp", {})
        self.ladder_enabled = ladder_cfg.get("enabled", True)
        self.ladder_levels: List[dict] = ladder_cfg.get("levels", [])
        # 按盈利比例升序排列
        self.ladder_levels.sort(key=lambda x: x.get("profit", 0))

        # 时间止盈
        time_cfg = config.get("time_stop", {})
        self.time_enabled = time_cfg.get("enabled", True)
        self.max_hold_days = time_cfg.get("max_hold_days", 20)

        # P-v3.4: 公式卖出 (formula_sell) — TDX 信号驱动, 最高优先级
        fs_cfg = config.get("formula_sell", {})
        self.formula_sell_enabled = bool(fs_cfg.get("enabled", False))
        self.formula_sell_ratio = float(fs_cfg.get("sell_ratio", 1.0))
        self.formula_sell_priority = int(fs_cfg.get("priority", 0))
        self.formula_sell_formula_name = str(fs_cfg.get("formula_name", ""))

        # 2026-07-05: 优先级 (与 engine._simulate_core_v3 保持一致)
        #   ladder_tp_first=True   → ladder_tp 先于 cost_stop
        #   trailing_first=True    → ladder_tp > trailing > cost_stop
        #   stop_first (默认)      → cost_stop > ladder_tp > trailing
        # 仅影响 best_reason 标签选择, 不影响成交价 (成交决策由 engine 完成)
        self.ladder_tp_first = (str(config.get("priority", "stop_first")) == "ladder_tp_first")
        self.trailing_first = (str(config.get("priority", "stop_first")) == "trailing_first")

    def compute_exit_signals(
        self,
        close_prices: pd.DataFrame,
        entry_signals: pd.DataFrame,
        high_prices: Optional[pd.DataFrame] = None,
        low_prices: Optional[pd.DataFrame] = None,
        *,
        formula_exit_np: Optional[np.ndarray] = None,
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        计算每只股票每天的卖出信号。

        Args:
            close_prices: 收盘价 DataFrame，index=日期, columns=股票代码
            entry_signals: 买入信号 DataFrame，index=日期, columns=股票代码，True=买入
            high_prices: 最高价 DataFrame（可选，用于检测日内触发）
            low_prices: 最低价 DataFrame（可选，用于检测日内触发）
            formula_exit_np: P-v3.4 公式卖出矩阵，shape (n_dates, n_stocks), dtype=bool

        Returns:
            (exit_signals, exit_info):
                exit_signals: 卖出信号 DataFrame
                exit_info: 每条卖出记录详情 DataFrame
        """
        close = close_prices.astype(np.float64)
        entries = entry_signals.astype(bool)

        n_dates, n_stocks = close.shape
        exit_signal = pd.DataFrame(False, index=close.index, columns=close.columns)
        exit_records = []

        # P-v3.4: 公式卖出矩阵可能与 close.columns 数量不一致, 强制对齐 (找不到的列视为无信号)
        formula_exit_aligned = None
        if formula_exit_np is not None:
            try:
                # 用列名匹配重索引
                if formula_exit_np.shape[0] == n_dates:
                    formula_exit_aligned = formula_exit_np
                else:
                    logger.warning(
                        "formula_exit_np 行数 (%d) 与 close 行数 (%d) 不匹配, 跳过",
                        formula_exit_np.shape[0], n_dates,
                    )
            except Exception as e:
                logger.warning("formula_exit_np 对齐失败, 跳过: %s", e)
                formula_exit_aligned = None

        for col_idx, stock_code in enumerate(close.columns):
            price_arr = close.iloc[:, col_idx].values
            entry_arr = entries.iloc[:, col_idx].values if col_idx < entries.shape[1] else np.zeros(n_dates, dtype=bool)

            # 获取high/low数组
            high_arr = None
            low_arr = None
            if high_prices is not None and stock_code in high_prices.columns:
                high_arr = high_prices[stock_code].values.astype(np.float64)
            if low_prices is not None and stock_code in low_prices.columns:
                low_arr = low_prices[stock_code].values.astype(np.float64)

            exits, records = self._compute_single_stock(
                stock_code, price_arr, entry_arr, close.index,
                high_arr=high_arr, low_arr=low_arr,
                formula_exit_arr=formula_exit_aligned, code_idx=col_idx,
            )
            exit_signal.iloc[:, col_idx] = exits
            exit_records.extend(records)

        exit_info = pd.DataFrame(exit_records) if exit_records else pd.DataFrame(
            columns=["stock_code", "entry_date", "exit_date", "entry_price",
                     "exit_price", "exit_reason", "sell_ratio"]
        )

        return exit_signal, exit_info

    def _compute_single_stock(
        self,
        stock_code: str,
        prices: np.ndarray,
        entries: np.ndarray,
        date_index: pd.DatetimeIndex,
        high_arr: Optional[np.ndarray] = None,
        low_arr: Optional[np.ndarray] = None,
        formula_exit_arr: Optional[np.ndarray] = None,    # P-v3.4: 公式卖出矩阵
        code_idx: int = -1,                                # P-v3.4: 公式卖出矩阵的列索引
    ) -> Tuple[np.ndarray, list]:
        """单只股票的止损止盈计算。"""
        n = len(prices)
        exits = np.zeros(n, dtype=bool)
        records = []

        # 跟踪当前持仓
        in_position = False
        entry_price = 0.0
        entry_date = None
        entry_idx = -1
        highest_price = 0.0  # 持仓期间最高收盘价
        highest_hi = 0.0      # 持仓期间实际最高价
        remaining_ratio = 1.0  # 剩余仓位比例（阶梯止盈使用）
        ladder_level_triggered = set()  # 已触发的阶梯档位

        for i in range(n):
            if np.isnan(prices[i]):
                continue

            # 买入信号
            if entries[i] and not in_position:
                in_position = True
                entry_price = prices[i]
                entry_date = date_index[i]
                entry_idx = i
                highest_price = prices[i]
                highest_hi = prices[i]
                remaining_ratio = 1.0
                ladder_level_triggered = set()
                continue  # 入场当天不检查止损

            # 持仓中，检查止损止盈（叠加模式：所有条件独立检查）
            if in_position:
                current_price = prices[i]
                profit_pct = (current_price - entry_price) / entry_price

                if current_price > highest_price:
                    highest_price = current_price

                # 获取当前bar的high/low
                bar_high = high_arr[i] if high_arr is not None else current_price
                bar_low = low_arr[i] if low_arr is not None else current_price
                if bar_high > highest_hi:
                    highest_hi = bar_high
                hi_profit = (bar_high - entry_price) / entry_price
                lo_profit = (bar_low - entry_price) / entry_price
                peak_hi_profit = (highest_hi - entry_price) / entry_price

                # 收集所有触发的条件
                triggered = []  # (reason, sell_ratio)

                # P-v3.4: 公式卖出 (formula_sell) — 最高优先级, 早于 cost_stop
                # 注意: 这里是 StopManager "标签层", 实际决策由 _simulate_core_v3 完成
                # 这里仅作为 fallback 标签路径, 与主循环结果一致 (reason=12)
                if (formula_exit_arr is not None
                        and 0 <= code_idx < formula_exit_arr.shape[1]
                        and 0 <= i < formula_exit_arr.shape[0]
                        and bool(formula_exit_arr[i, code_idx])):
                    triggered.append((ExitReason.FORMULA_SELL, self.formula_sell_ratio))

                # 成本止损：检查Low是否跌破止损线（最高优先级）
                if self.cost_stop_enabled and lo_profit <= self.cost_stop_threshold:
                    triggered.append((ExitReason.COST_STOP, 1.0))

                # 阶梯止盈：检查High是否触及目标档位（主动策略优先）
                if self.ladder_enabled and self.ladder_levels:
                    for lv_idx, level in enumerate(self.ladder_levels):
                        if lv_idx in ladder_level_triggered:
                            continue
                        if hi_profit >= level["profit"]:
                            ratio = min(level.get("sell_ratio", 1.0), remaining_ratio)
                            if ratio > 0:
                                ladder_level_triggered.add(lv_idx)
                                triggered.append((ExitReason.LADDER_TP, ratio))
                                remaining_ratio -= ratio

                # 移动止损/止盈 (2026-07-05 v3): 盘中 Low 触及回撤线即触发 (与 engine 同步)
                #   旧: Close 回撤检测 (drawdown_pct = (Close - peak_hi)/peak_hi)
                #   新: Low 触及回撤线 (bar_low <= peak_hi * (1-drawdown))
                if self.trailing_enabled and peak_hi_profit >= self.trailing_activation:
                    trail_line = highest_hi * (1.0 - self.trailing_drawdown)
                    if bar_low <= trail_line:
                        triggered.append((ExitReason.TRAILING_STOP, 1.0))

                # 时间止盈
                if self.time_enabled:
                    hold_days = i - entry_idx
                    if hold_days >= self.max_hold_days:
                        triggered.append((ExitReason.TIME_STOP, 1.0))

                # 取最大卖出比例作为最终操作
                if triggered:
                    # 2026-07-05: 三档 priority 的 best_reason 选择 (与 engine 优先级一致)
                    #   trailing_first  → 优先 TRAILING_STOP
                    #   ladder_tp_first → 优先 LADDER_TP
                    #   stop_first      → 按 max sell_ratio (cost_stop ratio=1.0 通常赢)
                    if self.trailing_first:
                        trail_hits = [t for t in triggered if t[0] == ExitReason.TRAILING_STOP]
                        if trail_hits:
                            best_reason, best_ratio = trail_hits[0]
                        else:
                            best_reason, best_ratio = max(triggered, key=lambda x: x[1])
                    elif self.ladder_tp_first:
                        ladder_hits = [t for t in triggered if t[0] == ExitReason.LADDER_TP]
                        if ladder_hits:
                            best_reason, best_ratio = ladder_hits[0]
                        else:
                            best_reason, best_ratio = max(triggered, key=lambda x: x[1])
                    else:
                        best_reason, best_ratio = max(triggered, key=lambda x: x[1])
                    all_reasons = "+".join(r for r, _ in triggered)
                    exits[i] = True

                    records.append({
                        "stock_code": stock_code,
                        "entry_date": entry_date,
                        "exit_date": date_index[i],
                        "entry_price": round(entry_price, 4),
                        "exit_price": round(current_price, 4),
                        "exit_reason": all_reasons,
                        "sell_ratio": round(best_ratio, 4),
                        "profit_pct": round(profit_pct, 4),
                    })

                    # 完全卖出 → 清除持仓
                    if remaining_ratio <= 0.001:
                        in_position = False
                        entry_price = 0.0
                        entry_date = None
                        entry_idx = -1
                        highest_price = 0.0
                        highest_hi = 0.0
                        remaining_ratio = 1.0
                        ladder_level_triggered = set()

        # 数据结束仍持仓 → 不平仓，按市值计入最终权益
        return exits, records

    def get_config_summary(self) -> str:
        """获取止损止盈配置摘要。"""
        lines = []
        if self.cost_stop_enabled:
            lines.append(f"成本止损: {self.cost_stop_threshold:.1%}（Low触发, stop_price执行）")
        if self.ladder_enabled and self.ladder_levels:
            levels_str = " → ".join(
                f"盈利{lv['profit']:.0%}卖{lv['sell_ratio']:.0%}"
                for lv in self.ladder_levels
            )
            lines.append(f"阶梯止盈: {levels_str}（High触发, ladder_price执行）")
        if self.trailing_enabled:
            lines.append(f"移动止损: 盈利{self.trailing_activation:.1%}激活, 盘中Low触及回撤{self.trailing_drawdown:.1%}线即按回撤线价成交")
        if self.time_enabled:
            lines.append(f"时间止损: {self.max_hold_days}天（Close执行）")
        # P-v3.4: 公式卖出
        if self.formula_sell_enabled:
            fname = self.formula_sell_formula_name or "?(未配置公式名)"
            lines.append(
                f"公式卖出: [{fname}] 命中即卖{self.formula_sell_ratio:.0%} "
                f"（优先级 #{self.formula_sell_priority}，最高=0）"
            )
        return "\n".join(lines)
