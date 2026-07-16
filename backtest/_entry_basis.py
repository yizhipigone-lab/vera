"""入场价口径 (entry_price_basis) — 业务铁律 2+3 的代码层落地 (迭代 1, 2026-07-15).

CLAUDE.md 业务铁律:
  #2 回测买入价 = 信号日 T 收盘价
  #3 两套买卖口径别混: 回测走 T 收盘路径; 实盘走 T+1 开盘路径

CLAUDE.md 自承 "实盘 sim_trader T+1 开盘" — 但代码层面 sim_trader **从未存在**。
本模块:
- 显式常量 + 字面量锁定两套口径, 防止铁律被默默改掉
- 提供 BiasEstimator 工具, 可在回测结果上叠加滑点/涨跌停/T+1 偏差估算
  (虽然实盘口径未实现, 但偏差估算可作为策略层补偿依据)
- 提供路径冲突检查 (防止同代码同时混走两套口径)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


# 业务铁律 2 + 3 — 单一真相源,严禁字面量散落
ENTRY_BASIS_BACKTEST = "close_on_signal_day"   # 回测: 信号日 T 收盘价 (业务铁律 2)
ENTRY_BASIS_LIVE = "open_on_next_day"          # 实盘: T+1 开盘价 (业务铁律 3)


class EntryPath(Enum):
    """回测/实盘两条路径的代码层标识.

    设计意图:
    - 任何下单函数必须显式声明走哪条路径, 防止两套口径混用
    - 编译期 + 运行期双重保护 (字面量 + 类型)
    """
    BACKTEST_T_CLOSE = "close_on_signal_day"
    LIVE_T_PLUS_1_OPEN = "open_on_next_day"

    @property
    def is_backtest(self) -> bool:
        return self == EntryPath.BACKTEST_T_CLOSE

    @property
    def is_live(self) -> bool:
        return self == EntryPath.LIVE_T_PLUS_1_OPEN


# ═══════════════════════════════════════════════════════════════
# 偏差估算 (在实盘 sim_trader 未实现前, 用作策略层补偿依据)
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class LiveBiasEstimate:
    """回测结果 → 实盘预期偏差估算.

    字段:
        t_close_to_t1_open_gap:     T 收盘到 T+1 开盘的平均跳空 (历史回测期统计)
        limit_up_down_miss_rate:    涨跌停导致的成交失败率 (信号日 T 收盘想买但 T+1 一字板)
        liquidity_slippage_bps:     流动性滑点 (bps), 按日均成交额分档
        compound_bias_pct:          综合偏差 (百分点, 应用于回测 cumulative_return)

    注: 这些数字需要在历史实盘数据上标定. 本模块只定义结构, 数字由策略层注入.
    """
    t_close_to_t1_open_gap: float = 0.0      # 平均跳空 (绝对收益, 如 +0.001 = +0.1%)
    limit_up_down_miss_rate: float = 0.0     # 0~1, 如 0.05 = 5% 信号因涨跌停失败
    liquidity_slippage_bps: float = 5.0      # 默认 5bps = 0.05%
    compound_bias_pct: float = 0.0           # 综合偏差百分点

    def estimate_adjusted_return(self, backtest_cumulative_return: float) -> float:
        """估算实盘累计收益 = 回测累计 - 综合偏差.

        Args:
            backtest_cumulative_return: 回测累计收益 (如 0.25 = 25%)

        Returns:
            实盘估算累计收益 (保守值)
        """
        return backtest_cumulative_return - self.compound_bias_pct

    @staticmethod
    def from_empirical(
        t_close_to_t1_open_gap: float,
        limit_up_down_miss_rate: float,
        liquidity_slippage_bps: float = 5.0,
    ) -> "LiveBiasEstimate":
        """从经验参数构造偏差估算.

        compound_bias_pct 公式 (启发式, 可按策略调整):
            = limit_up_miss × |回测期均收益| + t1_gap + liquidity_slippage_bps/10000
        """
        compound = (
            limit_up_down_miss_rate * 0.02   # 假设回测期均收益 2%, 5% miss = 0.1% 损耗
            + t_close_to_t1_open_gap
            + liquidity_slippage_bps / 10000.0
        )
        return LiveBiasEstimate(
            t_close_to_t1_open_gap=t_close_to_t1_open_gap,
            limit_up_down_miss_rate=limit_up_down_miss_rate,
            liquidity_slippage_bps=liquidity_slippage_bps,
            compound_bias_pct=compound,
        )


# ═══════════════════════════════════════════════════════════════
# 路径冲突守卫
# ═══════════════════════════════════════════════════════════════


class EntryBasisConflictError(Exception):
    """两套入场价口径同时被触发 — 业务铁律 3 违反."""


def assert_single_path(*paths: EntryPath) -> None:
    """确保传入的所有路径属于同一口径 (全回测 OR 全实盘).

    Raises:
        EntryBasisConflictError: 当 BACKTEST_T_CLOSE 和 LIVE_T_PLUS_1_OPEN 同时出现

    用法 (伪代码):
        assert_single_path(
            EntryPath.BACKTEST_T_CLOSE,  # 回测入口
            EntryPath.BACKTEST_T_CLOSE,  # 引擎内部
        )
    """
    has_backtest = any(p.is_backtest for p in paths)
    has_live = any(p.is_live for p in paths)
    if has_backtest and has_live:
        raise EntryBasisConflictError(
            f"路径冲突: 同时检测到回测 ({ENTRY_BASIS_BACKTEST}) 和实盘 ({ENTRY_BASIS_LIVE}) "
            f"路径。业务铁律 3 严禁混用。"
        )