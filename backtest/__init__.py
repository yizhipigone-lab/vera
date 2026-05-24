"""backtest 模块。"""

from .engine import BacktestEngine
from .stop_manager import StopManager, ExitReason
from .metrics import MetricsCalculator
from .benchmark import BenchmarkComparator
