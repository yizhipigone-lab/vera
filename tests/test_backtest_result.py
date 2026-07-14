"""C3 — BacktestResult dataclass + dict 兼容语义测试。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.result import BacktestResult


class TestBacktestResultDictCompat:
    def test_set_field_accessible(self):
        r = BacktestResult(equity_curve=pd.DataFrame({"equity": [1, 2]}), metrics={"a": 1})
        assert r["equity_curve"] is not None
        assert r["metrics"] == {"a": 1}
        # .field 访问
        assert r.metrics == {"a": 1}

    def test_unset_field_not_in(self):
        """未设置的字段 not in result（精确 dict 语义）。"""
        r = BacktestResult(equity_curve=pd.DataFrame(), metrics={})
        assert "raw_equity" not in r
        assert "raw_trades" not in r
        assert "stop_config_summary" not in r
        assert "equity_curve" in r
        assert "metrics" in r

    def test_get_unset_returns_default(self):
        r = BacktestResult(metrics={})
        assert r.get("raw_equity") is None
        assert r.get("raw_equity", "sentinel") == "sentinel"
        assert r.get("metrics", {}) == {}

    def test_getitem_unset_raises(self):
        r = BacktestResult(metrics={})
        with pytest.raises(KeyError):
            _ = r["raw_equity"]

    def test_keys_reflect_only_set(self):
        r = BacktestResult(equity_curve=pd.DataFrame(), trades=pd.DataFrame(),
                           metrics={}, cumulative_return=0.05)
        assert set(r.keys()) == {"equity_curve", "trades", "metrics", "cumulative_return"}

    def test_items_and_to_dict(self):
        r = BacktestResult(metrics={"x": 1}, stock_count=3)
        d = dict(r.items())
        assert d == {"metrics": {"x": 1}, "stock_count": 3}
        assert r.to_dict() == d

    def test_run_vs_run_cached_key_sets(self):
        """run() 与 run_cached() 的 key 集合互不相同（对齐老 dict）。"""
        run_result = BacktestResult(
            equity_curve=pd.DataFrame(), trades=pd.DataFrame(), metrics={},
            stop_config_summary="s", selections=pd.DataFrame(), stock_count=0)
        cached_result = BacktestResult(
            metrics={}, trades=pd.DataFrame(), cumulative_return=0.0,
            equity_curve=pd.DataFrame())
        assert set(run_result.keys()) == {
            "equity_curve", "trades", "metrics", "stop_config_summary", "selections", "stock_count"}
        assert set(cached_result.keys()) == {
            "metrics", "trades", "cumulative_return", "equity_curve"}

    def test_frozen(self):
        r = BacktestResult(metrics={})
        with pytest.raises(Exception):
            r.metrics = {"new": 1}  # type: ignore

    def test_unknown_key(self):
        r = BacktestResult(metrics={})
        assert "trade_count" not in r  # 非字段
        assert r.get("trade_count") is None
        with pytest.raises(KeyError):
            _ = r["trade_count"]
