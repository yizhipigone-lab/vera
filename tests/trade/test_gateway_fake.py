"""FakeGateway 单元测试 (P1 地基, 2026-07-26).

锁住: 下单/撤单/查询往返、模拟成交推进持仓与资金并触发回调、
废单/断线钩子、行情推送。Fake 与真网关同接口同常量, 这里的行为
就是真网关集成的契约样板。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.book import (
    DIRECTION_BUY,
    DIRECTION_SELL,
    OS_CANCELED,
    OS_JUNK,
    OS_PART_SUCC,
    OS_REPORTED,
    OS_SUCCEEDED,
)
from trade.gateway import FakeGateway


@pytest.fixture()
def events():
    """收集三路回调。"""
    return {"orders": [], "trades": [], "quotes": [], "disconnects": []}


@pytest.fixture()
def gw(events):
    g = FakeGateway(
        cash=1_000_000.0,
        positions={"600519.SH": {"volume": 200, "can_use": 200, "avg_cost": 1700.0}},
        on_order=events["orders"].append,
        on_trade=events["trades"].append,
        on_quote=lambda code, q: events["quotes"].append((code, q)),
        on_disconnected=events["disconnects"].append,
    )
    g.connect()
    return g


def test_order_returns_id_and_fires_ack(gw, events):
    """下单即已报 ack, 回报内容与查询结果一致。"""
    oid = gw.order("000001.SZ", DIRECTION_BUY, 10.0, 100, remark="V0726-1A")
    assert oid
    assert events["orders"][-1]["status"] == OS_REPORTED
    rec = gw.query_orders()[-1]
    assert rec["order_id"] == oid and rec["remark"] == "V0726-1A"


def test_order_requires_connection(events):
    g = FakeGateway(on_order=events["orders"].append)
    with pytest.raises(RuntimeError, match="未连接"):
        g.order("000001.SZ", DIRECTION_BUY, 10.0, 100)


def test_cancel_roundtrip(gw, events):
    """撤单: 状态已撤 + 回报; 已终态的单再撤返回 False。"""
    oid = gw.order("000001.SZ", DIRECTION_BUY, 10.0, 100)
    assert gw.cancel(oid)
    assert events["orders"][-1]["status"] == OS_CANCELED
    assert not gw.cancel(oid)  # 已撤 = 终态, 再撤失败


def test_simulate_fill_full(gw, events):
    """全额成交: 订单已成、持仓/资金推进、成交回报触发。"""
    cash_before = gw.query_asset()["cash"]
    oid = gw.order("000001.SZ", DIRECTION_BUY, 10.0, 100)
    trade = gw.simulate_fill(oid)
    rec = gw.query_orders()[-1]
    assert rec["status"] == OS_SUCCEEDED and rec["filled_qty"] == 100
    assert gw.query_asset()["cash"] == cash_before - 1000.0
    pos = {p["code"]: p for p in gw.query_positions()}["000001.SZ"]
    assert pos["volume"] == 100 and pos["avg_cost"] == 10.0
    assert events["trades"][-1]["traded_id"] == trade["traded_id"]


def test_simulate_partial_fill(gw):
    """部分成交: 部成状态, 已成交量累计。"""
    oid = gw.order("000001.SZ", DIRECTION_BUY, 10.0, 200)
    gw.simulate_fill(oid, qty=100)
    rec = gw.query_orders()[-1]
    assert rec["status"] == OS_PART_SUCC and rec["filled_qty"] == 100


def test_sell_fill_uses_existing_position(gw):
    """卖出成交扣持仓与可用, 回款。"""
    cash_before = gw.query_asset()["cash"]
    oid = gw.order("600519.SH", DIRECTION_SELL, 1800.0, 100)
    gw.simulate_fill(oid)
    pos = {p["code"]: p for p in gw.query_positions()}["600519.SH"]
    assert pos["volume"] == 100 and pos["can_use"] == 100
    assert gw.query_asset()["cash"] == cash_before + 1800.0 * 100


def test_buy_fill_not_sellable_same_day(gw):
    """T+1: 当日买入不进 can_use, 与真实券商口径一致。"""
    oid = gw.order("000001.SZ", DIRECTION_BUY, 10.0, 100)
    gw.simulate_fill(oid)
    pos = {p["code"]: p for p in gw.query_positions()}["000001.SZ"]
    assert pos["volume"] == 100 and pos["can_use"] == 0


def test_simulate_reject(gw, events):
    oid = gw.order("000001.SZ", DIRECTION_BUY, 10.0, 100)
    gw.simulate_reject(oid)
    assert events["orders"][-1]["status"] == OS_JUNK


def test_simulate_disconnect_fires_hook(gw, events):
    """断线钩子: 回调收到原因, 网关进入未连接态。"""
    gw.simulate_disconnect("模拟断线")
    assert events["disconnects"] == ["模拟断线"]
    with pytest.raises(RuntimeError):
        gw.order("000001.SZ", DIRECTION_BUY, 10.0, 100)


def test_push_quote(gw, events):
    gw.subscribe_quotes(["600519.SH"])
    gw.push_quote("600519.SH", {"last": 1701.0})
    assert events["quotes"] == [("600519.SH", {"last": 1701.0})]
    gw.unsubscribe_all()


# ═══════════════════════════════════════════════════════════════
# 审计L7修复: 资金冻结建模
# ═══════════════════════════════════════════════════════════════

def test_buy_order_freezes_cash(events):
    """买单下单即冻结 (真实券商口径): 可用转冻结, 总额不变。"""
    g = FakeGateway(cash=100_000.0, on_order=events["orders"].append)
    g.connect()
    oid = g.order("000001.SZ", DIRECTION_BUY, 10.0, 100)
    asset = g.query_asset()
    assert asset["frozen_cash"] == 1000.0
    assert asset["cash"] == 100_000.0 - 1000.0   # 可用减少
    assert asset["total_asset"] == 100_000.0     # 总额不变 (冻结仍是我的钱)


def test_fill_converts_frozen_to_charge(events):
    """成交: 冻结释放 + 按成交额实扣。"""
    g = FakeGateway(cash=100_000.0, on_order=events["orders"].append)
    g.connect()
    oid = g.order("000001.SZ", DIRECTION_BUY, 10.0, 100)
    g.simulate_fill(oid)
    asset = g.query_asset()
    assert asset["frozen_cash"] == 0.0
    assert asset["cash"] == 100_000.0 - 1000.0


def test_cancel_releases_frozen(events):
    """撤单: 未成交部分冻结释放。"""
    g = FakeGateway(cash=100_000.0, on_order=events["orders"].append)
    g.connect()
    oid = g.order("000001.SZ", DIRECTION_BUY, 10.0, 200)
    g.simulate_fill(oid, qty=100)           # 部成 100 (冻结剩 1000)
    assert g.query_asset()["frozen_cash"] == 1000.0
    g.cancel(oid)                           # 撤剩余 100
    assert g.query_asset()["frozen_cash"] == 0.0


def test_simulate_fill_rejects_terminal_order(events):
    """终态单不可成交 (真机不可能, Fake 放行是帮上层掩盖状态机洞)。"""
    g = FakeGateway(on_order=events["orders"].append)
    g.connect()
    oid = g.order("000001.SZ", DIRECTION_BUY, 10.0, 100)
    g.simulate_reject(oid)
    with pytest.raises(ValueError, match="终态单"):
        g.simulate_fill(oid)


def test_real_gateway_query_asset_recomputes_total():
    """2026-08-18 (159949 事件): QMT 的 total_asset 字段对"昨日尾盘新买入"持仓
    有 T+1 结算延迟 (total_asset 漏算, market_value 正确)。真网关应重算
    total_asset = 可用 + 冻结 + 市值, 不信任 QMT 的 total_asset 字段。"""
    from trade.gateway import RealGateway

    class _Asset:
        cash = 1000.0
        frozen_cash = 500.0
        market_value = 50_000.0
        total_asset = 30_000.0    # QMT 漏算 (错值, 模拟 159949 的 T+1 延迟)

    class _Trader:
        def query_stock_asset(self, account):
            return _Asset()

    gw = RealGateway(account_id="test")
    gw._trader = _Trader()
    gw._account = "test"
    a = gw.query_asset()
    assert a["total_asset"] == 51_500.0      # 重算 = 1000 + 500 + 50000
    assert a["cash"] == 1000.0
    assert a["frozen_cash"] == 500.0
    assert a["market_value"] == 50_000.0     # 市值原样透传
