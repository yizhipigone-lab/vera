"""open_t1 引擎级集成测试 (2026-08-20 质量审计补充).

覆盖审计发现的覆盖缺口:
- run_cached open_t1 端到端: T+1 开盘价成交 / 一字涨停拒买 / entry_mode_info
- close_t 对照零变化
- 周线(1w) + open_t1 构造期 fail-fast (审计 HIGH 修复)
- result_writer: entry_price_basis 随口径标注 (审计 LOW 修复)
"""
import numpy as np
import pandas as pd
import pytest

from backtest._entry_basis import ENTRY_BASIS_BACKTEST, ENTRY_BASIS_BACKTEST_T1
from backtest.engine import BacktestEngine
from backtest.prepared import PreparedMatrix
from backtest.result import BacktestResult

IDX = pd.to_datetime(["2026-08-10", "2026-08-11", "2026-08-12",
                      "2026-08-13", "2026-08-14"])
COLS = ["600001"]

# 时间止盈 2 天, 保证持仓在窗口内平仓产生交易行 (期末未平仓不出 trades)
STOP = {"time_stop": {"enabled": True, "max_hold_days": 2},
        "cost_stop": {"enabled": False},
        "trailing_stop": {"enabled": False},
        "ladder_tp": {"enabled": False},
        "cond_time_stop": {"enabled": False}}


def _engine(mode):
    eng = BacktestEngine({"initial_capital": 100000.0, "commission": 0.0,
                          "slippage": 0.0, "stamp_tax": 0.0,
                          "enable_realistic_costs": False,
                          "entry_price_mode": mode})
    # 合成数据无 ST 语义; 绕开 TDX 股票信息外部依赖 (对齐 test_snapshot_parity 先例)
    eng._limit_ratio_vector = lambda cols: np.full(len(cols), 0.10)
    return eng


def _run(eng, o, h, l, c, entries):
    m = lambda v: np.asarray(v, dtype=np.float64).reshape(-1, 1)
    close = pd.DataFrame(m(c), index=IDX, columns=COLS)
    ent = pd.DataFrame(np.asarray(entries, dtype=bool).reshape(-1, 1),
                       index=IDX, columns=COLS)
    prep = PreparedMatrix(close=close, entries=ent,
                          high_np=m(h), low_np=m(l), open_np=m(o))
    return eng.run_cached(prep, STOP,
                          np.array([], dtype=np.float64),
                          np.array([], dtype=np.float64), 0)


BASE_O = [10.0, 11.0, 11.2, 11.4, 11.6]
BASE_H = [10.5, 11.3, 11.5, 11.7, 11.9]
BASE_L = [9.9, 10.9, 11.1, 11.3, 11.5]
BASE_C = [10.4, 11.2, 11.4, 11.6, 11.8]
SIG = [True, False, False, False, False]


def test_run_cached_open_t1_buys_next_open():
    """信号日 8-10 → 次日 8-11 开盘价 11.0 成交 (不是信号日收盘 10.4)。"""
    res = _run(_engine("open_t1"), BASE_O, BASE_H, BASE_L, BASE_C, SIG)
    trades = res["trades"]
    assert len(trades) == 1
    assert trades.iloc[0]["entry_price"] == pytest.approx(11.0)
    assert trades.iloc[0]["entry_date"] == pd.Timestamp("2026-08-11")
    info = res["entry_mode_info"]
    assert info["mode"] == "open_t1"
    assert info["n_signals"] == 1 and info["n_shifted"] == 1
    assert info["n_oneline_limit_up"] == 0


def test_run_cached_open_t1_oneline_rejected():
    """T+1 一字涨停 (8-11 四价 11.44 = 10.4×1.1) → 拒买, 无交易, 计数正确。"""
    o = [10.0, 11.44, 11.2, 11.4, 11.6]
    h = [10.5, 11.44, 11.5, 11.7, 11.9]
    l = [9.9, 11.44, 11.1, 11.3, 11.5]
    c = [10.4, 11.44, 11.4, 11.6, 11.8]
    res = _run(_engine("open_t1"), o, h, l, c, SIG)
    assert len(res["trades"]) == 0
    info = res["entry_mode_info"]
    assert info["n_oneline_limit_up"] == 1
    assert info["n_shifted"] == 0


def test_run_cached_close_t_unaffected():
    """close_t 对照: 信号日收盘 10.4 成交, 无 entry_mode_info (零行为变化)。"""
    res = _run(_engine("close_t"), BASE_O, BASE_H, BASE_L, BASE_C, SIG)
    trades = res["trades"]
    assert len(trades) == 1
    assert trades.iloc[0]["entry_price"] == pytest.approx(10.4)
    assert trades.iloc[0]["entry_date"] == pd.Timestamp("2026-08-10")
    assert res.get("entry_mode_info") is None


def test_open_t1_rejects_weekly_period():
    """审计 HIGH: 周线 + open_t1 → 构造期 fail-fast ("次日"在周线下语义偷换)。"""
    with pytest.raises(ValueError, match="open_t1"):
        BacktestEngine({"period": "1w", "entry_price_mode": "open_t1"})
    # 支持的 period 不抛
    for p in ("1d", "5m", "1m"):
        BacktestEngine({"period": p, "entry_price_mode": "open_t1"})
    # 非法模式值照旧 fail-fast
    with pytest.raises(ValueError, match="entry_price_mode"):
        BacktestEngine({"entry_price_mode": "bogus"})


def test_open_t1_requires_ohlc():
    """open_np=None (capabilities 关掉跳空保护) → fail-fast, 不静默退化口径。"""
    m = lambda v: np.asarray(v, dtype=np.float64).reshape(-1, 1)
    close = pd.DataFrame(m(BASE_C), index=IDX, columns=COLS)
    ent = pd.DataFrame(np.asarray(SIG, dtype=bool).reshape(-1, 1),
                       index=IDX, columns=COLS)
    prep = PreparedMatrix(close=close, entries=ent,
                          high_np=m(BASE_H), low_np=m(BASE_L), open_np=None)
    with pytest.raises(ValueError, match="OHLC"):
        _engine("open_t1").run_cached(
            prep, STOP, np.array([], dtype=np.float64),
            np.array([], dtype=np.float64), 0)


def test_result_writer_basis_follows_mode():
    """审计 LOW: 响应 entry_price_basis 随口径标注, entry_mode_info 透传。"""
    from pipeline.result_writer import PipelineResult, ResultWriter
    w = ResultWriter(status_sink=lambda s, p: None)
    bt = BacktestResult(metrics={},
                        entry_mode_info={"mode": "open_t1", "n_oneline_limit_up": 3})
    resp = w.serialize(PipelineResult(
        selections=pd.DataFrame(), backtest=bt, benchmark={}, reports={}))
    assert resp["entry_price_basis"] == ENTRY_BASIS_BACKTEST_T1
    assert resp["entry_mode_info"]["n_oneline_limit_up"] == 3
    # close_t: 旧值不变, 无 entry_mode_info key
    resp2 = w.serialize(PipelineResult(
        selections=pd.DataFrame(), backtest=BacktestResult(metrics={}),
        benchmark={}, reports={}))
    assert resp2["entry_price_basis"] == ENTRY_BASIS_BACKTEST
    assert "entry_mode_info" not in resp2
