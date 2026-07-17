"""Phase 3 预筛安全性测试 (2026-07-18)。

1. 假阴性监控(属性测试): 随机 5000 组 (持仓, bar) — 预筛判 False ⇒ 全路径必零触发。
2. 强制开启对照: could_trigger 恒 True 与正常版整loop字节一致。
3. swap-pop 专项: 同 bar 多持仓卖出重排后, 后续持仓判定不错位。
"""
from __future__ import annotations

import numpy as np
import pytest

from backtest.engine import _simulate_core_v3, _simulate_core_v3_legacy
from backtest.loop.builder import build_backtest_loop
from backtest.loop.prefilter import TriggerPreFilter
from backtest.loop.state import Bar, Context, Position
from tests.test_loop_parity import BASE_PARAMS, run_both, assert_parity, make_synthetic


def _build_loop_and_filter(**over):
    kw = dict(
        initial_capital=100000.0, commission=0.0003,
        min_buy_amount=2000.0, max_buy_amount=20000.0, lot_size=100, min_lots=1,
        cost_stop_enabled=True, cost_stop_threshold=-0.12,
        trailing_enabled=True, trailing_activation=0.05, trailing_drawdown=0.03,
        ladder_enabled=True, ladder_profits=np.array([0.06, 0.15]),
        ladder_ratios=np.array([0.5, 0.5]), n_ladder=2,
        time_enabled=True, max_hold_days=20,
        cond_time_enabled=True, cond_time_days=7, cond_time_profit=0.10,
        first_day_enabled=True, first_day_target=0.03,
    )
    kw.update(over)
    loop = build_backtest_loop(**kw)
    return loop


def test_no_false_negative_property():
    """属性测试: 预筛判 False 的随机 (持仓,bar), 全路径 evaluate 必须为空。

    直接证明"跳过=零触发", 这是预筛安全的充要条件。
    """
    loop = _build_loop_and_filter()
    pf = loop._prefilter
    disp = loop.dispatcher
    rng = np.random.default_rng(2026)
    profits = np.array([0.06, 0.15])
    n_checked = 0
    n_skipped = 0
    for _ in range(5000):
        ep = float(rng.uniform(5, 50))
        # 贴近真实持仓的温和分布(大量不触发 + 少量触发边界), 保证跳过样本充足
        hi_pp = float(abs(rng.normal(0.01, 0.04)))
        lo_pp = float(-abs(rng.normal(0.02, 0.04)))
        hi = ep * (1 + hi_pp)
        lo = ep * (1 + lo_pp)
        peak_profit = float(abs(rng.normal(0.02, 0.04)))
        peak = ep * (1 + peak_profit)
        hold = int(rng.integers(0, 25))
        entry_idx = int(rng.integers(0, 100))
        i = entry_idx + hold
        ladder_done = int(rng.integers(0, 4))
        ci = 0
        verdict = pf.could_trigger(
            ci=ci, i=i, ep=ep, hi=hi, lo=lo, hi_pp=hi_pp, lo_pp=lo_pp,
            peak_hi=peak, peak_hi_profit=peak_profit,
            hold_days=hold, entry_idx=entry_idx, bpday=1,
            ladder_done=ladder_done,
            ladder_profits=profits, n_ladder=2)
        n_checked += 1
        if verdict:
            continue
        n_skipped += 1
        # 预筛判 False → 全路径必须零触发
        pos = Position(code=ci, shares=1000.0, entry_px=ep, entry_idx=entry_idx,
                       high_px=ep * 1.01, high_hi=peak, ladder_done=ladder_done)
        bar = Bar(close=ep * 1.0, high=hi, low=lo, open=float("nan"))
        ctx = Context(
            bar_index=i, ci=ci, bpday=1, hold_days=hold,
            entry_px=ep, pp=0.0, hp_profit=0.01,
            peak_hi=peak, peak_hi_profit=peak_profit,
            pos_high_px=ep * 1.01, pos_high_hi=peak,
            hi_pp=hi_pp, lo_pp=lo_pp,
            ladder_profits=profits, ladder_ratios=np.array([0.5, 0.5]), n_ladder=2)
        for abs_s in loop.absolutes:
            assert abs_s.check(pos, bar, ctx) == [], \
                f"假阴性! absolutes 触发但预筛判 False: ep={ep} hi_pp={hi_pp} lo_pp={lo_pp}"
        assert disp.evaluate(pos, bar, ctx) == [], \
            f"假阴性! dispatcher 触发但预筛判 False: ep={ep} hi_pp={hi_pp} lo_pp={lo_pp} hold={hold} ladder_done={ladder_done}"
    # 样本量 sanity: 跳过率应在合理区间(防测试退化成全 True)
    assert n_skipped > n_checked * 0.3, f"跳过率异常低 {n_skipped}/{n_checked}, 预筛可能失效"


def test_forced_true_control_byte_identical(monkeypatch):
    """预筛强制恒 True(全评估) 与正常版整 loop 字节一致 → 预筛不改变任何结果。"""
    price, high, low, open_, entry = make_synthetic(seed=99)
    eq1, tr1 = _simulate_core_v3(price, entry, *_args(price, entry))
    monkeypatch.setattr(TriggerPreFilter, "could_trigger", lambda self, **kw: True)
    eq2, tr2 = _simulate_core_v3(price, entry, *_args(price, entry))
    assert np.array_equal(eq1, eq2) and np.array_equal(tr1, tr2)


def _args(price, entry):
    kw = BASE_PARAMS
    return (kw["initial_capital"], kw["commission"],
            kw["min_buy_amount"], kw["max_buy_amount"], kw["lot_size"], kw["min_lots"],
            True, kw["cost_stop_threshold"], True, kw["trailing_activation"], kw["trailing_drawdown"],
            True, kw["ladder_profits"], kw["ladder_ratios"], kw["n_ladder"],
            True, kw["max_hold_days"], False, kw["cond_time_days"], kw["cond_time_profit"],
            False, kw["first_day_target"], 1, None, None, 1, kw["slippage"], kw["stamp_tax"],
            None, None, None, None, 1.0, 1, False, False, 1.0)


def test_swap_pop_same_bar_multi_sell():
    """swap-pop 专项: 3 持仓同 bar 两个触发, 重排后第三持仓判定不错位。"""
    n, k = 6, 3
    price = np.array([
        [10.0, 10.0, 10.0],
        [10.0, 10.0, 10.0],
        [8.0, 12.0, 10.5],   # bar2: s0 破止损(-20%), s1 冲高
        [8.0, 10.0, 10.5],   # bar3: s1 回撤触及 trailing 线
        [8.0, 10.0, 10.5],
        [8.0, 10.0, 10.5],
    ])
    high = price * 1.01
    low = price * 0.99
    low[2, 0] = 7.9
    op = price.copy()
    entry = np.zeros((n, k), dtype=bool)
    entry[0, 0] = True
    entry[0, 1] = True
    entry[0, 2] = True
    eq_old, tr_old, eq_new, tr_new = run_both(price, high, low, op, entry,
                                              trailing_first=True)
    assert_parity(eq_old, tr_old, eq_new, tr_new, "swap-pop same-bar multi-sell")
    assert tr_new.shape[0] >= 2, "测试数据未造出多触发场景, 断言无意义"
