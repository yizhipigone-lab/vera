"""trade/pool_money.py 纯函数单测 (治理III W2-1, 2026-09-05).

行为锁来源: 8-21 风控误拒现场 (tests/trade/test_rotation.py 的 daily_loss
用例) —— 本文件是口径下沉后的纯函数层, 原集成测试仍全量保留。
"""
import pytest

from trade.book import DIRECTION_SELL
from trade.pool_money import (
    in_flight_sell_returns,
    pool_split,
    position_value,
    stock_budget_cap,
    stock_pool_value,
)

DAY_START = 1_700_000_000.0   # 任意"当日 0 点"时间戳, 测试只关心相对比较


class _Pos:
    def __init__(self, volume=0, avg_cost=0.0):
        self.volume = volume
        self.avg_cost = avg_cost


class _RotCfg:
    def __init__(self, enabled=True, etf_ratio=0.5, cyb="159949.SZ",
                 risk2="513100.SH", gold="518880.SH", hedge2="511010.SH"):
        self.enabled = enabled
        self.etf_ratio = etf_ratio
        self.cyb_etf = cyb
        self.risk_etf2 = risk2
        self.gold_etf = gold
        self.hedge_etf2 = hedge2


def _quotes(map_):
    return lambda code: ({"last": map_[code]} if code in map_ else None)


# ── pool_split ────────────────────────────────────────────────────

def test_pool_split_half():
    etf, stock = pool_split(0.5)
    assert (etf, stock) == (0.5, 0.5)


def test_pool_split_seven_three():
    etf, stock = pool_split(0.7)
    assert etf == pytest.approx(0.7)
    assert stock == pytest.approx(0.3)


# ── stock_pool_value / position_value ─────────────────────────────

def test_stock_pool_value_excludes_all_rotation_etfs():
    cfg = _RotCfg()
    positions = {
        "159949.SZ": _Pos(1000, 1.0),   # 轮动 cyb
        "513100.SH": _Pos(1000, 1.0),   # 轮动 risk2
        "518880.SH": _Pos(1000, 2.0),   # 轮动 gold
        "511010.SH": _Pos(1000, 2.0),   # 轮动 hedge2
        "000001.SZ": _Pos(1000, 10.0),  # 股票池
    }
    q = _quotes({"159949.SZ": 1.0, "513100.SH": 1.0, "518880.SH": 2.0,
                 "511010.SH": 2.0, "000001.SZ": 10.0})
    assert stock_pool_value(cfg, positions, q) == pytest.approx(10_000.0, abs=1.0)


def test_stock_pool_value_falls_back_to_cost_no_quote():
    cfg = _RotCfg()
    positions = {"000001.SZ": _Pos(1000, 10.0), "159949.SZ": _Pos(1000, 1.0)}
    assert stock_pool_value(cfg, positions, _quotes({})) == pytest.approx(10_000.0)


def test_position_value_prefers_quote_else_cost():
    positions = {"000001.SZ": _Pos(100, 10.0)}
    # 有行情 → 最新价 × 量
    assert position_value("000001.SZ", 100, _quotes({"000001.SZ": 12.0}),
                          positions) == 1200.0
    # 无行情 → 成本价 × 量
    assert position_value("000001.SZ", 100, _quotes({}), positions) == 1000.0
    # 量 ≤ 0 → 0
    assert position_value("000001.SZ", 0, _quotes({}), positions) == 0.0
    # 无持仓且无行情 → 0
    assert position_value("999999.SZ", 100, _quotes({}), {}) == 0.0


# ── stock_budget_cap ──────────────────────────────────────────────

def test_stock_budget_cap_formula():
    # S_target = (1-0.5)×1M = 500k; 股票市值 10k → 预算 490k
    assert stock_budget_cap(_RotCfg(), 1_000_000.0, 10_000.0) == \
        pytest.approx(490_000.0, abs=1.0)


def test_stock_budget_cap_disabled_returns_none():
    cfg = _RotCfg(enabled=False)
    assert stock_budget_cap(cfg, 1_000_000.0, 0.0) is None


def test_stock_budget_cap_negative_total_none():
    assert stock_budget_cap(_RotCfg(), 0.0, 0.0) is None
    assert stock_budget_cap(_RotCfg(), -1.0, 0.0) is None


def test_stock_budget_cap_floor_zero():
    # 股票市值超过 S_target → 预算为 0 不出现负值
    assert stock_budget_cap(_RotCfg(), 100_000.0, 90_000.0) == 0.0


# ── in_flight_sell_returns (8-21 现场场景, 纯函数层) ──────────────

def _sell_trade(tid, amount, ts=DAY_START + 1):
    return {"traded_id": tid, "amount": amount, "ts": ts,
            "direction": DIRECTION_SELL}


def _sell_order(oid, price, filled, ts=DAY_START + 1, direction=DIRECTION_SELL):
    return {"order_id": oid, "price": price, "filled_qty": filled,
            "ts": ts, "direction": direction}


def test_in_flight_trades_empty_orders_ahead_uses_orders():
    """8-21 现场: trades 尚未回报, orders 已见终态 → 用 orders 兜住。
    max(0, orders) = orders 值, 不因 trades 空而误判 0。"""
    orders = [_sell_order("O1", 1.676, 274700), _sell_order("O2", 9.384, 5600)]
    expect = 274700 * 1.676 + 5600 * 9.384
    assert in_flight_sell_returns([], orders, DAY_START) == \
        pytest.approx(expect, abs=1.0)


def test_in_flight_both_present_no_double_count():
    """两路回报都齐 (金额一致) → max 取同值, 不重复计。"""
    trades = [_sell_trade("T1", 460397.2), _sell_trade("T2", 52550.4)]
    orders = [_sell_order("O1", 1.676, 274700), _sell_order("O2", 9.384, 5600)]
    assert in_flight_sell_returns(trades, orders, DAY_START) == \
        pytest.approx(460397.2 + 52550.4, abs=1.0)


def test_in_flight_ignores_before_day_start_and_buys():
    orders = [_sell_order("O1", 2.0, 100, ts=DAY_START - 5),   # 昨日
              _sell_order("O2", 2.0, 100, ts=DAY_START + 5,
                          direction=23)]                       # 买入
    assert in_flight_sell_returns([], orders, DAY_START) == 0.0


def test_in_flight_orders_exceeds_trades_uses_orders():
    """orders 领先 (金额更大) → 取 orders 防低估 (宁可偏松)。"""
    trades = [_sell_trade("T1", 1000.0)]
    orders = [_sell_order("O1", 2.0, 1000)]  # 2000 > 1000
    assert in_flight_sell_returns(trades, orders, DAY_START) == 2000.0
