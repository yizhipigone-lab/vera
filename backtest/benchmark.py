"""基准指数对比器 — 将策略收益与大盘指数进行对比。"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional

# P2-6 (2026-07-15): 直接 import 纯数据常量, 无需拖入整个 BacktestEngine 模块
from backtest._constants import PERIODS_PER_YEAR

from core.data_fetcher import DataFetcher
from utils.logger import get_logger

logger = get_logger(__name__)


def compute_comparison_stats(comparison: pd.DataFrame,
                             periods_per_year: int = 252) -> dict:
    """从对齐后的对比帧算超额汇总指标 (2026-07-18; 此前这些数只打日志不输出)。

    Args:
        comparison: _align 产物 (date 索引, strategy_equity/index_close/
                    strategy_return/index_return/excess_return 列)
        periods_per_year: 年化周期数 (与 BenchmarkComparator 口径一致)

    Returns:
        dict: strategy_total / index_total (区间累计涨跌),
              total_excess (几何口径累计超额), annual_excess (复合年化超额),
              information_ratio (年化超额 ÷ 年化跟踪误差, 超额稳定性),
              excess_monthly_win_rate (月度跑赢基准的月份占比, <2 个月为 None)
    """
    if comparison is None or comparison.empty or len(comparison) < 2:
        return {}
    total_strategy = float(comparison["strategy_equity"].iloc[-1])
    total_index = float(comparison["index_close"].iloc[-1])
    stats = {
        "strategy_total": total_strategy - 1,
        "index_total": total_index - 1,
    }
    total_excess = (total_strategy / total_index) - 1 if total_index > 0 else 0.0
    n_years = len(comparison) / periods_per_year if periods_per_year > 0 else 0
    stats["total_excess"] = float(total_excess)
    stats["annual_excess"] = (
        float((1 + total_excess) ** (1 / n_years) - 1) if n_years > 0 else 0.0)

    ex = comparison["excess_return"].dropna()
    # std 阈值 1e-12: 恒定超额场景只剩浮点尾差 (~1e-16), 不能当有效波动除
    if len(ex) >= 2 and ex.std() > 1e-12:
        # IR = 年化超额均值 ÷ 年化跟踪误差 (算术年化, IR 标准口径)
        stats["information_ratio"] = float(
            ex.mean() * periods_per_year / (ex.std() * np.sqrt(periods_per_year)))
    else:
        stats["information_ratio"] = None

    # 月度跑赢比例: 逐月几何收益, 策略 > 基准的月份占比
    try:
        monthly = comparison[["strategy_return", "index_return"]].resample("ME").apply(
            lambda s: (1 + s.dropna()).prod() - 1)
        monthly = monthly.dropna()
        if len(monthly) >= 2:
            stats["excess_monthly_win_rate"] = float(
                (monthly["strategy_return"] > monthly["index_return"]).mean())
        else:
            stats["excess_monthly_win_rate"] = None
    except Exception as e:
        logger.warning("月度胜率计算失败: %s", e)
        stats["excess_monthly_win_rate"] = None
    return stats


class BenchmarkComparator:
    """
    基准指数对比。

    将策略权益曲线与指定指数归一化对比。
    """

    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        self.index_names: List[str] = config.get("indices", ["shanghai"])
        self.normalize_start = config.get("normalize_start", True)
        self.period = config.get("period", "1d")

    def fetch_and_compare(
        self,
        equity_curve: pd.DataFrame,
        start_time: str = "",
        end_time: str = "",
    ) -> Dict[str, pd.DataFrame]:
        """
        获取指数数据并与策略权益曲线对齐比较。

        Args:
            equity_curve: 策略权益曲线 (date, equity)
            start_time: 起始时间
            end_time: 结束时间

        Returns:
            dict: {index_name: comparison_df}
        """
        # P1-4: 1w 用 52 周/年，避免 *252 高估 4.8 倍。P2-6 (2026-07-15): 直接引用 _constants
        periods_per_year = PERIODS_PER_YEAR.get(self.period, 252)
        results = {}
        for idx_name in self.index_names:
            logger.info(f"获取指数 [{idx_name}] 数据进行对比...")
            index_df = self._fetch_index(idx_name, start_time, end_time)
            if index_df.empty:
                logger.warning(f"指数 [{idx_name}] 数据为空，跳过")
                continue

            comparison = self._align(equity_curve, index_df, idx_name, periods_per_year)
            results[idx_name] = comparison

        return results

    def _fetch_index(
        self,
        index_name: str,
        start_time: str = "",
        end_time: str = "",
    ) -> pd.DataFrame:
        """获取指数 K 线数据。"""
        df = DataFetcher.get_index_data(
            index_name, start_time, end_time,
            dividend_type="none", period=self.period,
        )
        return df

    def _align(
        self,
        equity_curve: pd.DataFrame,
        index_df: pd.DataFrame,
        index_name: str,
        periods_per_year: int = 252,
    ) -> pd.DataFrame:
        """
        将策略权益与指数收盘价对齐。

        Returns:
            DataFrame with columns: date, strategy_equity, strategy_return,
                                   index_close, index_return, excess_return
        """
        eq = equity_curve.copy()

        # 确保日期索引
        if "date" in eq.columns:
            eq = eq.set_index("date")
        if "date" in index_df.columns:
            index_df = index_df.set_index("date")

        eq.index = pd.to_datetime(eq.index)
        index_df.index = pd.to_datetime(index_df.index)

        # 找到共同日期
        common_dates = eq.index.intersection(index_df.index)
        if len(common_dates) < 2:
            logger.warning(f"策略与指数 [{index_name}] 共同交易日不足")
            return pd.DataFrame()

        eq = eq.loc[common_dates].copy()
        idx = index_df.loc[common_dates].copy()

        # 归一化
        if "equity" in eq.columns:
            strategy_eq = eq["equity"]
        else:
            strategy_eq = eq.iloc[:, 0]

        if self.normalize_start:
            strategy_norm = strategy_eq / strategy_eq.iloc[0]
            if "close" in idx.columns:
                index_norm = idx["close"] / idx["close"].iloc[0]
            else:
                index_norm = idx.iloc[:, 0] / idx.iloc[:, 0].iloc[0]
        else:
            strategy_norm = strategy_eq
            index_norm = idx.get("close", idx.iloc[:, 0])

        comparison = pd.DataFrame({
            "date": common_dates,
            "strategy_equity": strategy_norm.values,
            "index_close": index_norm.values,
        })
        comparison["strategy_return"] = comparison["strategy_equity"].pct_change()
        comparison["index_return"] = comparison["index_close"].pct_change()
        comparison["excess_return"] = comparison["strategy_return"] - comparison["index_return"]
        comparison.set_index("date", inplace=True)

        # 计算超额收益统计（P1-5: 几何口径 + 复合年化，替换原算术和+线性年化）
        total_strategy = comparison["strategy_equity"].iloc[-1]
        total_index = comparison["index_close"].iloc[-1]
        total_excess = (total_strategy / total_index) - 1 if total_index > 0 else 0.0
        n_years = len(comparison) / periods_per_year if periods_per_year > 0 else 0
        annual_excess = (1 + total_excess) ** (1 / n_years) - 1 if n_years > 0 else 0.0

        logger.info(
            f"[{index_name}] 对比: 策略累计={comparison['strategy_equity'].iloc[-1]:.3f}, "
            f"指数累计={comparison['index_close'].iloc[-1]:.3f}, "
            f"超额收益={total_excess:.2%}"
        )

        # 2026-07-18: 汇总指标挂 attrs (不动函数签名, 下游 report/可视化零影响;
        # result_writer 读取后输出到前端超额卡片)
        comparison.attrs["stats"] = compute_comparison_stats(
            comparison, periods_per_year)

        return comparison
