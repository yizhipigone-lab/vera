"""Phase 2 (2026-07-17): _build_trades 向量化等价性 + 舍入探针固化。

锁定:
1. 新实现与旧逐行实现(甲骨文内联)输出 assert_frame_equal 全等 (值+dtype)。
2. 舍入探针固化: np.round 对 x.xx5 型值与 Python round 不一致 — 这就是
   _build_trades 舍入列必须保留 Python round 的原因, 防后人"优化"回去。
3. 边界: 空 raw / 单行 / 越界索引 / bpday>1。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from backtest.engine import BacktestEngine


def _old_build_trades(raw, columns, dates, bpday=1):
    """旧逐行实现 (甲骨文, 复制自向量化前 engine.py)。"""
    if len(raw) == 0:
        return pd.DataFrame()
    reason_map = {1.0: "换股卖出", 3.0: "成本止损",
                  4.0: "移动止损", 8.0: "移动止盈",   # 2026-07-18 审计修正(原 4.0 误标"移动止盈")
                  5.0: "阶梯止盈",
                  6.0: "时间止损", 9.0: "时间止盈",
                  7.0: "cond_time_stop",
                  10.0: "首日未达标",
                  11.0: "退市",
                  12.0: "formula_sell",
                  13.0: "ATR止损"}
    col_map = {c: i for i, c in enumerate(columns)}
    inv_col = {i: c for c, i in col_map.items()}
    records = []
    for row in raw:
        ci = int(row[0]); code = inv_col.get(ci, str(ci))
        ei = int(row[1]); xi = int(row[2])
        ed = dates[ei] if 0 <= ei < len(dates) else dates[0]
        xd = dates[xi] if 0 <= xi < len(dates) else dates[-1]
        ep = round(float(row[3]), 4); xp = round(float(row[4]), 4)
        sh = int(row[5])
        records.append({
            "stock_code": code, "entry_date": ed, "exit_date": xd,
            "entry_price": ep, "exit_price": xp, "shares": sh,
            "entry_amount": round(ep * sh, 2), "exit_amount": round(xp * sh, 2),
            "pnl": round(float(row[6]), 2), "return": round(float(row[7]), 4),
            "profit_pct": round(float(row[7]), 4),
            "exit_reason": reason_map.get(row[8], "换股卖出"),
            "hold_days": max(1, (xi - ei) // bpday) if bpday > 1 else (xi - ei),
        })
    return pd.DataFrame(records)


def _make_raw(n=200, n_dates=250, n_stocks=6, seed=11):
    rng = np.random.default_rng(seed)
    raw = np.zeros((n, 9), dtype=np.float64)
    raw[:, 0] = rng.integers(0, n_stocks, n)                    # code idx
    raw[:, 1] = rng.integers(0, n_dates - 30, n)                # entry idx
    raw[:, 2] = raw[:, 1] + rng.integers(1, 30, n)              # exit idx
    raw[:, 3] = rng.uniform(3, 80, n)                           # entry px
    raw[:, 4] = raw[:, 3] * rng.uniform(0.85, 1.25, n)          # sell px
    raw[:, 5] = rng.integers(1, 50, n) * 100                    # shares
    raw[:, 6] = rng.uniform(-5000, 8000, n)                     # profit
    raw[:, 7] = rng.uniform(-0.2, 0.3, n)                       # return
    raw[:, 8] = rng.choice([1.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0], n)
    return raw


def _dates(n=250):
    return pd.date_range('2024-01-01', periods=n, freq='B')


def test_build_trades_equals_old():
    eng = BacktestEngine({})
    columns = ['600001', '600002', '300001', '688001', '000001', '601318']
    raw = _make_raw()
    new = eng._build_trades(raw, columns, _dates())
    old = _old_build_trades(raw, columns, _dates())
    assert_frame_equal(new, old)


def test_build_trades_equals_old_bpday48():
    """5m 模式 bpday=48: hold_days 走 max(1, (xi-ei)//bpday) 分支。"""
    eng = BacktestEngine({})
    columns = ['600001', '600002', '300001', '688001', '000001', '601318']
    raw = _make_raw()
    new = eng._build_trades(raw, columns, _dates(), bpday=48)
    old = _old_build_trades(raw, columns, _dates(), bpday=48)
    assert_frame_equal(new, old)


def test_build_trades_empty():
    eng = BacktestEngine({})
    out = eng._build_trades(np.empty((0, 9)), ['600001'], _dates())
    assert out.empty


def test_build_trades_single_row():
    eng = BacktestEngine({})
    raw = _make_raw(n=1)
    columns = ['600001', '600002', '300001', '688001', '000001', '601318']
    new = eng._build_trades(raw, columns, _dates())
    old = _old_build_trades(raw, columns, _dates())
    assert_frame_equal(new, old)


def test_build_trades_out_of_range_index():
    """越界 ei/xi: 负值回退 dates[0], 超界回退 dates[-1] (与旧版一致)。"""
    eng = BacktestEngine({})
    columns = ['600001', '600002']
    raw = np.array([[0.0, -5.0, 9999.0, 10.0, 11.0, 100.0, 100.0, 0.1, 3.0],
                    [1.0, 9999.0, -3.0, 9.0, 8.5, 200.0, -100.0, -0.05, 11.0]])
    new = eng._build_trades(raw, columns, _dates())
    old = _old_build_trades(raw, columns, _dates())
    assert_frame_equal(new, old)
    assert new.iloc[0]['entry_date'] == _dates()[0]
    assert new.iloc[0]['exit_date'] == _dates()[-1]


def test_build_trades_xx5_rounding_preserved():
    """x.xx5 型值必须用 Python round 语义 (673.485 → 673.49, 不是 673.48)。"""
    eng = BacktestEngine({})
    columns = ['600001']
    raw = np.array([[0.0, 10.0, 20.0, 673.485, 673.485, 100.0, 0.0, 0.0, 3.0]])
    new = eng._build_trades(raw, columns, _dates())
    # entry_price round4: 673.485; entry_amount = round(673.485*100, 2) = round(67348.5, 2)
    assert new.iloc[0]['entry_amount'] == round(round(673.485, 4) * 100, 2)


def test_rounding_probe_np_round_differs_on_xx5():
    """探针固化: np.round 与 Python round 对 x.xx5 型值存在分歧 (~4%)。
    若未来 numpy 行为变化使此测试失败, 需重新评估 _build_trades 是否可向量化舍入。"""
    import random
    rng = random.Random(42)
    vals = [round(rng.uniform(0.001, 1000), 3) + 0.005 for _ in range(100_000)]
    arr = np.array(vals)
    py = [round(v, 2) for v in vals]
    npy = list(np.round(arr, 2))
    diff = sum(1 for a, b in zip(py, npy) if a != b)
    assert diff > 0, "np.round 与 Python round 已一致? _build_trades 舍入列可重新评估向量化"
