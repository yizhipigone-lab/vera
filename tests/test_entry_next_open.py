"""open_t1 信号平移 + 一字板拒买 单元测试 (计划书 Task 1).

计划书: docs/plan/2026-08-20_回测次日开盘买入模式_计划书.md
"""
import numpy as np
import pandas as pd

from backtest.entry_next_open import shift_entries_to_next_open

RATIO = np.array([0.10])


def _mk(dates, o, h, l, c):
    """构造单股四价 DataFrame。"""
    idx = pd.to_datetime(dates)
    cols = ["600001"]
    mk = lambda v: pd.DataFrame(np.asarray(v, dtype=float).reshape(-1, 1),
                                index=idx, columns=cols)
    return mk(o), mk(h), mk(l), mk(c)


def test_shift_to_next_open_basic():
    """T 日信号 → T+1 bar, 买入价 = T+1 开盘价 (由调用方取 open 矩阵)。"""
    o, h, l, c = _mk(["2026-08-17", "2026-08-18"],
                     [10.0, 11.0], [10.5, 11.5], [9.9, 10.8], [10.4, 11.2])
    entries = pd.DataFrame([[True], [False]], index=o.index, columns=o.columns)
    res = shift_entries_to_next_open(entries, o, h, l, c, limit_ratio_vec=RATIO)
    assert res.entries.values[1, 0] == True      # 信号落在 T+1
    assert res.entries.values[0, 0] == False     # T 日信号被移走
    assert res.n_shifted == 1 and res.n_signals == 1
    assert res.n_oneline_limit_up == 0


def test_oneline_limit_up_rejected():
    """T+1 一字涨停(OHLC 全等且达涨停价) → 拒买计数。"""
    # 8-17 收 10.0; 8-18 一字板 11.0 = 10.0*1.1 恰好涨停
    o, h, l, c = _mk(["2026-08-17", "2026-08-18"],
                     [10.0, 11.0], [10.5, 11.0], [9.9, 11.0], [10.0, 11.0])
    entries = pd.DataFrame([[True], [False]], index=o.index, columns=o.columns)
    res = shift_entries_to_next_open(entries, o, h, l, c, limit_ratio_vec=RATIO)
    assert res.entries.values.sum() == 0
    assert res.n_oneline_limit_up == 1
    assert res.n_shifted == 0


def test_oneline_limit_down_not_rejected():
    """一字跌停(OHLC 全等但非涨停) → 可以买 (跌停随便买)。"""
    o, h, l, c = _mk(["2026-08-17", "2026-08-18"],
                     [10.0, 9.0], [10.5, 9.0], [9.9, 9.0], [10.0, 9.0])
    entries = pd.DataFrame([[True], [False]], index=o.index, columns=o.columns)
    res = shift_entries_to_next_open(entries, o, h, l, c, limit_ratio_vec=RATIO)
    assert res.entries.values[1, 0] == True
    assert res.n_oneline_limit_up == 0


def test_limit_open_but_trades_not_rejected():
    """涨停价开盘但盘中有成交(open=limit 但 high≠low) → 可以买。"""
    o, h, l, c = _mk(["2026-08-17", "2026-08-18"],
                     [10.0, 11.0], [10.5, 11.0], [9.9, 10.5], [10.0, 10.8])
    entries = pd.DataFrame([[True], [False]], index=o.index, columns=o.columns)
    res = shift_entries_to_next_open(entries, o, h, l, c, limit_ratio_vec=RATIO)
    assert res.entries.values[1, 0] == True


def test_last_day_signal_dropped():
    """信号日已是最后交易日(无 T+1) → 丢弃+计数 (v3.5: 不外延取数)。"""
    o, h, l, c = _mk(["2026-08-17"], [10.0], [10.5], [9.9], [10.4])
    entries = pd.DataFrame([[True]], index=o.index, columns=o.columns)
    res = shift_entries_to_next_open(entries, o, h, l, c, limit_ratio_vec=RATIO)
    assert res.entries.values.sum() == 0
    assert res.n_no_t1_bar == 1


def test_t1_suspended_whole_day_dropped():
    """T+1 全天开盘价为 NaN(停牌) → 丢弃+计数。"""
    o, h, l, c = _mk(["2026-08-17", "2026-08-18"],
                     [10.0, np.nan], [10.5, np.nan], [9.9, np.nan], [10.4, np.nan])
    entries = pd.DataFrame([[True], [False]], index=o.index, columns=o.columns)
    res = shift_entries_to_next_open(entries, o, h, l, c, limit_ratio_vec=RATIO)
    assert res.entries.values.sum() == 0
    assert res.n_no_tradable_bar == 1


def test_5m_first_bar_of_next_day():
    """分钟级: 信号在 T 日末 bar → 平移到 T+1 首 bar (开盘价=首 bar open)。"""
    idx = pd.to_datetime(["2026-08-17 14:55", "2026-08-17 15:00",
                          "2026-08-18 09:35", "2026-08-18 09:40"])
    cols = ["600001"]
    mk = lambda v: pd.DataFrame(np.asarray(v, float).reshape(-1, 1),
                                index=idx, columns=cols)
    o = mk([10.0, 10.2, 11.0, 11.1])
    h = mk([10.1, 10.3, 11.2, 11.2])
    l = mk([9.9, 10.0, 10.9, 11.0])
    c = mk([10.1, 10.2, 11.1, 11.15])
    entries = pd.DataFrame([[False], [True], [False], [False]],
                           index=idx, columns=cols)
    res = shift_entries_to_next_open(entries, o, h, l, c, limit_ratio_vec=RATIO)
    assert res.entries.values[2, 0] == True   # T+1 首 bar (09:35)
    assert res.entries.values[1, 0] == False
    assert res.n_shifted == 1


def test_tradable_np_skip_suspended_first_bar():
    """T+1 首 bar 停牌(tradable=False), 当日第二根可交易 → 顺延到第二根。"""
    idx = pd.to_datetime(["2026-08-17 15:00",
                          "2026-08-18 09:35", "2026-08-18 09:40"])
    cols = ["600001"]
    mk = lambda v: pd.DataFrame(np.asarray(v, float).reshape(-1, 1),
                                index=idx, columns=cols)
    o = mk([10.0, np.nan, 11.0])
    h = mk([10.1, np.nan, 11.2])
    l = mk([9.9, np.nan, 10.9])
    c = mk([10.0, np.nan, 11.1])
    tradable = np.array([[True], [False], [True]])
    entries = pd.DataFrame([[True], [False], [False]], index=idx, columns=cols)
    res = shift_entries_to_next_open(entries, o, h, l, c, limit_ratio_vec=RATIO,
                                     tradable_np=tradable)
    assert res.entries.values[2, 0] == True   # 顺延到 09:40
    assert res.n_shifted == 1


def test_st_stock_5pct_limit():
    """ST 股涨停幅度 5%: 四价合一 +5% → 拒买; +4% → 不拒。"""
    o, h, l, c = _mk(["2026-08-17", "2026-08-18"],
                     [10.0, 10.5], [10.5, 10.5], [9.9, 10.5], [10.0, 10.5])
    entries = pd.DataFrame([[True], [False]], index=o.index, columns=o.columns)
    res = shift_entries_to_next_open(entries, o, h, l, c,
                                     limit_ratio_vec=np.array([0.05]))
    assert res.n_oneline_limit_up == 1
    # 只涨 4% 的一字 (非涨停) → 不拒
    o2, h2, l2, c2 = _mk(["2026-08-17", "2026-08-18"],
                         [10.0, 10.4], [10.5, 10.4], [9.9, 10.4], [10.0, 10.4])
    res2 = shift_entries_to_next_open(entries, o2, h2, l2, c2,
                                      limit_ratio_vec=np.array([0.05]))
    assert res2.entries.values[1, 0] == True


def test_signals_count_and_multi_stock():
    """多股多信号: 统计口径正确。"""
    idx = pd.to_datetime(["2026-08-17", "2026-08-18"])
    cols = ["600001", "600002"]
    mk = lambda m: pd.DataFrame(np.asarray(m, float), index=idx, columns=cols)
    # 600001: 8-17 收 10.0 → 涨停价 11.0; 8-18 四价合一 11.0 → 一字涨停拒买
    # 600002: 8-18 正常波动 → 平移成功
    o = mk([[10.0, 20.0], [11.0, 22.0]])
    h = mk([[10.5, 20.5], [11.0, 22.5]])
    l = mk([[9.9, 19.9], [11.0, 21.8]])
    c = mk([[10.0, 20.4], [11.0, 22.4]])
    entries = pd.DataFrame([[True, True], [False, False]],
                           index=idx, columns=cols)
    res = shift_entries_to_next_open(
        entries, o, h, l, c, limit_ratio_vec=np.array([0.10, 0.10]))
    assert res.n_signals == 2
    assert res.n_shifted == 1               # 600002 平移成功
    assert res.n_oneline_limit_up == 1      # 600001 一字涨停拒买
    assert res.entries.values[1, 1] == True
