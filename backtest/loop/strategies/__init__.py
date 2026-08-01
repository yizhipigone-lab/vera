"""ExitStrategy adapters (Strategy pattern)."""

from .atr_stop import AtrStopStrategy
from .base import AbsoluteStrategy, ExitStrategy, TriggerResult
from .cond_time import CondTimeStrategy
from .cost_stop import CostStopStrategy
from .first_day import FirstDayStrategy
from .ladder_tp import LadderTpStrategy
from .time_stop import TimeStopStrategy
from .trailing import TrailingStrategy

__all__ = [
    "ExitStrategy", "AbsoluteStrategy", "TriggerResult",
    "CostStopStrategy", "LadderTpStrategy", "TrailingStrategy",
    "TimeStopStrategy", "CondTimeStrategy", "FirstDayStrategy",
    "AtrStopStrategy",
]
