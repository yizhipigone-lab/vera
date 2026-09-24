"""Reconciler 同花顺占位号回填认亲测试 (2026-09-07 T5, 方案设计书 §5.7)。

锁住: 本地 THS 占位单与券商合同编号 (code+方向+价+量+时间窗) 唯一命中 →
book 内存键迁移 + store 落库重绑 + trades 级联 + audit ths_backfill;
多笔歧义 / 时间窗不中 → 不重绑不猜测。store.rebind_order 语义单独锁。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.book import DIRECTION_BUY, OS_REPORTED, Book  # noqa: E402
from trade.reconciler import Reconciler  # noqa: E402
from trade.risk import KillSwitch  # noqa: E402
from trade.store import TradeStore  # noqa: E402

CODE = "600519.SH"
T0 = 1_700_000_000.0   # 锚定时间 (epoch 秒, 测试任意固定)


class OrdersGw:
    """只供 _sync_orders 的替身网关: 回放券商当日委托, 成交/持仓为空。"""

    def __init__(self, orders):
        self._orders = orders

    def connect(self):
        pass

    def query_positions(self):
        return []

    def query_asset(self):
        return {"total_asset": 1.0}

    def query_trades(self):
        return []

    def query_orders(self):
        return list(self._orders)


@pytest.fixture()
def store(tmp_path):
    s = TradeStore(tmp_path / "trade.db", tmp_path / "raw.jsonl")
    yield s
    s.close()


@pytest.fixture()
def kill(store, tmp_path):
    return KillSwitch(store, tmp_path / "KILL")


def _seed_local_ths(book, store, oid="THS0907-001", ts=T0, price=10.0,
                    qty=100, remark="THS0907-001"):
    book.apply_order_update(oid, OS_REPORTED, code=CODE, direction=DIRECTION_BUY,
                            price=price, qty=qty, filled_qty=0, remark=remark)
    store.save_order({"order_id": oid, "remark": remark, "code": CODE,
                      "direction": DIRECTION_BUY, "price": price, "qty": qty,
                      "filled_qty": 0, "status": OS_REPORTED,
                      "created_ts": ts, "status_msg": ""})


def _broker_order(oid="900001", ts=T0 + 60, price=10.0, qty=100):
    return {"order_id": oid, "code": CODE, "direction": DIRECTION_BUY,
            "price": price, "qty": qty, "filled_qty": 0,
            "status": OS_REPORTED, "ts": ts, "status_msg": ""}


def _sync(store, kill, gw):
    rec = Reconciler(gw, Book(), store, kill, retry_interval_sec=0.0)
    # 注意: 占位单要落进 book —— 这里共用同一 book 实例
    return rec


def test_unique_match_rebinds(store, kill):
    """唯一命中 → 占位号重绑为合同编号 (book 内存 + store 落库 + audit)。"""
    book = Book()
    _seed_local_ths(book, store, ts=T0)
    gw = OrdersGw([_broker_order(ts=T0 + 60)])
    rec = Reconciler(gw, book, store, kill, retry_interval_sec=0.0)
    rec._sync_orders(T0 + 60)
    orders = book.snapshot()["orders"]
    assert "900001" in orders            # 已换绑为券商合同编号
    assert "THS0907-001" not in orders   # 占位号消失
    assert orders["900001"].remark == "THS0907-001"   # 策略归属保留
    row = store._conn.execute(
        "SELECT order_id, remark FROM orders WHERE order_id='900001'").fetchone()
    assert row and row[1] == "THS0907-001"
    kinds = {r[0] for r in store._conn.execute("SELECT kind FROM audit")}
    assert "ths_backfill" in kinds


def test_ambiguous_two_placeholders_no_rebind(store, kill):
    """同码同价同量两笔占位单 → 歧义, 不猜不重绑。"""
    book = Book()
    _seed_local_ths(book, store, oid="THS0907-001", ts=T0)
    _seed_local_ths(book, store, oid="THS0907-002", ts=T0 + 1)
    gw = OrdersGw([_broker_order(ts=T0 + 60)])
    rec = Reconciler(gw, book, store, kill, retry_interval_sec=0.0)
    rec._sync_orders(T0 + 60)
    orders = book.snapshot()["orders"]
    assert "THS0907-001" in orders and "THS0907-002" in orders
    # broker 单会作为新单正常入库 (remark 空), 但未发生重绑 (无 ths_backfill)
    assert orders.get("900001") is None or orders["900001"].remark == ""
    kinds = {r[0] for r in store._conn.execute("SELECT kind FROM audit")}
    assert "ths_backfill" not in kinds


def test_time_window_miss_no_rebind(store, kill):
    """券商时间超出 ±5min 窗 → 不认亲。"""
    book = Book()
    _seed_local_ths(book, store, ts=T0)
    gw = OrdersGw([_broker_order(ts=T0 + 3600)])
    rec = Reconciler(gw, book, store, kill, retry_interval_sec=0.0)
    rec._sync_orders(T0 + 3600)
    orders = book.snapshot()["orders"]
    assert "THS0907-001" in orders
    assert orders.get("900001") is None or orders["900001"].remark == ""


def test_no_local_placeholder_noop(store, kill):
    """本地无占位单 → 认亲逻辑零打扰 (正常 QMT 路径不受影响)。"""
    book = Book()
    gw = OrdersGw([_broker_order()])
    rec = Reconciler(gw, book, store, kill, retry_interval_sec=0.0)
    rec._sync_orders(T0 + 60)
    # 券商单正常入库为新委托 (无占位可认, 走原有建单路径)
    orders = book.snapshot()["orders"]
    assert "900001" in orders


def test_store_rebind_order_semantics(store):
    """store.rebind_order 幂等与冲突语义。"""
    _seed_local_ths(Book(), store, ts=T0)
    # 目标不存在 → True
    assert store.rebind_order("THS0907-001", "900001") is True
    row = store._conn.execute(
        "SELECT order_id FROM orders WHERE order_id='900001'").fetchone()
    assert row
    # 旧键已删
    assert store._conn.execute(
        "SELECT 1 FROM orders WHERE order_id='THS0907-001'").fetchone() is None
    # 再次重绑 (旧键没了) → False
    assert store.rebind_order("THS0907-001", "900002") is False
    # 目标已占用 → False
    _seed_local_ths(Book(), store, oid="THS0907-003", ts=T0)
    assert store.rebind_order("THS0907-003", "900001") is False


def test_book_rebind_order_semantics():
    """book.rebind_order: 键迁移保留 remark; 冲突/缺失返回 False。"""
    book = Book()
    book.apply_order_update("THS-A", OS_REPORTED, code=CODE,
                            direction=DIRECTION_BUY, price=10.0, qty=100,
                            filled_qty=0, remark="THS-A")
    assert book.rebind_order("THS-A", "900-A") is True
    orders = book.snapshot()["orders"]
    assert "THS-A" not in orders and orders["900-A"].remark == "THS-A"
    assert book.rebind_order("THS-A", "900-B") is False          # 旧键缺失
    assert book.rebind_order("900-A", "900-C") is True
    assert book.rebind_order("900-C", "900-C") is False          # 新键=自己(占位)
