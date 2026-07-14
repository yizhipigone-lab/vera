"""ExitStrategy adapters (Strategy pattern)."""

from .base import ExitStrategy, AbsoluteStrategy, TriggerResult
from .cost_stop import CostStopStrategy
from .ladder_tp import LadderTpStrategy
from .trailing import TrailingStrategy
from .time_stop import TimeStopStrategy
from .cond_time import CondTimeStrategy
from .first_day import FirstDayStrategy
from .atr_stop import AtrStopStrategy

__all__ = [
    "ExitStrategy", "AbsoluteStrategy", "TriggerResult",
    "CostStopStrategy", "LadderTpStrategy", "TrailingStrategy",
    "TimeStopStrategy", "CondTimeStrategy", "FirstDayStrategy",
    "AtrStopStrategy",
]
