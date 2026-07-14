"""候选 A 阶段 2 — BacktestLoop vs _simulate_core_v3 字节级 parity 测试。

stage 3/4 核心验证: 同一份数据 + 同一套参数, 新循环 BacktestLoop 必须与
旧 _simulate_core_v3 产出**完全一致**的 equity_arr 与 raw_trades。

覆盖矩阵（v3 计划书 §4.2）:
  3 priority × 4 capability × 2 trade_count × 2 trailing 状态 + 边界场景
"""

from __future__ import annotations

import numpy as np
import pytest

from backtest.engine import _simulate_core_v3, _simulate_core_v3_legacy


# ─────────────────────────────────────────────────────────────
# 数据生成
# ─────────────────────────────────────────────────────────────
def make_synthetic(n_dates=40, n_stocks=4, seed=42, vol=0.02):
    """合成 OHLC + 信号。返回 price/high/low/open/entry。"""
    rng = np.random.default_rng(seed)
    base = 10.0 * np.cumprod(1.0 + rng.normal(0, vol, (n_dates, n_stocks)), axis=0)
    price = base
    high = price * (1 + np.abs(rng.normal(0, vol, (n_dates, n_stocks))))
    low = price * (1 - np.abs(rng.normal(0, vol, (n_dates, n_stocks))))
    open_ = price * (1 + rng.normal(0, vol * 0.5, (n_dates, n_stocks)))
    # 信号: 每 stock 在前 1/3 随机几个 bar 发信号
    entry = np.zeros((n_dates, n_stocks), dtype=bool)
    for ci in range(n_stocks):
        bars = rng.choice(n_dates // 3, size=3, replace=False)
        entry[bars, ci] = True
    return price, high, low, open_, entry


def make_crafted_dual_trigger():
    """构造 trailing_first 双触发场景: ladder 部分卖 + trailing 全卖剩余。

    stock0: 入场 10 → 冲高 11.5（触发 ladder 第一档 0.06 + trailing 激活）
            → 回撤到 10.3（trailing 触及回撤线 11.5*0.9=10.35）
    """
    n = 8
    price = np.full((n, 1), 10.0)
    high = np.full((n, 1), 10.0)
    low = np.full((n, 1), 10.0)
    op = np.full((n, 1), 10.0)
    # bar0 入场 @10; bar1 持有(T+1); bar2 冲高11.5; bar3 回撤10.3
    price[2, 0] = 11.5; high[2, 0] = 11.6; low[2, 0] = 11.2; op[2, 0] = 11.3
    price[3, 0] = 10.3; high[3, 0] = 10.4; low[3, 0] = 10.2; op[3, 0] = 10.5
    entry = np.zeros((n, 1), dtype=bool)
    entry[0, 0] = True
    return price, high, low, op, entry


# ─────────────────────────────────────────────────────────────
# 通用参数 + run_both
# ─────────────────────────────────────────────────────────────
BASE_PARAMS = dict(
    initial_capital=1_000_000.0, commission=0.0003,
    min_buy_amount=1000.0, max_buy_amount=200_000.0, lot_size=100, min_lots=1,
    cost_stop_threshold=-0.05,
    trailing_activation=0.05, trailing_drawdown=0.10,
    ladder_profits=np.array([0.06, 0.15], dtype=np.float64),
    ladder_ratios=np.array([0.5, 0.5], dtype=np.float64),
    n_ladder=2,
    max_hold_days=10,
    cond_time_days=3, cond_time_profit=0.08,
    first_day_target=0.03,
    bpday=1, slippage=0.0, stamp_tax=0.001,
    max_position_pct=1.0,
)


def run_both(price, high, low, open_, entry, *,
             cost_stop_enabled=True, trailing_enabled=True, ladder_enabled=True,
             time_enabled=True, cond_time_enabled=False, first_day_enabled=False,
             ladder_tp_first=False, trailing_first=False,
             formula_exit_np=None, formula_exit_ratio=1.0,
             tradable_np=None, last_tradable_idx=None,
             max_position_pct=1.0, **extra):
    """同一参数跑 legacy(甲骨文) + 兼容壳 _simulate_core_v3, 返回 (eq_old, tr_old, eq_new, tr_new)。"""
    kw = dict(BASE_PARAMS)
    kw.update(dict(
        cost_stop_enabled=cost_stop_enabled, trailing_enabled=trailing_enabled,
        ladder_enabled=ladder_enabled, time_enabled=time_enabled,
        cond_time_enabled=cond_time_enabled, first_day_enabled=first_day_enabled,
        ladder_tp_first=ladder_tp_first, trailing_first=trailing_first,
        formula_exit_np=formula_exit_np, formula_exit_ratio=formula_exit_ratio,
        max_position_pct=max_position_pct,
    ))
    # 位置参数（legacy 与 shell 签名完全一致, 用同一份 args 调两个）
    args = (
        price, entry,
        kw["initial_capital"], kw["commission"],
        kw["min_buy_amount"], kw["max_buy_amount"], kw["lot_size"], kw["min_lots"],
        kw["cost_stop_enabled"], kw["cost_stop_threshold"],
        kw["trailing_enabled"], kw["trailing_activation"], kw["trailing_drawdown"],
        kw["ladder_enabled"], kw["ladder_profits"], kw["ladder_ratios"], kw["n_ladder"],
        kw["time_enabled"], kw["max_hold_days"],
        kw["cond_time_enabled"], kw["cond_time_days"], kw["cond_time_profit"],
        kw["first_day_enabled"], kw["first_day_target"],
        1, high, low, kw["bpday"], kw["slippage"], kw["stamp_tax"],
        tradable_np, last_tradable_idx, open_,
        kw["formula_exit_np"], kw["formula_exit_ratio"], 1,
        kw["ladder_tp_first"], kw["trailing_first"], kw["max_position_pct"],
    )
    # ── 甲骨文: legacy 527 行原版 ──
    eq_old, tr_old = _simulate_core_v3_legacy(*args)
    # ── 兼容壳: 转调 BacktestLoop ──
    eq_new, tr_new = _simulate_core_v3(*args)
    return eq_old, tr_old, eq_new, tr_new


def assert_parity(eq_old, tr_old, eq_new, tr_new, msg=""):
    assert eq_old.shape == eq_new.shape, f"equity shape mismatch {msg}"
    assert np.array_equal(eq_old, eq_new), (
        f"equity 不一致 {msg}\nold={eq_old}\nnew={eq_new}\n"
        f"diff={np.where(eq_old != eq_new)}")
    assert tr_old.shape == tr_new.shape, (
        f"trades shape mismatch {msg}: old={tr_old.shape} new={tr_new.shape}")
    if tr_old.shape[0] > 0:
        assert np.array_equal(tr_old, tr_new), (
            f"trades 不一致 {msg}\nold={tr_old}\nnew={tr_new}")


# ─────────────────────────────────────────────────────────────
# 核心 parity: 3 priority × 多 seed
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("seed", [42, 7, 123, 2024])
@pytest.mark.parametrize("priority", ["stop_first", "ladder_tp_first", "trailing_first"])
def test_parity_synthetic_all_priority(seed, priority):
    price, high, low, open_, entry = make_synthetic(seed=seed)
    kw = dict(ladder_tp_first=(priority == "ladder_tp_first"),
              trailing_first=(priority == "trailing_first"))
    eq_old, tr_old, eq_new, tr_new = run_both(
        price, high, low, open_, entry, **kw)
    assert_parity(eq_old, tr_old, eq_new, tr_new, f"priority={priority} seed={seed}")


# ─────────────────────────────────────────────────────────────
# capability 开关组合（4 组）
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cost,trail,ladder,time", [
    (True, True, True, True),    # 全开
    (False, True, True, True),   # cost off
    (True, False, True, True),   # trail off
    (True, True, False, True),   # ladder off
    (False, False, False, True),  # 仅 time
])
def test_parity_capability_combos(cost, trail, ladder, time):
    price, high, low, open_, entry = make_synthetic(seed=42)
    eq_old, tr_old, eq_new, tr_new = run_both(
        price, high, low, open_, entry,
        cost_stop_enabled=cost, trailing_enabled=trail,
        ladder_enabled=ladder, time_enabled=time)
    assert_parity(eq_old, tr_old, eq_new, tr_new, f"cap=({cost},{trail},{ladder},{time})")


# ─────────────────────────────────────────────────────────────
# 双触发专项（CA1 核心场景）
# ─────────────────────────────────────────────────────────────
def test_parity_trailing_first_dual_trigger():
    price, high, low, open_, entry = make_crafted_dual_trigger()
    eq_old, tr_old, eq_new, tr_new = run_both(
        price, high, low, open_, entry, trailing_first=True)
    # 确认旧版确实产生了交易（双触发应 ≥2 笔或至少 1 笔）
    assert tr_old.shape[0] >= 1, "crafted 数据应触发交易"
    assert_parity(eq_old, tr_old, eq_new, tr_new, "trailing_first dual")


# ─────────────────────────────────────────────────────────────
# formula_sell（绝对优先级）
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("priority", ["stop_first", "ladder_tp_first", "trailing_first"])
def test_parity_formula_sell(priority):
    price, high, low, open_, entry = make_synthetic(seed=99)
    n, k = price.shape
    fsig = np.zeros((n, k), dtype=bool)
    # 在若干 bar 发公式卖出信号
    fsig[10, 0] = True
    fsig[15, 1] = True
    fsig[20, 2] = True
    kw = dict(ladder_tp_first=(priority == "ladder_tp_first"),
              trailing_first=(priority == "trailing_first"),
              formula_exit_np=fsig, formula_exit_ratio=1.0)
    eq_old, tr_old, eq_new, tr_new = run_both(price, high, low, open_, entry, **kw)
    assert_parity(eq_old, tr_old, eq_new, tr_new, f"formula_sell {priority}")


def test_parity_formula_sell_partial():
    price, high, low, open_, entry = make_synthetic(seed=55)
    n, k = price.shape
    fsig = np.zeros((n, k), dtype=bool)
    fsig[12, 0] = True
    eq_old, tr_old, eq_new, tr_new = run_both(
        price, high, low, open_, entry, formula_exit_np=fsig, formula_exit_ratio=0.4)
    assert_parity(eq_old, tr_old, eq_new, tr_new, "formula_sell partial 0.4")


# ─────────────────────────────────────────────────────────────
# 退市 + 停牌
# ─────────────────────────────────────────────────────────────
def test_parity_delisting():
    price, high, low, open_, entry = make_synthetic(seed=11, n_stocks=3)
    n, k = price.shape
    tradable = np.ones((n, k), dtype=bool)
    # stock1 在 bar 20 后退市
    tradable[21:, 1] = False
    last_tradable = np.full(k, -1, dtype=np.int64)
    last_tradable[1] = 20
    eq_old, tr_old, eq_new, tr_new = run_both(
        price, high, low, open_, entry,
        tradable_np=tradable, last_tradable_idx=last_tradable)
    assert_parity(eq_old, tr_old, eq_new, tr_new, "delisting")


def test_parity_suspension():
    price, high, low, open_, entry = make_synthetic(seed=23)
    n, k = price.shape
    tradable = np.ones((n, k), dtype=bool)
    tradable[10:13, 0] = False  # 临时停牌（无 last_tradable_idx → 不算退市）
    last_tradable = np.full(k, -1, dtype=np.int64)
    eq_old, tr_old, eq_new, tr_new = run_both(
        price, high, low, open_, entry,
        tradable_np=tradable, last_tradable_idx=last_tradable)
    assert_parity(eq_old, tr_old, eq_new, tr_new, "suspension")


# ─────────────────────────────────────────────────────────────
# 多持仓 + 换股
# ─────────────────────────────────────────────────────────────
def test_parity_multi_holding_and_replace():
    # 多个股票同时发信号 → 多持仓; 后续同股再发信号 → 换股
    price, high, low, open_, entry = make_synthetic(seed=88, n_stocks=5)
    eq_old, tr_old, eq_new, tr_new = run_both(price, high, low, open_, entry)
    assert_parity(eq_old, tr_old, eq_new, tr_new, "multi-holding+replace")


# ─────────────────────────────────────────────────────────────
# max_position_pct
# ─────────────────────────────────────────────────────────────
def test_parity_max_position_pct():
    price, high, low, open_, entry = make_synthetic(seed=314)
    eq_old, tr_old, eq_new, tr_new = run_both(
        price, high, low, open_, entry, max_position_pct=0.2)
    assert_parity(eq_old, tr_old, eq_new, tr_new, "max_position_pct=0.2")


# ─────────────────────────────────────────────────────────────
# 边界场景（v3 §4.2 +6 组: CR3/HR2/MR3/MR4/bpday/cond_time/first_day）
# ─────────────────────────────────────────────────────────────
def test_parity_consecutive_ladder_partial():
    """CR3: ladder 部分卖 bar i → bar i+1 再触发, bitmask 跨 bar 累计。"""
    n = 10
    price = np.full((n, 1), 10.0)
    high = np.full((n, 1), 10.0)
    low = np.full((n, 1), 10.0)
    op = np.full((n, 1), 10.0)
    # bar0 入场; bar1 T+1; bar2 涨到10.7(触发第一档0.06); bar3 涨到11.6(触发第二档0.15)
    price[2, 0] = 10.7; high[2, 0] = 10.8; low[2, 0] = 10.5
    price[3, 0] = 11.6; high[3, 0] = 11.7; low[3, 0] = 11.4
    entry = np.zeros((n, 1), dtype=bool)
    entry[0, 0] = True
    for prio in ["stop_first", "ladder_tp_first", "trailing_first"]:
        eq_old, tr_old, eq_new, tr_new = run_both(
            price, high, low, op, entry,
            ladder_tp_first=(prio == "ladder_tp_first"),
            trailing_first=(prio == "trailing_first"),
            trailing_enabled=False, time_enabled=False, cost_stop_enabled=False)
        assert_parity(eq_old, tr_old, eq_new, tr_new, f"consecutive ladder {prio}")


def test_parity_formula_sell_and_delisting_same_bar():
    """HR2: 同 bar formula_sell(12) + delisting(11) 双 pre-dispatcher。"""
    price, high, low, open_, entry = make_synthetic(seed=71, n_stocks=3)
    n, k = price.shape
    tradable = np.ones((n, k), dtype=bool)
    tradable[15:, 0] = False  # stock0 bar15 起退市
    last_tradable = np.full(k, -1, dtype=np.int64)
    last_tradable[0] = 14
    fsig = np.zeros((n, k), dtype=bool)
    fsig[15, 0] = True  # 同 bar 发 formula_sell 信号
    eq_old, tr_old, eq_new, tr_new = run_both(
        price, high, low, open_, entry,
        tradable_np=tradable, last_tradable_idx=last_tradable,
        formula_exit_np=fsig)
    assert_parity(eq_old, tr_old, eq_new, tr_new, "formula+delisting same bar")


def test_parity_empty_signals():
    """MR3: 空信号数组（entry_np 全 False）→ 不买入, 只有现金。"""
    price, high, low, open_, _ = make_synthetic(seed=19)
    n, k = price.shape
    entry = np.zeros((n, k), dtype=bool)  # 全 False
    eq_old, tr_old, eq_new, tr_new = run_both(price, high, low, open_, entry)
    assert_parity(eq_old, tr_old, eq_new, tr_new, "empty signals")
    # 无信号 → 无交易
    assert tr_old.shape[0] == 0


def test_parity_t1_same_day():
    """MR4: bpday>1, entry 与 exit 同 day 不同 sub-bar, T+1 约束。"""
    n, k = 16, 2
    bpday = 4
    rng = np.random.default_rng(5)
    price = 10.0 * np.cumprod(1.0 + rng.normal(0, 0.01, (n, k)), axis=0)
    high = price * (1 + np.abs(rng.normal(0, 0.01, (n, k))))
    low = price * (1 - np.abs(rng.normal(0, 0.01, (n, k))))
    op = price * (1 + rng.normal(0, 0.005, (n, k)))
    entry = np.zeros((n, k), dtype=bool)
    entry[2, 0] = True  # bar2 (day0) 入场, 同 day 不可卖
    entry[6, 1] = True  # bar6 (day1) 入场
    # 用 run_both 的 BP_PARAMS 但 bpday 需透传 — run_both 不支持, 走直接 args
    kw = dict(BASE_PARAMS)
    args = (price, entry, kw["initial_capital"], kw["commission"],
            kw["min_buy_amount"], kw["max_buy_amount"], kw["lot_size"], kw["min_lots"],
            True, kw["cost_stop_threshold"], True, kw["trailing_activation"], kw["trailing_drawdown"],
            True, kw["ladder_profits"], kw["ladder_ratios"], kw["n_ladder"],
            True, kw["max_hold_days"], False, kw["cond_time_days"], kw["cond_time_profit"],
            False, kw["first_day_target"], 1, high, low, bpday, kw["slippage"], kw["stamp_tax"],
            None, None, op, None, 1.0, 1, False, False, 1.0)
    eq_old, tr_old = _simulate_core_v3_legacy(*args)
    eq_new, tr_new = _simulate_core_v3(*args)
    assert_parity(eq_old, tr_old, eq_new, tr_new, "T+1 bpday=4")


def test_parity_cond_time():
    price, high, low, open_, entry = make_synthetic(seed=66)
    eq_old, tr_old, eq_new, tr_new = run_both(
        price, high, low, open_, entry, cond_time_enabled=True)
    assert_parity(eq_old, tr_old, eq_new, tr_new, "cond_time")


def test_parity_first_day():
    price, high, low, open_, entry = make_synthetic(seed=77, n_stocks=3)
    eq_old, tr_old, eq_new, tr_new = run_both(
        price, high, low, open_, entry, first_day_enabled=True)
    assert_parity(eq_old, tr_old, eq_new, tr_new, "first_day")


@pytest.mark.parametrize("trail_act", [0.05, 0.20])  # 激活线低=易激活 / 高=难激活
@pytest.mark.parametrize("cost_thr", [-0.05, -0.15])  # 阈值浅=易触发 / 深=难触发
def test_parity_trailing_and_cost_states(trail_act, cost_thr):
    """2 trailing 状态 × 2 cost 阈值 → 覆盖 trade_count/trailing 维度。"""
    price, high, low, open_, entry = make_synthetic(seed=123)
    kw = dict(BASE_PARAMS)
    args = (price, entry, kw["initial_capital"], kw["commission"],
            kw["min_buy_amount"], kw["max_buy_amount"], kw["lot_size"], kw["min_lots"],
            True, cost_thr, True, trail_act, kw["trailing_drawdown"],
            True, kw["ladder_profits"], kw["ladder_ratios"], kw["n_ladder"],
            True, kw["max_hold_days"], False, kw["cond_time_days"], kw["cond_time_profit"],
            False, kw["first_day_target"], 1, high, low, 1, kw["slippage"], kw["stamp_tax"],
            None, None, open_, None, 1.0, 1, False, False, 1.0)
    eq_old, tr_old = _simulate_core_v3_legacy(*args)
    eq_new, tr_new = _simulate_core_v3(*args)
    assert_parity(eq_old, tr_old, eq_new, tr_new,
                  f"trail_act={trail_act} cost_thr={cost_thr}")


@pytest.mark.parametrize("priority", ["stop_first", "ladder_tp_first", "trailing_first"])
@pytest.mark.parametrize("seed", [9, 21, 88, 200])
def test_parity_priority_more_seeds(priority, seed):
    """补足 3×4=12 组核心对照（不同 seed 覆盖 trade_count/trailing 维度）。"""
    price, high, low, open_, entry = make_synthetic(seed=seed)
    eq_old, tr_old, eq_new, tr_new = run_both(
        price, high, low, open_, entry,
        ladder_tp_first=(priority == "ladder_tp_first"),
        trailing_first=(priority == "trailing_first"))
    assert_parity(eq_old, tr_old, eq_new, tr_new, f"more {priority} seed={seed}")


def test_parity_no_high_low():
    """high_np/low_np=None 退化路径（用 close 代 high/low）。"""
    price, _, _, _, entry = make_synthetic(seed=44)
    n, k = price.shape
    kw = dict(BASE_PARAMS)
    args = (price, entry, kw["initial_capital"], kw["commission"],
            kw["min_buy_amount"], kw["max_buy_amount"], kw["lot_size"], kw["min_lots"],
            True, kw["cost_stop_threshold"], True, kw["trailing_activation"], kw["trailing_drawdown"],
            True, kw["ladder_profits"], kw["ladder_ratios"], kw["n_ladder"],
            True, kw["max_hold_days"], False, kw["cond_time_days"], kw["cond_time_profit"],
            False, kw["first_day_target"], 1, None, None, 1, kw["slippage"], kw["stamp_tax"],
            None, None, None, None, 1.0, 1, False, False, 1.0)
    eq_old, tr_old = _simulate_core_v3_legacy(*args)
    eq_new, tr_new = _simulate_core_v3(*args)
    assert_parity(eq_old, tr_old, eq_new, tr_new, "no high/low")


def test_parity_slippage_stamp():
    """非零滑点 + 印花税。"""
    price, high, low, open_, entry = make_synthetic(seed=300)
    kw = dict(BASE_PARAMS)
    args = (price, entry, kw["initial_capital"], kw["commission"],
            kw["min_buy_amount"], kw["max_buy_amount"], kw["lot_size"], kw["min_lots"],
            True, kw["cost_stop_threshold"], True, kw["trailing_activation"], kw["trailing_drawdown"],
            True, kw["ladder_profits"], kw["ladder_ratios"], kw["n_ladder"],
            True, kw["max_hold_days"], False, kw["cond_time_days"], kw["cond_time_profit"],
            False, kw["first_day_target"], 1, high, low, 1, 0.002, 0.001,
            None, None, open_, None, 1.0, 1, False, False, 1.0)
    eq_old, tr_old = _simulate_core_v3_legacy(*args)
    eq_new, tr_new = _simulate_core_v3(*args)
    assert_parity(eq_old, tr_old, eq_new, tr_new, "slippage=0.002 stamp=0.001")
