# -*- coding: utf-8 -*-
"""tests/test_engine_caliber.py — 选股口径校验下沉到 engine.run (批次 3.2, 2026-09-19)。

原本只有 Pipeline.step2_backtest 校验复权一致性, 直调 engine.run() 全部静默绕过
(engine.py 旧 docstring 自己承认)。下沉后三种状态必须各自明确:
  ① 传 caliber 且复权不一致 → 立即 raise ValueError (与 pipeline 同级);
  ② 传 caliber 且 period 不一致 → WARNING, 不中断 (1d 选股+5m 回测合法);
  ③ 不传 caliber (直调脚本) → WARNING 明示"未校验", 不再静默。
"""
from __future__ import annotations

import pandas as pd
import pytest

from backtest.engine import BacktestEngine


def _sel():
    """非空 selections (校验先于取数, 不一致时不会走到取数)。"""
    return pd.DataFrame({"stock_code": ["000001.SZ"], "select_date": ["20260803"]})


def test_mismatched_dividend_raises_before_fetch():
    """直调 run() 传不复权口径 → 立即 ValueError (不用 monkeypatch 取数:
    校验先于准备段, 不到取数就该炸)。"""
    eng = BacktestEngine({"period": "1d"})
    with pytest.raises(ValueError, match="复权口径不一致"):
        eng.run(_sel(), "20260801", "20260810", {},
                selection_caliber={"dividend_type": 0, "period": "1d"})


def test_matching_caliber_no_warning(caplog):
    eng = BacktestEngine({"period": "1d"})
    with caplog.at_level("WARNING", logger="backtest.engine"):
        eng._validate_caliber({"dividend_type": 1, "period": "1d"})
    assert not [r for r in caplog.records if "caliber_unverified" in r.message]
    assert not [r for r in caplog.records if "period_mismatch" in r.message]


def test_period_mismatch_warns_not_raises(caplog):
    eng = BacktestEngine({"period": "5m"})
    with caplog.at_level("WARNING", logger="backtest.engine"):
        eng._validate_caliber({"dividend_type": 1, "period": "1d"})
    assert any("period_mismatch" in r.message for r in caplog.records)


def test_missing_caliber_warns_explicitly(caplog):
    """直调不传 caliber: 明示未校验 (旧行为是完全静默)。"""
    eng = BacktestEngine({"period": "1d"})
    with caplog.at_level("WARNING", logger="backtest.engine"):
        eng._validate_caliber(None)
    assert any("caliber_unverified" in r.message for r in caplog.records), (
        "直调未声明口径必须留下可见痕迹, 不许静默")


def test_empty_selections_still_validates_caliber():
    """空 selections 也校验 (与下沉前 pipeline 行为一致): 坏口径照样抛。"""
    eng = BacktestEngine({"period": "1d"})
    empty = pd.DataFrame(columns=["stock_code", "select_date"])
    with pytest.raises(ValueError, match="复权口径不一致"):
        eng.run(empty, "20260801", "20260810", {},
                selection_caliber={"dividend_type": 0, "period": "1d"})
