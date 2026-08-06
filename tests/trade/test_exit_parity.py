"""parity 测试 (铁律 4, 2026-07-26 裁决①扩展): monitor 全规则 + priority
调度 vs 回测 ExitDispatcher/策略对象, 同输入同决策。

对齐口径 (两侧语义的真实差异, 不假装不存在):
1. **bar low/close ≡ tick last, hi_pp ≡ 当日行情 high 涨幅**: 回测按 bar
   判, 实盘按 tick 判; 合成数据令 bar.low = tick last、bar.high = quote high。
2. **激活线**: 两侧同款 (activation, 裁决①已给实盘补齐) —— 峰值涨幅
   (peak-cost)/cost ≥ activation 才武装 trailing, 有专例锁死。
3. **trailing_first 双触发**: 回测可返回 [ladder 部分卖, trailing 全卖];
   实盘部分卖由 executor 预埋单承担, monitor 只对未预埋档兜底、首触发
   即返回。parity 锁"首个触发策略名" ([0].strategy_name), 双触发属执行
   层分工差异, 不在此锁。
4. **档位状态**: pos.ladder_done bitmask ≡ book 当日 tier 标记
   (已置位档 = 已预埋档, 两侧都不再触发)。
5. **first_day**: 回测在次日最后一根 bar 判定, 实盘取"当日"粒度;
   合成用 bpday=1 (当日=最后一根 bar) 对齐。hold_days ≡ current_day-entry_day。
6. 执行价不在 parity 范围 (回测线价/bar价 vs 实盘撤单流水线买一价)。
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backtest.loop.exit_engine import ExitDispatcher, Priority
from backtest.loop.state import Bar, Context, Position
from backtest.loop.strategies.cond_time import CondTimeStrategy
from backtest.loop.strategies.cost_stop import CostStopStrategy
from backtest.loop.strategies.first_day import FirstDayStrategy
from backtest.loop.strategies.ladder_tp import LadderTpStrategy
from backtest.loop.strategies.time_stop import TimeStopStrategy
from backtest.loop.strategies.trailing import TrailingStrategy
from trade.book import Book
from trade.config import (
    CondTimeStopConfig,
    FirstDayConfig,
    StopConfig,
    TradeConfig,
)
from trade.monitor import Monitor
from trade.store import TradeStore


@pytest.fixture()
def store(tmp_path):
    s = TradeStore(tmp_path / "t.db", tmp_path / "r.jsonl")
    yield s
    s.close()


# 同一份配置构造两侧 (铁律 4: 参数只有一份事实源)
CFG = TradeConfig(account_id="PARITY")
# cond_time/first_day 默认关, parity 需要开启版
CFG_ALL = TradeConfig(
    account_id="PARITY",
    stop=StopConfig(
        cond_time_stop=CondTimeStopConfig(enabled=True, days=5, profit=0.03),
        first_day=FirstDayConfig(enabled=True, target=0.05),
    ),
)


def _monitor(store, cfg, hold_days, book=None):
    return Monitor(None, book or Book(), None, store, cfg,
                   hold_days=lambda code: hold_days, clock=lambda: 1000.0)


def _bt(cfg):
    """回测侧策略对象, 与 monitor 同一份 cfg 构造。"""
    s = cfg.stop
    return {
        "cost_stop": CostStopStrategy(threshold=s.cost_stop.threshold),
        "trailing": TrailingStrategy(activation=s.trailing_stop.activation,
                                     drawdown=s.trailing_stop.drawdown),
        "ladder_tp": LadderTpStrategy(),
        "time_stop": TimeStopStrategy(max_hold_days=s.time_stop.max_hold_days),
        "cond_time": CondTimeStrategy(days=s.cond_time_stop.days,
                                      profit=s.cond_time_stop.profit),
        "first_day": FirstDayStrategy(target=s.first_day.target),
    }


def _ctx(cost, last, high, days, cfg, bar_index=None, entry_idx=0,
         ladder_done=0):
    """合成回测侧 pos/bar/ctx: bar.low ≡ tick last, bar.high ≡ quote high。"""
    peak = max(cost, high)
    pos = Position(code=np.int32(0), shares=np.float64(100.0),
                   entry_px=np.float64(cost), entry_idx=np.int32(entry_idx),
                   high_px=np.float64(peak), high_hi=np.float64(high),
                   ladder_done=np.int32(ladder_done))
    bar = Bar(close=last, high=high, low=last, open=last)
    s = cfg.stop
    profits = np.array([p for p, _ in s.ladder_tp.levels])
    ratios = np.array([r for _, r in s.ladder_tp.levels])
    ctx = Context(
        bar_index=bar_index if bar_index is not None else days,
        ci=0, bpday=1, hold_days=days,
        entry_px=cost, pp=(last - cost) / cost, hp_profit=(high - cost) / cost,
        peak_hi=peak, peak_hi_profit=(peak - cost) / cost,
        pos_high_px=peak, pos_high_hi=high,
        hi_pp=(high - cost) / cost, lo_pp=(last - cost) / cost,
        ladder_profits=profits, ladder_ratios=ratios,
        n_ladder=len(profits),
    )
    return pos, bar, ctx


def _monitor(store, cfg, hold_days, book=None):
    return Monitor(None, book or Book(), None, store, cfg,
                   hold_days=lambda code: hold_days, clock=lambda: 1000.0)


def _m_trigger(store, cfg, code, cost, last, high, days, book=None,
               mark_all_tiers=True):
    """monitor 首触发规则名。mark_all_tiers=True (默认): 预标记全部档位
    —— 反映实盘正常分工 "ladder 已由预埋单覆盖", monitor 评估其余腿;
    ladder 专项用例传 False (测未预埋档的兜底触发)。"""
    if book is None:
        book = Book()
    if mark_all_tiers:
        today = __import__("time").strftime(
            "%Y%m%d", __import__("time").localtime(1000.0))
        for i in range(len(cfg.stop.ladder_tp.levels)):
            book.mark_tier(code, i, today)
    mon = _monitor(store, cfg, days, book)
    result = mon._evaluate(code, cost, {"last": last, "high": high,
                                        "bid1": last})
    reason = result[0] if result else None
    return reason.split(":")[0] if reason else None


# ═══════════════════════════════════════════════════════════════
# 1. 成本止损 (threshold 负值口径; 公式不同形, 边界用二进制友好数)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("last,expected", [
    (88.01, False), (88.00, True), (87.99, True), (100.0, False),
])
def test_parity_cost_stop(store, last, expected):
    m = _m_trigger(store, CFG, "X", 100.0, last, last, 0)
    pos, bar, ctx = _ctx(100.0, last, last, 0, CFG)
    bt = len(_bt(CFG)["cost_stop"].check(pos, bar, ctx)) > 0
    assert (m == "cost_stop") == bt == expected


# ═══════════════════════════════════════════════════════════════
# 2. 移动止盈 (activation 激活线 + 峰值回撤, 两个子条件都锁)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("last,high,expected", [
    (118.81, 120.0, False),
    (118.80, 120.0, True),    # 边界: 峰值 120×(1-0.01)=118.8
    (118.79, 120.0, True),
    (100.90, 102.0, False),   # 跌破线但峰值涨幅 2% < 激活线 3.5% → 不武装
])
def test_parity_trailing(store, last, high, expected):
    m = _m_trigger(store, CFG, "X", 100.0, last, high, 0)
    pos, bar, ctx = _ctx(100.0, last, high, 0, CFG)
    bt = len(_bt(CFG)["trailing"].check(pos, bar, ctx)) > 0
    assert (m == "trailing") == bt == expected


# ═══════════════════════════════════════════════════════════════
# 3. 阶梯止盈 (High 涨破新档位; 已置位档 ≡ 已预埋档不再触发)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("high,expected", [
    (105.99, False),
    (106.00, True),   # 档 0 (6%)
    (115.00, True),   # 档 0+1 同破
])
def test_parity_ladder_trigger(store, high, expected):
    m = _m_trigger(store, CFG, "X", 100.0, high, high, 0, mark_all_tiers=False)
    pos, bar, ctx = _ctx(100.0, high, high, 0, CFG)
    bt = len(_bt(CFG)["ladder_tp"].check(pos, bar, ctx)) > 0
    assert (m == "ladder_tp") == bt == expected


def test_parity_ladder_done_tier_skipped(store):
    """档 0 已置位 (≡ 已预埋): high 破档 0 不破档 1 → 两侧都不触发。"""
    book = Book()
    today = __import__("time").strftime("%Y%m%d", __import__("time").localtime(1000.0))
    book.mark_tier("X", 0, today)
    m = _m_trigger(store, CFG, "X", 100.0, 106.0, 106.0, 0, book=book,
                   mark_all_tiers=False)
    pos, bar, ctx = _ctx(100.0, 106.0, 106.0, 0, CFG, ladder_done=0b01)
    bt = len(_bt(CFG)["ladder_tp"].check(pos, bar, ctx)) > 0
    assert (m == "ladder_tp") == bt == False


# ═══════════════════════════════════════════════════════════════
# 4. 时间止损 (到点即走, 无收益门槛)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("days,expected", [(19, False), (20, True), (25, True)])
def test_parity_time_stop(store, days, expected):
    m = _m_trigger(store, CFG, "X", 100.0, 105.0, 105.0, days)
    pos, bar, ctx = _ctx(100.0, 105.0, 105.0, days, CFG)
    bt = len(_bt(CFG)["time_stop"].check(pos, bar, ctx)) > 0
    assert (m == "time_stop") == bt == expected


# ═══════════════════════════════════════════════════════════════
# 5. 条件时间止盈 (天数 + 当日最高涨幅双条件)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("days,high,expected", [
    (5, 103.5, True),    # ≥5 天且涨幅 3.5% ≥ 3%
    (5, 102.9, False),   # 涨幅不够
    (4, 105.0, False),   # 天数不够
])
def test_parity_cond_time(store, days, high, expected):
    # last=high 保证不碰 trailing 回撤线 (peak=high, last>线)
    m = _m_trigger(store, CFG_ALL, "X", 100.0, high, high, days)
    pos, bar, ctx = _ctx(100.0, high, high, days, CFG_ALL)
    bt = len(_bt(CFG_ALL)["cond_time"].check(pos, bar, ctx)) > 0
    assert (m == "cond_time") == bt == expected


# ═══════════════════════════════════════════════════════════════
# 6. 首日规则 (次日评估, 日内最高涨幅 < target 即卖)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("days,high,expected", [
    (1, 104.0, True),    # 首日, 涨 4% < 5% → 卖
    (1, 106.0, False),   # 首日达标
    (2, 104.0, False),   # 非首日
])
def test_parity_first_day(store, days, high, expected):
    m = _m_trigger(store, CFG_ALL, "X", 100.0, high, high, days)
    # 回测: entry_day=0, current_day=days, bpday=1 (当日=最后一根 bar)
    pos, bar, ctx = _ctx(100.0, high, high, days, CFG_ALL,
                         bar_index=days, entry_idx=0)
    bt = len(_bt(CFG_ALL)["first_day"].check(pos, bar, ctx)) > 0
    assert (m == "first_day") == bt == expected


# ═══════════════════════════════════════════════════════════════
# 7. priority 调度顺序: 首触发策略名 ≡ ExitDispatcher [0].strategy_name
# ═══════════════════════════════════════════════════════════════

def _dispatcher_first(cfg, priority, pos, bar, ctx):
    strategies = _bt(cfg)
    # 未启用的策略不进 dict (capability gating, exit_engine.py:9)
    s = cfg.stop
    enabled = {}
    if s.cost_stop.enabled:
        enabled["cost_stop"] = strategies["cost_stop"]
    if s.trailing_stop.enabled:
        enabled["trailing"] = strategies["trailing"]
    if s.ladder_tp.enabled:
        enabled["ladder_tp"] = strategies["ladder_tp"]
    if s.time_stop.enabled:
        enabled["time_stop"] = strategies["time_stop"]
    if s.cond_time_stop.enabled:
        enabled["cond_time"] = strategies["cond_time"]
    if s.first_day.enabled:
        enabled["first_day"] = strategies["first_day"]
    d = ExitDispatcher(enabled, Priority(priority))
    results = d.evaluate(pos, bar, ctx)
    return results[0].strategy_name if results else None


@pytest.mark.parametrize("priority", ["stop_first", "ladder_tp_first",
                                      "trailing_first"])
def test_parity_priority_cost_vs_ladder(store, priority):
    """cost 与 ladder 同时可触发 (last 87 破止损, high 107 破档 0):
    三档 priority 下首触发必须一致。"""
    cfg = TradeConfig(account_id="P", stop=StopConfig(priority=priority))
    m = _m_trigger(store, cfg, "X", 100.0, 87.0, 107.0, 0,
                   mark_all_tiers=False)
    pos, bar, ctx = _ctx(100.0, 87.0, 107.0, 0, cfg)
    bt = _dispatcher_first(cfg, priority, pos, bar, ctx)
    assert m == bt


@pytest.mark.parametrize("priority", ["stop_first", "ladder_tp_first",
                                      "trailing_first"])
def test_parity_priority_trailing_vs_cost(store, priority):
    """peak 105 (激活线过) last 103.9 (破回撤线): 仅 trailing 触发,
    三档 priority 下首触发都是 trailing (cost 不触发 103.9>88,
    ladder 不破档 105<106) —— 调度顺序不同但结论一致的情形。"""
    cfg = TradeConfig(account_id="P", stop=StopConfig(priority=priority))
    m = _m_trigger(store, cfg, "X", 100.0, 103.9, 105.0, 0)
    pos, bar, ctx = _ctx(100.0, 103.9, 105.0, 0, cfg)
    bt = _dispatcher_first(cfg, priority, pos, bar, ctx)
    assert m == bt == "trailing"


def test_parity_priority_tail_order(store):
    """time_stop 与 cond_time 同时满足 → 尾部固定 time 先
    [exit_engine.py:53]。"""
    cfg = CFG_ALL  # priority trailing_first, cond_time days=5 enabled
    # days 25 ≥ 20 (time) 且 ≥ 5 + 涨幅 6% ≥ 3% (cond); high 不破 ladder 档?
    # high 106 恰破档 0 — 避开, 用 105.9; trailing: peak 105.9 涨幅 5.9%
    # ≥ 3.5% 激活, dd 线 104.8, last 105.9 > 线不触发; cost 不触发
    m = _m_trigger(store, cfg, "X", 100.0, 105.9, 105.9, 25)
    pos, bar, ctx = _ctx(100.0, 105.9, 105.9, 25, cfg)
    bt = _dispatcher_first(cfg, "trailing_first", pos, bar, ctx)
    assert m == bt == "time_stop"
