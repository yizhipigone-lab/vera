"""多维度止损止盈管理器 — 叠加模式。

四个机制全部独立检查（基于收盘价），可独立启用/禁用：

    1. 成本止损    — (收盘价-成本)/成本 ≤ -X%，全仓卖出
    2. 移动止损    — 盈利>激活% 且 (最高价-收盘)/最高价 ≥ Y%，全仓卖出
    3. 阶梯止盈    — 盈利达 Z₁%→卖P₁%, Z₂%→卖P₂%, ...（分批）
    4. 时间止盈    — 持仓天数 ≥ N 天，全仓卖出

同一 bar 上若多个触发，取最大卖出比例执行（成本=100% > 移动=100%=时间=100% > 阶梯=部分）。
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
    COST_STOP = 1
    TRAILING_STOP = 2
    LADDER_TP = 3
    TIME_STOP = 4


class ExitReason:
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
        self.cost_stop_threshold = cost_cfg.get("threshold", -0.08)

        # 移动止损
        trail_cfg = config.get("trailing_stop", {})
        self.trailing_enabled = trail_cfg.get("enabled", True)
        self.trailing_activation = trail_cfg.get("activation", 0.05)
        self.trailing_drawdown = trail_cfg.get("drawdown", 0.03)

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

    def compute_exit_signals(
        self,
        close_prices: pd.DataFrame,
        entry_signals: pd.DataFrame,
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        计算每只股票每天的卖出信号。

        Args:
            close_prices: 收盘价 DataFrame，index=日期, columns=股票代码
            entry_signals: 买入信号 DataFrame，index=日期, columns=股票代码，True=买入

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

        for col_idx, stock_code in enumerate(close.columns):
            price_arr = close.iloc[:, col_idx].values
            entry_arr = entries.iloc[:, col_idx].values if col_idx < entries.shape[1] else np.zeros(n_dates, dtype=bool)

            exits, records = self._compute_single_stock(
                stock_code, price_arr, entry_arr, close.index,
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
        highest_price = 0.0
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
                remaining_ratio = 1.0
                ladder_level_triggered = set()
                continue  # 入场当天不检查止损

            # 持仓中，检查止损止盈（叠加模式：所有条件独立检查）
            if in_position:
                current_price = prices[i]
                profit_pct = (current_price - entry_price) / entry_price

                if current_price > highest_price:
                    highest_price = current_price

                # 收集所有触发的条件
                triggered = []  # (reason, sell_ratio)

                # 成本止损
                if self.cost_stop_enabled and profit_pct <= self.cost_stop_threshold:
                    triggered.append((ExitReason.COST_STOP, 1.0))

                # 移动止损: 最高价曾盈利≥激活% → 已激活; 回撤≥阈值 → 触发
                highest_profit = (highest_price - entry_price) / entry_price
                if self.trailing_enabled and highest_profit >= self.trailing_activation:
                    drawdown_pct = (current_price - highest_price) / highest_price
                    if drawdown_pct <= -self.trailing_drawdown:
                        triggered.append((ExitReason.TRAILING_STOP, 1.0))

                # 阶梯止盈（检查所有未触发的档位）
                if self.ladder_enabled and self.ladder_levels:
                    for lv_idx, level in enumerate(self.ladder_levels):
                        if lv_idx in ladder_level_triggered:
                            continue
                        if profit_pct >= level["profit"]:
                            ratio = min(level.get("sell_ratio", 1.0), remaining_ratio)
                            if ratio > 0:
                                ladder_level_triggered.add(lv_idx)
                                triggered.append((ExitReason.LADDER_TP, ratio))
                                remaining_ratio -= ratio

                # 时间止盈
                if self.time_enabled:
                    hold_days = i - entry_idx
                    if hold_days >= self.max_hold_days:
                        triggered.append((ExitReason.TIME_STOP, 1.0))

                # 取最大卖出比例作为最终操作
                if triggered:
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
                        remaining_ratio = 1.0
                        ladder_level_triggered = set()

        # 数据结束仍持仓 → 不平仓，按市值计入最终权益
        return exits, records

    def get_config_summary(self) -> str:
        """获取止损止盈配置摘要。"""
        lines = []
        if self.cost_stop_enabled:
            lines.append(f"成本止损: {self.cost_stop_threshold:.1%}")
        if self.trailing_enabled:
            lines.append(f"移动止损: 盈利{self.trailing_activation:.1%}激活, 回撤{self.trailing_drawdown:.1%}")
        if self.ladder_enabled and self.ladder_levels:
            levels_str = " → ".join(
                f"盈利{lv['profit']:.0%}卖{lv['sell_ratio']:.0%}"
                for lv in self.ladder_levels
            )
            lines.append(f"阶梯止盈: {levels_str}")
        if self.time_enabled:
            lines.append(f"时间止损: {self.max_hold_days}天")
        return "\n".join(lines)
