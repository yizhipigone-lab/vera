"""候选 A 阶段 2 — 核心回测循环拆解。

BacktestLoop 子包: 把 _simulate_core_v3 (527 行/39 参数) 拆成可单独测试的模块。
阶段 1 只导出数据结构与策略 adapter; BacktestLoop/ExitDispatcher 在阶段 2-3 补。
"""

from .state import (
    BacktestParams, Context, Position, PositionBook, TradeBuffer, Bar,
    TradeColumns, assert_state_dtype,
)
from .strategies import (
    ExitStrategy, AbsoluteStrategy, TriggerResult,
    CostStopStrategy, LadderTpStrategy, TrailingStrategy,
    TimeStopStrategy, CondTimeStrategy, FirstDayStrategy,
)
from .exit_engine import Priority, ExitDispatcher, PRE_DISPATCH_STRATEGIES
from .absolute import FormulaSellStrategy
from .entry import EntryEngine
from .equity import EquityTracker
from .loop import BacktestLoop
from .builder import build_backtest_loop

__all__ = [
    "BacktestParams", "Context", "Position", "PositionBook", "TradeBuffer", "Bar",
    "TradeColumns", "assert_state_dtype",
    "ExitStrategy", "AbsoluteStrategy", "TriggerResult",
    "CostStopStrategy", "LadderTpStrategy", "TrailingStrategy",
    "TimeStopStrategy", "CondTimeStrategy", "FirstDayStrategy",
    "Priority", "ExitDispatcher", "PRE_DISPATCH_STRATEGIES",
    "FormulaSellStrategy",
    "EntryEngine", "EquityTracker", "BacktestLoop", "build_backtest_loop",
]
