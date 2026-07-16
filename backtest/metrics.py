"""性能指标计算器 — 从回测结果计算标准量化指标。"""

import numpy as np
import pandas as pd
from typing import Dict, Any

from utils.logger import get_logger

logger = get_logger(__name__)


class MetricsCalculator:
    """从权益曲线和交易记录计算各项标准指标。"""

    @staticmethod
    def compute_all(
        equity_curve: pd.DataFrame,
        trades: pd.DataFrame,
        initial_capital: float = 1000000.0,
        risk_free: float = 0.015,
        periods_per_year: int = 252,
    ) -> dict:
        metrics = {}
        equity = equity_curve.get("equity", pd.Series(dtype=float))
        if equity.empty or len(equity) < 2:
            return metrics

        # P1-4: 年化用 periods_per_year（替换字面量 252），5m/1w 周期不再失真
        n_periods = len(equity)

        metrics["cumulative_return"] = float((equity.iloc[-1] - initial_capital) / initial_capital)
        if n_periods > 1:
            metrics["annualized_return"] = float(
                (1 + metrics["cumulative_return"]) ** (periods_per_year / n_periods) - 1
            )
        else:
            metrics["annualized_return"] = 0.0
        metrics["max_drawdown"] = float((equity / equity.expanding().max() - 1).min())
        metrics["sharpe_ratio"] = MetricsCalculator._sharpe(equity, risk_free, periods_per_year)
        metrics["calmar_ratio"] = (
            metrics["annualized_return"] / abs(metrics["max_drawdown"])
            if abs(metrics["max_drawdown"]) > 0.0001 else 0.0
        )

        if not trades.empty:
            metrics["total_trades"] = len(trades)
            pnl = trades.get("profit_pct", pd.Series(dtype=float))
            if len(pnl) > 0:
                metrics["win_rate"] = float((pnl > 0).sum() / len(pnl))
                gains = pnl[pnl > 0]
                losses = pnl[pnl < 0]
                metrics["profit_loss_ratio"] = float(gains.mean() / abs(losses.mean())) if len(losses) > 0 and len(gains) > 0 else 0.0
                metrics["total_pnl"] = float(pnl.sum())
                metrics["max_single_gain"] = float(pnl.max())
                metrics["max_single_loss"] = float(pnl.min())
                total_gain = float(gains.sum())
                total_loss = float(abs(losses.sum()))
                metrics["profit_factor"] = total_gain / total_loss if total_loss > 0 else float("inf")
            if "entry_date" in trades.columns and "exit_date" in trades.columns:
                try:
                    hold = (pd.to_datetime(trades["exit_date"]) - pd.to_datetime(trades["entry_date"])).dt.days
                    hold = hold.dropna()
                    if len(hold) > 0:
                        metrics["avg_hold_days"] = float(hold.mean())
                        metrics["max_hold_days"] = int(hold.max())
                        metrics["min_hold_days"] = int(hold.min())
                except Exception as e:
                    logger.warning("hold_days 计算失败: %s", e)

        # 注意: 对非日线周期(5m等)这是 bar 数而非实际交易日数, 语义上更接近 total_bars
        metrics["total_trading_days"] = len(equity_curve)
        return metrics

    @staticmethod
    def _sharpe(equity: pd.Series, risk_free: float = 0.015, periods_per_year: int = 252) -> float:
        returns = equity.pct_change().dropna()
        if len(returns) < 2 or returns.std() == 0:
            return 0.0
        excess = returns - risk_free / periods_per_year
        return float(excess.mean() / excess.std() * np.sqrt(periods_per_year))

    @staticmethod
    def max_drawdown(equity: pd.Series) -> float:
        """最大回撤: max((peak - trough) / peak)。"""
        if len(equity) < 2:
            return 0.0
        peak = equity.expanding().max()
        drawdown = (equity - peak) / peak
        return float(drawdown.min())

    @staticmethod
    def sharpe_ratio(equity: pd.Series, risk_free: float = 0.015, periods_per_year: int = 252) -> float:
        """夏普比率。"""
        if len(equity) < 2:
            return 0.0
        returns = equity.pct_change().dropna()
        if len(returns) < 2:
            return 0.0
        daily_rf = risk_free / periods_per_year
        excess = returns - daily_rf
        if excess.std() == 0:
            return 0.0
        return float(excess.mean() / excess.std() * np.sqrt(periods_per_year))

    @staticmethod
    def win_rate(trades: pd.DataFrame) -> float:
        """胜率: 盈利交易数 / 总交易数。"""
        if trades.empty or "profit_pct" not in trades.columns:
            return 0.0
        total = len(trades)
        if total == 0:
            return 0.0
        wins = (trades["profit_pct"] > 0).sum()
        return float(wins / total)

    @staticmethod
    def profit_loss_ratio(trades: pd.DataFrame) -> float:
        """盈亏比: avg(盈利) / abs(avg(亏损))。"""
        if trades.empty or "profit_pct" not in trades.columns:
            return 0.0
        pnl = trades["profit_pct"]
        gains = pnl[pnl > 0]
        losses = pnl[pnl < 0]
        if len(losses) == 0:
            return float("inf") if len(gains) > 0 else 0.0
        if len(gains) == 0:
            return 0.0
        return float(gains.mean() / abs(losses.mean()))

    @staticmethod
    def calmar_ratio(annualized_ret: float, max_dd: float) -> float:
        """卡玛比率: 年化收益 / |最大回撤|。"""
        if abs(max_dd) < 0.0001:
            return 0.0
        return float(annualized_ret / abs(max_dd))
