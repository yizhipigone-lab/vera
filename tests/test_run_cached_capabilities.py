"""
候选 A 阶段 1 — run_cached 能力透传测试 (RED)

验证深化后的 run_cached 正确透传三类能力:
  - formula_exit_np → reason=12 (公式卖出)
  - tradable_np + last_tradable_idx → reason=11 (退市)
  - open_np → 跳空保护 (不崩 + 产生交易)
  - capabilities 三开关 off → 对应能力不触发
  - 无能力数据 → 旧行为 (无 reason 11/12)

深化前 run_cached 不认识 filter_limit_up/return_raw/formula_exit_np 等 keyword
→ 全部 TypeError 失败 (RED)。深化后全绿 (GREEN)。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest

from backtest.engine import BacktestEngine


def _make_engine():
    """realistic_costs=False → slippage/stamp=0, 数值干净, 匹配 4 直调脚本口径."""
    return BacktestEngine({
        "initial_capital": 1_000_000.0,
        "commission": 0.0003,
        "enable_realistic_costs": False,
        "period": "1d",
        "position_sizing": {
            "min_buy_amount": 1000.0,
            "max_buy_amount": 60_000.0,
            "lot_size": 100,
            "min_lots": 1,
        },
    })


def _stop_config(priority="stop_first", capabilities=None):
    """宽松止损 (不触发 cost_stop/trailing), 留出空间让能力信号主导."""
    return {
        "priority": priority,
        "cost_stop": {"enabled": True, "threshold": -0.30},
        "trailing_stop": {"enabled": False, "activation": 0.50, "drawdown": 0.30},
        "ladder_tp": {"enabled": False, "levels": []},
        "time_stop": {"enabled": True, "max_hold_days": 8},
        "cond_time_stop": {"enabled": False, "days": 9999, "profit": 0.99},
        "first_day": {"enabled": False, "target": 0.03},
        "formula_sell": {
            "enabled": False, "formula_name": "", "formula_arg": "",
            "sell_ratio": 1.0, "priority": 0,
        },
        "capabilities": capabilities or {
            "formula_exit": True, "gap_protection": True, "delisting": True,
        },
    }


def _make_market(n_dates=20):
    """bar 5 信号 → 当日收盘买入 (entry_bar=5). 价格 500 元, 100 股=5 万 < max_buy 6 万."""
    dates = pd.bdate_range('2024-01-02', periods=n_dates)
    columns = ['600519.SH']
    close = pd.DataFrame(np.full((n_dates, 1), 500.0), index=dates, columns=columns)
    high = close * 1.02
    low = close * 0.98
    open_ = close.copy()
    entries = pd.DataFrame(False, index=dates, columns=columns)
    entries.iloc[5, 0] = True
    return dates, close, high, low, open_, entries, columns


def _run(eng, close, entries, high, low, sc, **cap_kwargs):
    """统一调用深化后 run_cached; filter_limit_up=False 复现直调口径, return_raw=True 拿 raw_trades."""
    return eng.run_cached(
        close, entries,
        high.values.astype(np.float64), low.values.astype(np.float64),
        sc, None, np.array([]), np.array([]), 0,
        skip_sm=True,
        filter_limit_up=False, return_raw=True,
        **cap_kwargs,
    )


# ---------------------------------------------------------------------------
# Test 1: formula_exit_np 透传 → reason=12
# ---------------------------------------------------------------------------
def test_run_cached_forwards_formula_exit():
    """bar 5 买入, formula_exit_np[6,0]=True → bar 7 查 matrix[6] 触发 reason=12."""
    dates, close, high, low, open_, entries, columns = _make_market()
    matrix = np.zeros((len(dates), 1), dtype=bool)
    matrix[6, 0] = True  # bar 7 卖出循环查 matrix[7-1=6] → 触发

    eng = _make_engine()
    result = _run(eng, close, entries, high, low, _stop_config(),
                  formula_exit_np=matrix, formula_exit_ratio=1.0,
                  open_np=open_.values.astype(np.float64))

    rt = result["raw_trades"]
    assert len(rt) >= 1, "应有 1 笔公式卖出交易"
    assert int(rt[0][1]) == 5, f"entry_bar 应=5 (信号日收盘买入), 实际 {int(rt[0][1])}"
    assert int(rt[0][2]) == 7, f"exit_bar 应=7 (查 matrix[6] 触发), 实际 {int(rt[0][2])}"
    assert int(rt[0][8]) == 12, f"reason 应=12 (formula_sell), 实际 {int(rt[0][8])}"


# ---------------------------------------------------------------------------
# Test 2: tradable_np + last_tradable_idx 透传 → reason=11 (退市)
# ---------------------------------------------------------------------------
def test_run_cached_forwards_tradable_delisting():
    """bar 5 买入, bar 7 起退市 (tradable=False, last_tradable_idx=6) → reason=11."""
    dates, close, high, low, open_, entries, columns = _make_market()
    n = len(dates)
    tradable = np.ones((n, 1), dtype=bool)
    tradable[7:, 0] = False  # bar 7 起不可交易
    lti = np.array([6], dtype=np.int64)  # 最后可交易 bar = 6

    eng = _make_engine()
    result = _run(eng, close, entries, high, low, _stop_config(),
                  tradable_np=tradable, last_tradable_idx=lti)

    rt = result["raw_trades"]
    reasons = [int(r[8]) for r in rt]
    assert 11 in reasons, f"应有退市 reason=11, 实际 reasons={reasons}"


# ---------------------------------------------------------------------------
# Test 3: open_np 透传不崩 + 产生交易 (跳空保护)
# ---------------------------------------------------------------------------
def test_run_cached_forwards_open_np():
    """传 open_np (跳空保护数据) 不崩, 且产生交易."""
    dates, close, high, low, open_, entries, columns = _make_market()
    eng = _make_engine()
    result = _run(eng, close, entries, high, low, _stop_config(),
                  open_np=open_.values.astype(np.float64))

    rt = result["raw_trades"]
    assert len(rt) >= 1, "传 open_np 应仍能产生交易 (time_stop 兜底)"
    # 不崩 + raw_equity 形状对
    assert result["raw_equity"].shape == (len(dates),)


# ---------------------------------------------------------------------------
# Test 4: capabilities.formula_exit=False → 即使传 matrix 也不触发 reason=12
# ---------------------------------------------------------------------------
def test_run_cached_switch_disables_formula_exit():
    dates, close, high, low, open_, entries, columns = _make_market()
    matrix = np.zeros((len(dates), 1), dtype=bool)
    matrix[6, 0] = True

    eng = _make_engine()
    sc = _stop_config(capabilities={
        "formula_exit": False, "gap_protection": True, "delisting": True,
    })
    result = _run(eng, close, entries, high, low, sc,
                  formula_exit_np=matrix, formula_exit_ratio=1.0,
                  open_np=open_.values.astype(np.float64))

    rt = result["raw_trades"]
    reasons = [int(r[8]) for r in rt]
    assert 12 not in reasons, (
        f"capabilities.formula_exit=False 时不应触发 reason=12, 实际 {reasons}"
    )


# ---------------------------------------------------------------------------
# Test 5: capabilities.delisting=False → 即使传 tradable_np 也不触发 reason=11
# ---------------------------------------------------------------------------
def test_run_cached_switch_disables_delisting():
    dates, close, high, low, open_, entries, columns = _make_market()
    n = len(dates)
    tradable = np.ones((n, 1), dtype=bool)
    tradable[7:, 0] = False
    lti = np.array([6], dtype=np.int64)

    eng = _make_engine()
    sc = _stop_config(capabilities={
        "formula_exit": True, "gap_protection": True, "delisting": False,
    })
    result = _run(eng, close, entries, high, low, sc,
                  tradable_np=tradable, last_tradable_idx=lti)

    rt = result["raw_trades"]
    reasons = [int(r[8]) for r in rt]
    assert 11 not in reasons, (
        f"capabilities.delisting=False 时不应触发 reason=11, 实际 {reasons}"
    )


# ---------------------------------------------------------------------------
# Test 6: 无能力数据 → 旧行为 (无 reason 11/12)
# ---------------------------------------------------------------------------
def test_run_cached_no_data_legacy_behavior():
    """不传三类能力数据 → 三类能力全 off (旧行为), 无 reason 11/12."""
    dates, close, high, low, open_, entries, columns = _make_market()
    eng = _make_engine()
    result = _run(eng, close, entries, high, low, _stop_config())

    rt = result["raw_trades"]
    reasons = [int(r[8]) for r in rt] if len(rt) else []
    assert 11 not in reasons and 12 not in reasons, (
        f"无能力数据时不应有 reason 11/12, 实际 {reasons}"
    )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
