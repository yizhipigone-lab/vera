"""期末未平仓持仓测试 (2026-07-21, 用户决策)。

请求区间终点仍持仓的: 不平仓、不做退市强平 (reason=11), 按市值计入权益,
并导出 open_positions 明细供报告展示。
走真实 BacktestLoop (不 mock 核心循环), 只 mock 取数, 不触 TDX。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backtest.engine as engine_module
from backtest.engine import BacktestEngine


DAYS = ["2026-06-22", "2026-06-23"]
BARS = 48


def _grid_index(days):
    out = []
    for d in days:
        t = pd.Timestamp(f"{d} 09:35")
        for _ in range(24):
            out.append(t)
            t += pd.Timedelta(minutes=5)
        t = pd.Timestamp(f"{d} 13:05")
        for _ in range(24):
            out.append(t)
            t += pd.Timedelta(minutes=5)
    return pd.DatetimeIndex(out)


def _run(monkeypatch, close, selections, stop_config, config=None):
    """真实 loop 驱动: 只 mock 窗口取数 + 涨停过滤。"""
    idx = close.index
    kline = {"Close": close, "High": close * 1.005, "Low": close * 0.995,
             "Open": close.copy()}
    mask = pd.DataFrame(True, index=idx, columns=close.columns)
    monkeypatch.setattr(
        engine_module.DataFetcher, "get_kline_windowed",
        staticmethod(lambda selections, period, window_trading_days, dividend_type,
                     fill_data, use_cache=False, end_time=None: (kline, mask)))
    monkeypatch.setattr(BacktestEngine, "_filter_limit_up",
                        lambda self, entries, prices: entries)
    eng = BacktestEngine(config or {"period": "5m", "initial_capital": 100000.0})
    return eng.run(selections=selections,
                   start_time=DAYS[0].replace("-", ""), end_time=DAYS[-1].replace("-", ""),
                   stop_config=stop_config)


def _flat_close(v1=10.0, v2=11.0):
    """day1 恒 v1, day2 恒 v2。"""
    idx = _grid_index(DAYS)
    vals = [v1] * BARS + [v2] * BARS
    return pd.DataFrame({"600001.SH": vals}, index=idx)


_ALL_STOPS_OFF = {
    "cost_stop": {"enabled": False},
    "trailing_stop": {"enabled": False},
    "ladder_tp": {"enabled": False},
    "time_stop": {"enabled": False},
    "cond_time_stop": {"enabled": False},
    "first_day": {"enabled": False},
}


def test_position_open_at_end_not_liquidated(monkeypatch):
    """期末仍持仓: 无卖出 (尤其无 reason=11 退市强平), 权益=现金+市值, 导出明细。"""
    close = _flat_close(10.0, 11.0)
    sel = pd.DataFrame([{"select_date": pd.Timestamp(DAYS[0]), "stock_code": "600001.SH"}])
    result = _run(monkeypatch, close, sel, _ALL_STOPS_OFF)

    # 无退市强平: 没有任何卖出记录
    assert result.trades.empty, f"不应有卖出 (含退市强平), 实际: {result.trades.to_dict('records')}"

    # open_positions 明细
    ops = result.get("open_positions")
    assert ops is not None and len(ops) == 1, "应有 1 笔未平仓"
    op = ops[0]
    assert op["stock_code"] == "600001.SH"
    assert op["entry_price"] == pytest.approx(10.0)
    assert op["shares"] == 2000  # min(现金, max_buy=20000) / 10 元 / 100股整手
    assert op["last_price"] == pytest.approx(11.0)
    assert op["market_value"] == pytest.approx(22000.0)
    assert op["unrealized_pnl"] == pytest.approx(2000.0)
    assert op["unrealized_pct"] == pytest.approx(0.1)
    assert str(op["entry_date"]).startswith(DAYS[0])

    # 权益最末 bar = 现金 + 市值 (买入成本 = 2000股 × 10 × (1+滑点0.001) × (1+佣金0.0003))
    eq = result.equity_curve["equity"].iloc[-1]
    cost = 2000 * 10.0 * (1 + 0.001) * (1 + 0.0003)
    assert eq == pytest.approx(100000.0 - cost + 22000.0, rel=1e-9)


def test_open_positions_remainder_after_ladder_partial_sell(monkeypatch):
    """阶梯止盈部分卖出后, 剩余股数仍计入未平仓。"""
    close = _flat_close(10.0, 12.0)  # day2 +20%, 触发 +10% 档卖 50%
    sel = pd.DataFrame([{"select_date": pd.Timestamp(DAYS[0]), "stock_code": "600001.SH"}])
    stop = dict(_ALL_STOPS_OFF)
    stop["ladder_tp"] = {"enabled": True,
                         "levels": [{"profit": 0.10, "sell_ratio": 0.5}]}
    result = _run(monkeypatch, close, sel, stop)

    assert not result.trades.empty, "阶梯部分卖应有成交"
    sold = int(result.trades["shares"].sum())
    assert 0 < sold < 2000

    ops = result.get("open_positions")
    assert ops is not None and len(ops) == 1
    assert ops[0]["shares"] == 2000 - sold
    # 权益自洽: 末 bar 权益 = 现金 + 剩余市值
    eq = result.equity_curve["equity"].iloc[-1]
    assert eq > 100000.0  # 整体盈利


def test_no_open_positions_key_when_all_closed(monkeypatch):
    """全部平仓 → 结果不含 open_positions 键 (响应形状不变, 同 degradation 先例)。"""
    close = _flat_close(10.0, 10.5)
    sel = pd.DataFrame([{"select_date": pd.Timestamp(DAYS[0]), "stock_code": "600001.SH"}])
    stop = dict(_ALL_STOPS_OFF)
    stop["time_stop"] = {"enabled": True, "max_hold_days": 1}  # 次日必平
    result = _run(monkeypatch, close, sel, stop)

    assert not result.trades.empty, "时间止损应有卖出"
    assert "open_positions" not in result


def test_end_beyond_data_no_phantom_delisting(monkeypatch):
    """2026-07-21 审计: 请求终点晚于数据末端 (如缓存到 6.23, end=6.25) 时,
    降级网格尾部的全 NaN 日不得把持仓误判退市强平 (reason=11) —
    网格应裁到最后有数据的交易日, 持仓保持 open 按市值计价。"""
    import numpy as _np
    days_cal = list(pd.bdate_range("2026-06-22", periods=4))  # 6.22~6.25
    idx = _grid_index(DAYS)  # 数据只到 6.23
    close = pd.DataFrame({"600001.SH": [10.0] * (2 * BARS)}, index=idx)
    kline = {"Close": close, "High": close * 1.005, "Low": close * 0.995,
             "Open": close.copy()}
    mask = pd.DataFrame(True, index=idx, columns=close.columns)
    c1 = pd.DataFrame({"600001.SH": [10.0, 10.1]},
                      index=pd.DatetimeIndex([pd.Timestamp(d) for d in DAYS]))
    kline_1d = {"Close": c1, "High": c1 + 0.1, "Low": c1 - 0.1, "Open": c1 - 0.05}

    monkeypatch.setattr(
        engine_module.DataFetcher, "get_kline_windowed",
        staticmethod(lambda selections, period, window_trading_days, dividend_type,
                     fill_data, use_cache=False, end_time=None: (kline, mask)))
    monkeypatch.setattr(
        engine_module.DataFetcher, "get_trading_days",
        classmethod(lambda cls, start, end, market="SH": days_cal))
    monkeypatch.setattr(
        engine_module.DataFetcher, "get_kline",
        classmethod(lambda cls, stock_list, start_time="", end_time="", period="1d",
                    dividend_type="front", count=-1, fill_data=True, field_list=None,
                    **kw: kline_1d))
    monkeypatch.setattr(BacktestEngine, "_filter_limit_up",
                        lambda self, entries, prices: entries)
    monkeypatch.setattr(BacktestEngine, "_limit_ratio_vector",
                        lambda self, columns: _np.full(len(columns), 0.10))

    eng = BacktestEngine({"period": "5m", "degrade_5m": True,
                          "initial_capital": 100000.0})
    sel = pd.DataFrame([{"select_date": pd.Timestamp(DAYS[0]), "stock_code": "600001.SH"}])
    result = eng.run(selections=sel, start_time="20260622", end_time="20260625",
                     stop_config=_ALL_STOPS_OFF)

    assert result.trades.empty, (
        f"终点晚于数据末端不应产生退市强平, 实际: {result.trades.to_dict('records')}")
    ops = result.get("open_positions")
    assert ops is not None and len(ops) == 1
    # 权益网格裁到最后数据日 6.23, 不含 6.24/6.25 的全 NaN 行
    eq_dates = pd.to_datetime(result.equity_curve["date"])
    assert eq_dates.iloc[-1] == pd.Timestamp("2026-06-23 15:00")
