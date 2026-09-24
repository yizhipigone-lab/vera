"""ThsGuiGateway 转换与接线接缝测试 (2026-09-06)。

定位与 test_gateway_real.py 相同: 给不可信外部库 (easytrader GUI 操控)
做接缝测试, 断言的是"我们的转换与接线逻辑" —— 代码市场后缀互转、
委托状态中文映射、资产口径重算、armed 保险栓、市价类型映射、
合同编号/本地占位号分支。不断言 easytrader 本身的行为。
无 GUI 环境照跑 (替身经实例属性注入, 不 import easytrader)。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.book import (  # noqa: E402
    DIRECTION_BUY,
    DIRECTION_SELL,
    OS_CANCELED,
    OS_JUNK,
    OS_PARTSUCC_CANCEL,
    OS_PART_SUCC,
    OS_REPORTED,
    OS_REPORTED_CANCEL,
    OS_SUCCEEDED,
    PRICE_TYPE_MARKET_PEER_FIRST,
    PRICE_TYPE_SZ_5LEVEL_CANCEL,
)
from trade.gateway_ths import ThsGuiGateway  # noqa: E402


class FakeThsClient:
    """easytrader ClientTrader 的测试替身: 记录下单调用, 回放查询数据。"""

    def __init__(self):
        self.calls: list[tuple] = []
        self.balance_data = {"资金余额": 120000.0, "可用金额": 100000.0,
                             "股票市值": 80000.0, "总资产": 200000.0}
        self.position_data = [
            {"证券代码": "159949", "股票余额": 1000, "可用余额": 800,
             "成本价": 1.234},
            {"证券代码": "600519", "股票余额": 100, "可用余额": 100,
             "成本价": 1500.0},
            {"证券代码": "000001", "股票余额": 0, "可用余额": 0,
             "成本价": 10.0},
        ]
        self.entrusts_data = []
        self.trades_data = []
        self.buy_result = {"entrust_no": "HT12345"}
        self.sell_result = {"entrust_no": "HT12346"}
        self.market_result = {"entrust_no": "HT12347"}
        self.cancel_result = {"message": "success"}

    @property
    def balance(self):
        return self.balance_data

    @property
    def position(self):
        return self.position_data

    @property
    def today_entrusts(self):
        return self.entrusts_data

    @property
    def today_trades(self):
        return self.trades_data

    def buy(self, security, price, amount, **kwargs):
        self.calls.append(("buy", security, price, amount))
        return self.buy_result

    def sell(self, security, price, amount, **kwargs):
        self.calls.append(("sell", security, price, amount))
        return self.sell_result

    def market_buy(self, security, amount, ttype=None, limit_price=None):
        self.calls.append(("market_buy", security, amount, ttype, limit_price))
        return self.market_result

    def market_sell(self, security, amount, ttype=None, limit_price=None):
        self.calls.append(("market_sell", security, amount, ttype, limit_price))
        return self.market_result

    def cancel_entrust(self, entrust_no):
        self.calls.append(("cancel", entrust_no))
        return self.cancel_result


@pytest.fixture()
def ths():
    return FakeThsClient()


@pytest.fixture()
def gw(ths):
    g = ThsGuiGateway(armed=True, clock=lambda: 1_800_000_000.0)
    g._trader = ths          # 接缝: 注入替身 (跳过 connect 的 GUI attach)
    return g


# ── armed 保险栓 ─────────────────────────────────────────────

def test_order_requires_armed(ths):
    """armed=False (默认) 下单直接 raise —— GUI 下单不可撤回,
    保险栓没打开时一股都不许出去。"""
    g = ThsGuiGateway()      # 默认 armed=False
    g._trader = ths
    with pytest.raises(RuntimeError, match="未武装"):
        g.order("000001.SZ", DIRECTION_BUY, 10.0, 100)
    assert ths.calls == []


def test_order_requires_connection():
    g = ThsGuiGateway(armed=True)
    with pytest.raises(RuntimeError, match="未连接"):
        g.order("000001.SZ", DIRECTION_BUY, 10.0, 100)


# ── 下单映射 ─────────────────────────────────────────────────

def test_limit_buy_maps_code_and_returns_entrust_no(gw, ths):
    oid = gw.order("159949.SZ", DIRECTION_BUY, 1.23, 1000, remark="V0906-1B")
    assert ths.calls == [("buy", "159949", 1.23, 1000)]   # 后缀剥掉
    assert oid == "HT12345"


def test_limit_sell_maps(gw, ths):
    oid = gw.order("600519.SH", DIRECTION_SELL, 1500.0, 100)
    assert ths.calls == [("sell", "600519", 1500.0, 100)]
    assert oid == "HT12346"


def test_market_peer_first_maps_ttype(gw, ths):
    """对手最优逃生通道 → 市价单 ttype='对手方最优价格',
    限价仍带 (科创板需要)。"""
    gw.order("300750.SZ", DIRECTION_BUY, 200.0, 100,
             price_type=PRICE_TYPE_MARKET_PEER_FIRST)
    assert ths.calls == [
        ("market_buy", "300750", 100, "对手方最优价格", 200.0)]


def test_sz_5level_cancel_maps_ttype(gw, ths):
    gw.order("159915.SZ", DIRECTION_SELL, 2.0, 1000,
             price_type=PRICE_TYPE_SZ_5LEVEL_CANCEL)
    assert ths.calls == [
        ("market_sell", "159915", 1000, "最优五档即时成交剩余撤销", 2.0)]


def test_order_without_entrust_no_falls_back_local_id(gw, ths):
    """弹窗没返回合同编号 → 发本地占位号并记 warning (对账靠认领)。"""
    ths.buy_result = {"message": "success"}
    oid = gw.order("000001.SZ", DIRECTION_BUY, 10.0, 100)
    assert oid.startswith("THS") and oid.endswith("001")


# ── 撤单 ─────────────────────────────────────────────────────

def test_cancel_success(gw, ths):
    assert gw.cancel("HT12345") is True
    assert ths.calls == [("cancel", "HT12345")]


def test_cancel_refused_returns_false(gw, ths):
    ths.cancel_result = {"message": "委托单状态错误不能撤单, 该委托单可能已经成交或者已撤"}
    assert gw.cancel("HT12345") is False


# ── 查询口径 ─────────────────────────────────────────────────

def test_query_asset_recomputes_total(gw):
    """total_asset 不信客户端汇总字段, 按 可用+冻结+市值 重算
    (冻结 = 资金余额 − 可用金额)。"""
    a = gw.query_asset()
    assert a["cash"] == 100000.0
    assert a["frozen_cash"] == 20000.0
    assert a["market_value"] == 80000.0
    assert a["total_asset"] == 200000.0


def test_query_positions_maps_and_suffixes(gw):
    ps = gw.query_positions()
    assert len(ps) == 2                      # 零持仓行被过滤
    assert ps[0] == {"code": "159949.SZ", "volume": 1000,
                     "can_use": 800, "avg_cost": 1.234}
    assert ps[1]["code"] == "600519.SH"      # 6 头 → 沪市


def test_query_orders_maps_status_and_fields(gw, ths):
    ths.entrusts_data = [
        {"委托时间": "09:30:01", "证券代码": "000001", "操作": "买入",
         "委托价格": 10.0, "委托数量": 1000, "成交数量": 400,
         "合同编号": "HT001", "状态说明": "部成"},
        {"委托时间": "09:31:02", "证券代码": "600519", "操作": "卖出",
         "委托价格": 1500.0, "委托数量": 100, "成交数量": 100,
         "合同编号": "HT002", "状态说明": "全部成交"},
    ]
    os_ = gw.query_orders()
    assert os_[0]["order_id"] == "HT001"
    assert os_[0]["direction"] == DIRECTION_BUY
    assert os_[0]["status"] == OS_PART_SUCC
    assert os_[0]["filled_qty"] == 400
    assert os_[0]["remark"] == ""            # THS 无 remark, 不伪装
    assert os_[1]["status"] == OS_SUCCEEDED
    assert os_[1]["direction"] == DIRECTION_SELL
    assert os_[1]["code"] == "600519.SH"
    assert os_[0]["ts"] is not None          # 时分秒补当日日期


def test_query_trades_maps(gw, ths):
    ths.trades_data = [
        {"成交时间": "09:30:05", "证券代码": "000001", "操作": "证券买入",
         "成交均价": 9.98, "成交数量": 400, "成交金额": 3992.0,
         "成交编号": "CJ001", "合同编号": "HT001"},
    ]
    ts = gw.query_trades()
    assert ts[0]["traded_id"] == "CJ001"
    assert ts[0]["order_id"] == "HT001"
    assert ts[0]["direction"] == DIRECTION_BUY   # "证券买入" 含 "买"
    assert ts[0]["price"] == 9.98
    assert ts[0]["amount"] == 3992.0


# ── 状态文字映射 (子串优先级) ────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("已报", OS_REPORTED),
    ("未成交", OS_REPORTED),       # 未识别 → 保守按已报 (不假装终态)
    ("部成", OS_PART_SUCC),
    ("全部成交", OS_SUCCEEDED),
    ("已成", OS_SUCCEEDED),
    ("已撤", OS_CANCELED),
    ("部成待撤", OS_PARTSUCC_CANCEL),   # 含 "部成", 必须先匹配长串
    ("已报待撤", OS_REPORTED_CANCEL),
    ("废单", OS_JUNK),
])
def test_status_text_mapping(text, expected):
    from trade.gateway_ths import _status_from_text
    assert _status_from_text(text) == expected


# ── 行情腿 fail-closed ───────────────────────────────────────

def test_query_quotes_always_empty(gw):
    assert gw.query_quotes(["000001.SZ"]) == {}
    assert gw.subscribe_quotes(["000001.SZ"]) is True
