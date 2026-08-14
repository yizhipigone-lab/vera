"""网关回调接线测试 (2026-08-07, 0807 废单事件).

背景: gateway._Cb 的回调方法名曾写成 on_order_status/on_deal_status —
XtQuantTraderCallback 查无此方法 (dir() 实证), 上线以来委托/成交回报
从未到达生产系统, 订单状态全靠对账轮询; 且未接 on_order_error,
10 笔限价单未达柜台的死因成黑盒。

本文件锁三件事:
1. 契约: 回调方法名必须是 xtquant 官方名 (环境无 xtquant 时跳过,
   有就强行对齐 —— 防再次写错/官方改名)
2. 路由: on_stock_order/on_stock_trade/on_order_error/on_disconnected
   各自转发到注入的 callable (不依赖 xtquant, base 传 object)
3. trade_main: EVENT_ORDER_ERROR → audit 落原文
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.book import DIRECTION_BUY, OS_JUNK, OS_REPORTED, OS_SUCCEEDED
from trade.config import TradeConfig
from trade.gateway import RealGateway, _build_trader_callback
from trade_main import TradeApp

CODE = "600519.SH"


def _make_gw(**cbs):
    """不 connect 的 RealGateway, 只验回调路由 (转换函数是纯逻辑)。"""
    return RealGateway(account_id="T", timeout_sec=1.0, **cbs)


# ═══════════════════════════════════════════════════════════════
# 1. 契约: 方法名对齐 xtquant 官方回调
# ═══════════════════════════════════════════════════════════════

def test_callback_names_match_xtquant_contract():
    """我们定义的回调方法名必须全部存在于 XtQuantTraderCallback —
    0807 前 on_order_status/on_deal_status 两个错名导致回报通道全断。"""
    xtquant = pytest.importorskip("xtquant", reason="无 xtquant 环境跳过契约校验")
    from xtquant.xttrader import XtQuantTraderCallback
    official = {m for m in dir(XtQuantTraderCallback) if m.startswith("on_")}
    gw = _make_gw()
    cb = _build_trader_callback(gw, object)
    ours = {m for m in dir(cb) if m.startswith("on_")}
    assert ours, "回调类一个 on_* 方法都没有"
    wrong = ours - official
    assert not wrong, f"回调方法名不在 xtquant 官方集合内 (永远不会被调用): {wrong}"
    # 关键三件套必须在: 委托/成交/下单失败 (+撤单失败 2026-08-07)
    assert {"on_stock_order", "on_stock_trade", "on_order_error",
            "on_cancel_error"} <= ours


# ═══════════════════════════════════════════════════════════════
# 2. 路由: 四个回调各自转发 (base=object, 不需要 xtquant)
# ═══════════════════════════════════════════════════════════════

def test_on_stock_order_routes_to_on_order():
    got = []
    gw = _make_gw(on_order=got.append)
    cb = _build_trader_callback(gw, object)
    cb.on_stock_order(SimpleNamespace(
        order_id=123, order_remark="R1", stock_code=CODE, order_type=DIRECTION_BUY,
        price=10.0, order_volume=500, traded_volume=0, order_status=50,
        order_time=int(time.time())))
    assert len(got) == 1 and got[0]["order_id"] == "123" and got[0]["code"] == CODE


def test_on_stock_trade_routes_to_on_trade():
    got = []
    gw = _make_gw(on_trade=got.append)
    cb = _build_trader_callback(gw, object)
    cb.on_stock_trade(SimpleNamespace(
        traded_id=456, order_id=123, stock_code=CODE, order_type=DIRECTION_BUY,
        traded_price=10.0, traded_volume=500, traded_amount=5000.0,
        traded_time=int(time.time())))
    assert len(got) == 1 and got[0]["traded_id"] == "456"
    assert got[0]["ts"] is not None          # traded_time or None 兜底 (HIGH#1)


def test_on_order_error_routes_reason():
    """下单失败: order_id/error_id/error_msg 原文转发。"""
    got = []
    gw = _make_gw(on_order_error=got.append)
    cb = _build_trader_callback(gw, object)
    cb.on_order_error(SimpleNamespace(
        order_id=789, error_id=-33, error_msg="无科创板交易权限"))
    assert got == [{"order_id": "789", "error_id": -33,
                    "error_msg": "无科创板交易权限"}]


def test_on_disconnected_still_routes():
    got = []
    gw = _make_gw(on_disconnected=got.append)
    cb = _build_trader_callback(gw, object)
    cb.on_disconnected()
    assert got == ["xtquant 回调通知断线"]


def test_on_cancel_error_routes_reason():
    """撤单失败 (XtCancelError): order_id/error_id/error_msg 原文转发。"""
    got = []
    gw = _make_gw(on_cancel_error=got.append)
    cb = _build_trader_callback(gw, object)
    cb.on_cancel_error(SimpleNamespace(
        order_id=321, error_id=-47, error_msg="订单已成交不可撤"))
    assert got == [{"order_id": "321", "error_id": -47,
                    "error_msg": "订单已成交不可撤"}]


def test_order_to_dict_carries_status_msg():
    """_order_to_dict 采 status_msg (废单原因); 字段缺失回退空串。"""
    o = SimpleNamespace(
        order_id=1, order_remark="R", stock_code=CODE, order_type=DIRECTION_BUY,
        price=10.0, order_volume=100, traded_volume=0, order_status=57,
        order_time=0, status_msg="无科创板交易权限")
    d = RealGateway._order_to_dict(o)
    assert d["status_msg"] == "无科创板交易权限"
    o2 = SimpleNamespace(**{**vars(o), "status_msg": None})
    assert RealGateway._order_to_dict(o2)["status_msg"] == ""
    del o2.status_msg
    assert RealGateway._order_to_dict(o2)["status_msg"] == ""


def test_none_callbacks_no_crash():
    """四路 callable 全 None → 回调来了也不炸 (BaseGateway 契约: 可为 None)。"""
    gw = _make_gw()
    cb = _build_trader_callback(gw, object)
    cb.on_stock_order(SimpleNamespace(
        order_id=1, order_remark="", stock_code=CODE, order_type=23,
        price=1.0, order_volume=100, traded_volume=0, order_status=50,
        order_time=0))
    cb.on_disconnected()                     # 不抛即过


# ═══════════════════════════════════════════════════════════════
# 3. trade_main: EVENT_ORDER_ERROR → audit 落原文
# ═══════════════════════════════════════════════════════════════

@pytest.fixture()
def app(tmp_path):
    cfg = TradeConfig(
        account_id="CB", fake_sdk=True,
        db_path=str(tmp_path / "t.db"),
        raw_log_path=str(tmp_path / "r.jsonl"),
        kill_flag_path=str(tmp_path / "KILL"),
    )
    a = TradeApp(cfg, fake=True, clock=time.time,
                 fake_gateway_kwargs={"cash": 1e6, "positions": {}})
    yield a
    a.stop()


def test_order_error_event_lands_in_audit(app):
    app._on_order_error({"order_id": "2014314579", "error_id": -33,
                         "error_msg": "无科创板交易权限"})
    rows = app.store._conn.execute(
        "SELECT kind, message, detail_json FROM audit WHERE kind='order_error'"
    ).fetchall()
    assert len(rows) == 1
    assert "无科创板交易权限" in rows[0][1]
    assert "2014314579" in rows[0][1]


# ═══════════════════════════════════════════════════════════════
# 4. trade_main: EVENT_ORDER_ERROR → 订单推进废单终态 (2026-08-10,
#    0810 事件: 收盘后 15 笔买入被柜台拒, 页面却永远停"已报")
# ═══════════════════════════════════════════════════════════════

def _place_order(app, oid: str, status: int = OS_REPORTED) -> None:
    """在 book + orders 表登记一张在途单 (模拟下单路径已记的已报)。"""
    app.book.apply_order_update(
        oid, status, code=CODE, direction=DIRECTION_BUY,
        price=10.0, qty=100, remark="V0810-001B")
    app.store.save_order({
        "order_id": oid, "remark": "V0810-001B", "code": CODE,
        "direction": DIRECTION_BUY, "price": 10.0, "qty": 100,
        "status": status})


def test_order_error_marks_order_junk(app):
    """在途单收到拒单回报 → book 与 orders 表都进废单 (57),
    status_msg 带拒单原因原文 (页面状态说明列直接可见)。"""
    _place_order(app, "672137227")
    app._on_order_error({"order_id": "672137227", "error_id": -61,
                         "error_msg": "当前时间不允许交易该证券业务"})
    assert app.book.snapshot()["orders"]["672137227"].status == OS_JUNK
    row = app.store._conn.execute(
        "SELECT status, status_msg FROM orders WHERE order_id='672137227'"
    ).fetchone()
    assert row[0] == OS_JUNK
    assert "当前时间不允许交易该证券业务" in row[1]


def test_order_error_unknown_order_only_audits(app):
    """非本系统订单 (券商端手工单) 的拒单回报: 只落 audit,
    不得在 book/orders 表凭空造单。"""
    app._on_order_error({"order_id": "999999999", "error_id": -61,
                         "error_msg": "当前时间不允许交易该证券业务"})
    assert "999999999" not in app.book.snapshot()["orders"]
    assert app.store._conn.execute(
        "SELECT COUNT(*) FROM orders WHERE order_id='999999999'"
    ).fetchone()[0] == 0


def test_order_error_does_not_override_terminal(app):
    """终态单 (状态回报已先到的竞态) 不被拒单回报盖回废单。"""
    _place_order(app, "672137228", status=OS_SUCCEEDED)
    app._on_order_error({"order_id": "672137228", "error_id": -61,
                         "error_msg": "当前时间不允许交易该证券业务"})
    assert app.book.snapshot()["orders"]["672137228"].status == OS_SUCCEEDED
    row = app.store._conn.execute(
        "SELECT status FROM orders WHERE order_id='672137228'"
    ).fetchone()
    assert row[0] == OS_SUCCEEDED
