"""T1 [先红] 唯一下单口 Executor.place_order 单元测试。

计划书: docs/plan/2026-09-05_唯一下单口收口_计划书.md §四 T1
锚定六件事: 七步脊柱副作用 / 风控拒静默 / 人工买不入账 /
风控价口径 (审计 P1: 风控吃 risk_price, 发单/入账用 price) /
审计 extra 的 order_id 占位回填 (审计 P2: 键序与现状逐字节一致) /
created_ts 不继承 (泰山石油事件语义)。

装配方式照抄 tests/trade/test_executor.py (FakeGateway + 临时 store + 注入 clock)。
"""
import json
import logging
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.book import DIRECTION_BUY, DIRECTION_SELL, OS_REPORTED, Book
from trade.config import LadderTpConfig, StopConfig, TradeConfig
from trade.executor import Executor, PlaceRequest
from trade.gateway import FakeGateway
from trade.risk import KillSwitch, RiskContext, RiskGate
from trade.store import TradeStore

CODE = "600519.SH"


@pytest.fixture()
def store(tmp_path):
    s = TradeStore(tmp_path / "trade.db", tmp_path / "raw.jsonl")
    yield s
    s.close()


@pytest.fixture()
def kill(store, tmp_path):
    return KillSwitch(store, tmp_path / "KILL")


def _seed_book(book, code, volume, cost=10.0, can_use=None):
    book.apply_trade(f"T-seed-{code}", f"O-seed-{code}", code,
                     DIRECTION_BUY, cost, volume)
    book.set_can_use(code, can_use if can_use is not None else volume)


def _make_executor(store, kill, book, gw, clock=None):
    cfg = TradeConfig(
        account_id="TEST",
        stop=StopConfig(ladder_tp=LadderTpConfig(levels=((0.05, 0.33),))),
    )
    gate = RiskGate(kill, store, cfg.daily_loss_limit, sizing=cfg.position_sizing)

    def build_ctx():
        return RiskContext(
            reconcile_passed=True, total_asset=1_000_000.0,
            positions=book.snapshot()["positions"],
            day_baseline_equity=1_000_000.0, current_equity=1_000_000.0,
            is_trading_day=True,
        )

    return Executor(
        gw, book, store, gate, cfg, build_ctx,
        clock=clock or (lambda: 1000.0),
    )


def _gw_with(volume=1000, can_use=None, cost=10.0, code=CODE):
    gw = FakeGateway(positions={code: {"volume": volume,
                                       "can_use": can_use if can_use is not None else volume,
                                       "avg_cost": cost}})
    gw.connect()
    return gw


def _sell_req(**over):
    base = dict(code=CODE, direction=DIRECTION_SELL, price=10.5, qty=100,
                remark_prefix="X", fill_payload={"label": "测试卖"},
                audit_kind="spine_place", audit_message=f"{CODE} 卖出 100@10.5",
                audit_extra={"code": CODE, "order_id": None, "qty": 100})
    base.update(over)
    return PlaceRequest(**base)


def test_place_order_full_spine(store, kill):
    """成功路径七步副作用: 发单(限价/数量) → 登记成交原因 → 订单簿
    OS_REPORTED → 落库(含 created_ts == 注入 clock) → 审计(kind/文案)。"""
    gw = _gw_with()
    bk = Book()
    _seed_book(bk, CODE, 1000, 10.0)
    ex = _make_executor(store, kill, bk, gw, clock=lambda: 1000.0)

    order_id, why = ex.place_order(_sell_req())

    assert order_id is not None and why is None
    # ① 发单恰好一笔
    assert list(gw._orders) == [order_id]
    # ② 订单簿 OS_REPORTED, 价格/数量/remark 前缀形态
    rec = bk.snapshot()["orders"][order_id]
    assert rec.status == OS_REPORTED and rec.price == 10.5 and rec.qty == 100
    assert re.fullmatch(r"V\d{4}-\d{3}X", rec.remark)
    # ③ 落库含下单时刻 (显式 created_ts, 泰山石油语义)
    row = store._conn.execute(
        "SELECT price, qty, status, created_ts FROM orders WHERE order_id=?",
        (order_id,)).fetchone()
    assert row == (10.5, 100, OS_REPORTED, 1000.0)
    # ④ 成交通知上下文已登记
    assert ex.fill_ctx.peek(order_id) == {"label": "测试卖"}
    # ⑤ 审计已写, extra 的 order_id 占位已回填
    arow = store._conn.execute(
        "SELECT kind, message, detail_json FROM audit WHERE kind='spine_place'"
    ).fetchone()
    assert arow is not None
    assert json.loads(arow[2])["order_id"] == order_id


def test_place_order_risk_reject_silent(store, kill, caplog):
    """风控拒 (sizing 金额上限): (None, why) 静默返回 —— 不发单、不落库、
    不写成交审计、不打 warning (风控层已自写 risk_reject 审计, 调用方自留痕)。"""
    gw = _gw_with(volume=0)
    ex = _make_executor(store, kill, Book(), gw)

    # 25000 > max_buy_amount(20000) → sizing 拒
    req = PlaceRequest(code=CODE, direction=DIRECTION_BUY, price=250.0, qty=100,
                       remark_prefix="B", fill_payload={"label": "TDX买入"},
                       audit_kind="spine_place", audit_message="不应出现",
                       audit_extra={"code": CODE})
    order_id, why = ex.place_order(req)

    assert order_id is None and why.startswith("sizing:")
    assert len(gw._orders) == 0
    assert store._conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT COUNT(*) FROM audit WHERE kind='spine_place'").fetchone()[0] == 0
    with caplog.at_level(logging.WARNING, logger="trade.executor"):
        ex.place_order(req)
    assert "trade.executor" not in caplog.text


def test_place_order_manual_no_accounting(store, kill):
    """人工买 (account_immediately=False): 发单+登记原因+审计照走,
    订单簿/落库**均无** —— 回报经事件链入账 (行为锚定, 与四路分叉
    已登记 CHANGELOG 待单独立项)。manual 跳过金额上限但不跳下限
    (risk.py:190/:195), 故用 2500 元过下限。"""
    gw = _gw_with(volume=0)
    ex = _make_executor(store, kill, Book(), gw)

    req = PlaceRequest(code=CODE, direction=DIRECTION_BUY, price=25.0, qty=100,
                       intent_flags={"manual": True},
                       account_immediately=False,
                       remark_prefix="B", fill_payload={"label": "人工买入"},
                       audit_kind="manual_buy", audit_message="人工买入",
                       audit_extra={"code": CODE, "order_id": None})
    order_id, why = ex.place_order(req)

    assert order_id is not None and why is None
    assert len(gw._orders) == 1
    assert ex.fill_ctx.peek(order_id) == {"label": "人工买入"}
    assert store._conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert ex._book.snapshot()["orders"] == {}
    assert store._conn.execute(
        "SELECT COUNT(*) FROM audit WHERE kind='manual_buy'").fetchone()[0] == 1


def test_place_order_risk_price(store, kill):
    """风控价口径 (审计 P1): 风控吃 risk_price(参考价), 发单/订单簿/落库
    用 price(委托价) —— 收盘竞价涨停/笼子上限场景两者可差 10%+。"""
    gw = _gw_with(volume=0)
    ex = _make_executor(store, kill, Book(), gw)

    # 委托价 250×100=25000 超上限, 参考价 150×100=15000 可过 → 必须放行
    req = PlaceRequest(code=CODE, direction=DIRECTION_BUY, price=250.0, qty=100,
                       risk_price=150.0,
                       remark_prefix="B", fill_payload={"label": "TDX买入"},
                       audit_kind="spine_place", audit_message="risk_price 锚定",
                       audit_extra={"code": CODE, "order_id": None})
    order_id, why = ex.place_order(req)

    assert order_id is not None, f"风控应吃参考价 150 而非委托价 250: {why}"
    # 发单与订单簿用的都是委托价 250
    row = store._conn.execute(
        "SELECT price FROM orders WHERE order_id=?", (order_id,)).fetchone()
    assert row == (250.0,)


def test_audit_extra_order_id_placeholder(store, kill):
    """审计 extra 的 order_id 占位回填 (审计 P2): 调用方以 None 占位于
    历史键位, 脊柱回填 —— 落库 detail_json 键序与占位键位逐字节一致
    (store.py json.dumps 不排序, 键序即插入序)。"""
    gw = _gw_with()
    bk = Book()
    _seed_book(bk, CODE, 1000, 10.0)
    ex = _make_executor(store, kill, bk, gw)

    order_id, _ = ex.place_order(_sell_req(
        audit_extra={"code": CODE, "order_id": None, "qty": 100}))

    detail_json = store._conn.execute(
        "SELECT detail_json FROM audit WHERE kind='spine_place'").fetchone()[0]
    d = json.loads(detail_json)
    assert list(d.keys()) == ["code", "order_id", "qty"]   # 键位不变
    assert d["order_id"] == order_id                        # 值已回填


def test_created_ts_not_inherited(store, kill):
    """泰山石油事件锚定: 同码两单、clock 推进 → 两条 created_ts 各自
    显式记录下单时刻, 新单不继承旧值。"""
    gw = _gw_with()
    bk = Book()
    _seed_book(bk, CODE, 1000, 10.0)
    ts = {"t": 1000.0}
    ex = _make_executor(store, kill, bk, gw, clock=lambda: ts["t"])

    oid1, _ = ex.place_order(_sell_req(audit_extra={"code": CODE, "order_id": None}))
    ts["t"] = 2000.0
    oid2, _ = ex.place_order(_sell_req(audit_extra={"code": CODE, "order_id": None}))

    assert oid1 != oid2
    rows = store._conn.execute(
        "SELECT order_id, created_ts FROM orders").fetchall()
    assert dict(rows) == {oid1: 1000.0, oid2: 2000.0}
