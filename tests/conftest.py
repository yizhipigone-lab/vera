"""pytest 全局 fixture — TDX 路径注入 + TdxConnector 生命周期管理 + Mock 工厂。

审计 T-C1 (2026-07-15): 原本 TDX 路径注入散落在多个测试文件的 fixture 里重复定义,
且 test_end_to_end_real_pipeline.py:42 引用不存在的 conftest。此处统一收口。

T-H-2 (2026-07-15): 加 FakeTq/FakeConnector mock 工厂 + autouse teardown
(FormulaRunner + DataFetcher connector 共享重置)。
"""

from __future__ import annotations

import sys

import numpy as np
import pytest

# TDX 插件路径 (通达信 PYPlugins\user 目录; 单一真相: core/tdx_path.py)
from core.tdx_path import tdx_plugins_user

_TDX_PATH = tdx_plugins_user()


@pytest.fixture(scope="session")
def tdx_path():
    """确保 TDX 模块路径在 sys.path 中。session 级别, 所有测试共享。"""
    if _TDX_PATH not in sys.path:
        sys.path.insert(0, _TDX_PATH)
    return _TDX_PATH


@pytest.fixture(scope="module")
def tdx_connector():
    """module 级别 TdxConnector 生命周期: initialize → yield → close。"""
    from core.connector import TdxConnector
    TdxConnector.initialize()
    yield TdxConnector
    TdxConnector.close()


# ═══════════════════════════════════════════════════════════════
# T-H-2 Mock 工厂 (2026-07-15)
# ═══════════════════════════════════════════════════════════════

class FakeTq:
    """Mock TDX TQ API, 可配置返回数据."""

    def __init__(self, ohlc_data=None, formula_result=None):
        self.calls = []
        self._ohlc = ohlc_data or {}
        self._formula = formula_result or []

    def get_market_data(self, **kw):
        self.calls.append(("get_market_data", kw))
        return self._ohlc if self._ohlc else {
            "ErrorId": "0",
        }

    def get_stock_list(self, *args, **kwargs):
        self.calls.append(("get_stock_list", args, kwargs))
        return [{"Code": "000001.SZ"}, {"Code": "600519.SH"}]

    def formula_process_mul_xg(self, formula_name, formula_arg="",
                                return_count=0, return_date=True,
                                stock_list=None, stock_period="1d",
                                start_time="", end_time="", count=3000,
                                dividend_type=1):
        self.calls.append(("formula_process_mul_xg", {
            "formula_name": formula_name, "stock_list": stock_list,
        }))
        return self._formula


class FakeConnector:
    """Mock TdxConnector, 包裹 FakeTq."""

    def __init__(self, ohlc_data=None, formula_result=None):
        self.connected = False
        self.tq_obj = FakeTq(ohlc_data, formula_result)

    def ensure_connected(self):
        self.connected = True

    def tq(self):
        return self.tq_obj

    @staticmethod
    def initialize():
        pass

    @staticmethod
    def close():
        pass


@pytest.fixture(autouse=True)
def _reset_all_connectors():
    """每个测试后自动重置所有 connector seam (防测试间污染)."""
    yield
    from core.data_fetcher import DataFetcher
    from core.formula_runner import FormulaRunner
    DataFetcher.reset_connector()
    FormulaRunner.reset_connector()


@pytest.fixture(autouse=True)
def _isolate_caches(tmp_path):
    """缓存根目录隔离 (2026-07-27 缓存投毒事件)。

    事件: test_sector_selection 把 mock 的 3 股池经 selector 接缝写入真实
    data/universe_cache, key 与用户 QUANTQQ 配置完全相同 → 用户回测命中
    毒缓存, 300 只池变 3 只, 1.5 年只有 21 笔交易。

    2026-08-01 批次2: 改经 utils.parquet_cache 注册表一行式覆盖, 不再
    import/monkeypatch 四个缓存模块。四个缓存根统一指到 per-test tmp:
    测试自己的缓存行为不受影响 (各 cache 测试本来就显式传 root 或自行 patch),
    但任何"忘了隔离"的测试从此不可能再污染生产缓存。
    """
    from utils import parquet_cache as pcu
    for name in ("universe_cache", "selection_cache",
                 "signal_day_cache", "matrix_cache"):
        pcu.set_root(name, tmp_path / name)
    yield


class FakeLoop:
    """Mock BacktestLoop for monkeypatch tests. Captures run() args, returns stub equity/trades."""
    def __init__(self, equity=None, trades=None):
        self._equity = equity
        self._trades = trades if trades is not None else np.empty((0, 9))
        self.captured = {}

    def run(self, price_np, entry_np, high_np=None, low_np=None, open_np=None,
            tradable_np=None, last_tradable_idx=None, formula_exit_np=None):
        import numpy as np
        self.captured = dict(price_np=price_np, entry_np=entry_np,
                             high_np=high_np, tradable_np=tradable_np,
                             last_tradable_idx=last_tradable_idx)
        eq = self._equity if self._equity is not None else np.full(price_np.shape[0], 100000.0)
        return eq, np.atleast_2d(self._trades)
