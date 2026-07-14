"""真实数据 parity（审计 T1: 自动化, 防回归）。

把 tools/real_parity_check.py 的真实 TDX 对拍纳入 pytest 套件。
无 TDX / 无信号时 skip, 不阻塞 CI。
"""
from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, r"E:\NEW_TDX\PYPlugins\user")

import numpy as np
import pandas as pd
import pytest


def _tdx_available() -> bool:
    try:
        from core.connector import TdxConnector
        TdxConnector.initialize()
        TdxConnector.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def real_data():
    """拉真实 K 线 + QUANTQQ 信号, 无则 skip。"""
    if not _tdx_available():
        pytest.skip("TDX 不可用, 跳过真实数据 parity")
    from core.connector import TdxConnector
    from core.data_fetcher import DataFetcher
    from core.formula_runner import FormulaRunner
    from utils.config_loader import ConfigLoader
    from backtest.engine import BacktestEngine
    import backtest.engine as eng_mod

    TdxConnector.initialize()
    try:
        stocks = ["601872.SH", "600519.SH", "000858.SZ", "601318.SH", "002475.SZ"]
        start, end = "20260101", "20260704"
        k = DataFetcher.get_kline(stocks, start_time=start, end_time=end,
                                  period="1d", fill_data=False)
        close, high, low, op = k["Close"], k["High"], k["Low"], k["Open"]
        valid = close.columns[close.notna().any()]
        close, high, low, op = close[valid], high[valid], low[valid], op[valid]
        picks = FormulaRunner.run_stock_selection_with_dates(
            formula_name="QUANTQQ", formula_arg="",
            stock_list=list(valid), start_time=start, end_time=end, stock_period="1d")
        entries = pd.DataFrame(False, index=close.index, columns=close.columns)
        if picks is not None and not picks.empty:
            dcol = "select_date" if "select_date" in picks.columns else picks.columns[0]
            ccol = next((c for c in ("stock_code", "code", "stock") if c in picks.columns), None)
            if ccol:
                for _, row in picks.iterrows():
                    d = pd.Timestamp(row[dcol]).normalize()
                    code = row[ccol]
                    if code in close.columns and d in close.index:
                        entries.loc[d, code] = True
        defaults = ConfigLoader.load_defaults()
        yield {
            "close": close, "high": high, "low": low, "op": op,
            "entries": entries, "stop": defaults.get("stop_loss", {}),
            "bt": defaults.get("backtest", {}),
        }
    finally:
        TdxConnector.close()


@pytest.mark.parametrize("priority", ["stop_first", "ladder_tp_first", "trailing_first"])
def test_real_parity(priority, real_data):
    """新壳 _simulate_core_v3 vs legacy, 真实数据字节级一致。"""
    import backtest.engine as eng_mod
    from backtest.engine import BacktestEngine
    d = real_data
    close, high, low, op, entries = d["close"], d["high"], d["low"], d["op"], d["entries"]
    stop = d["stop"]
    bt = d["bt"]
    # ladder 配置
    levels = stop.get("ladder_tp", {}).get("levels", [])
    lv = sorted(levels, key=lambda x: x["profit"])
    if lv:
        lp = np.array([x["profit"] for x in lv], dtype=np.float64)
        lr = np.array([x["sell_ratio"] for x in lv], dtype=np.float64)
    else:
        lp = np.array([0.06, 0.15], dtype=np.float64)
        lr = np.array([0.3, 0.3], dtype=np.float64)
    nl = len(lp)
    eng = BacktestEngine(bt)
    sc = dict(stop); sc["priority"] = priority
    common = dict(close=close, entries=entries,
                  high_np=high.values.astype(np.float64),
                  low_np=low.values.astype(np.float64),
                  stop_config=sc, selections=pd.DataFrame(columns=["select_date", "stock_code"]),
                  ladder_profits=lp, ladder_ratios=lr, n_ladder=nl,
                  filter_limit_up=False, return_raw=True,
                  open_np=op.values.astype(np.float64))
    res_new = eng.run_cached(**common)
    orig = eng_mod._simulate_core_v3
    eng_mod._simulate_core_v3 = eng_mod._simulate_core_v3_legacy
    try:
        res_old = eng.run_cached(**common)
    finally:
        eng_mod._simulate_core_v3 = orig
    assert np.array_equal(res_old["raw_equity"], res_new["raw_equity"]), (
        f"equity 不一致 {priority}")
    assert res_old["raw_trades"].shape == res_new["raw_trades"].shape, (
        f"trades shape 不一致 {priority}: {res_old['raw_trades'].shape} vs {res_new['raw_trades'].shape}")
    if res_old["raw_trades"].shape[0]:
        assert np.array_equal(res_old["raw_trades"], res_new["raw_trades"]), (
            f"trades 不一致 {priority}")
