"""Reconciler 三方对账单元测试 (P1 第二阶段, 2026-07-26).

锁住: 三方一致 NONE / 数量偏差 CRITICAL 触发急停 / 在途成交扣除
降级 WARN / 永不回写 book 与 store 持仓 / 无价时成本价兜底并标注。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.book import DIRECTION_BUY, DIRECTION_SELL, Book
from trade.gateway import FakeGateway
from trade.reconciler import (
    LEVEL_CRITICAL,
    LEVEL_NONE,
    LEVEL_UNKNOWN,
    LEVEL_WARN,
    Reconciler,
)
from trade.risk import KillSwitch
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


def _book_with(volume: int, cost: float = 10.0) -> Book:
    book = Book()
    book.apply_trade("T-seed", "O-seed", CODE, DIRECTION_BUY, cost, volume)
    book.set_can_use(CODE, volume)
    return book


def _reconciler(store, kill, book, gw_positions, quote=None, in_flight=None):
    gw = FakeGateway(positions={CODE: {"volume": gw_positions, "can_use": gw_positions,
                                       "avg_cost": 10.0}} if gw_positions else {})
    gw.connect()
    return Reconciler(
        gw, book, store, kill,
        quote_price=quote or (lambda code: None),
        in_flight_sells=in_flight or (dict),
        retry_interval_sec=0.0,   # 测试不等真实 1s 重试间隔
    )


def test_consistent_three_way_is_none(store, kill):
    """A=B=500, 无差异 → NONE, 且留"对账跑过"的痕迹。"""
    rec = _reconciler(store, kill, _book_with(500), 500)
    report = rec.reconcile()
    assert report.level == LEVEL_NONE and report.passed
    rows = store._conn.execute("SELECT level FROM reconcile_log").fetchall()
    assert rows and rows[0][0] == LEVEL_NONE


def test_critical_diff_triggers_kill_switch(store, kill):
    """本地 500 vs QMT 100, 差 400 > 门限 → CRITICAL + 急停。"""
    rec = _reconciler(store, kill, _book_with(500), 100)
    report = rec.reconcile()
    assert report.level == LEVEL_CRITICAL and not report.passed
    assert kill.is_active()
    rows = store._conn.execute(
        "SELECT level, code, expected, actual FROM reconcile_log"
    ).fetchall()
    assert (LEVEL_CRITICAL, CODE, "500", "100") in rows


def test_in_flight_sell_downgrades_to_warn(store, kill):
    """差异 400 恰有 400 在途卖单 → WARN, 不触发急停。"""
    rec = _reconciler(store, kill, _book_with(500), 100,
                      in_flight=lambda: {CODE: 400})
    report = rec.reconcile()
    assert report.level == LEVEL_WARN and report.passed
    assert not kill.is_active()
    assert report.diffs[0].level == LEVEL_WARN


def test_diff_within_threshold_is_none(store, kill):
    """偏差 50 股 < 门限 100 股 → NONE。"""
    rec = _reconciler(store, kill, _book_with(500), 450)
    report = rec.reconcile()
    assert report.level == LEVEL_NONE and report.passed


def test_cost_fallback_when_no_quote(store, kill):
    """无行情快照 → 成本价兜底, 报告标注 price_source=cost。"""
    rec = _reconciler(store, kill, _book_with(500), 100)
    report = rec.reconcile()
    assert report.diffs[0].price_source == "cost"
    # 有报价时标注 quote
    rec2 = _reconciler(store, kill, _book_with(500), 100,
                       quote=lambda code: 10.5)
    kill2_report = rec2.reconcile()
    assert kill2_report.diffs[0].price_source == "quote"


def test_reconcile_never_writes_back(store, kill):
    """铁律 1: 对账后 book 持仓与昨仓快照都不被改动。"""
    store.save_position_snapshot(
        {CODE: {"volume": 1000, "can_use": 1000, "avg_cost": 10.0}})
    book = _book_with(500)
    rec = _reconciler(store, kill, book, 100)
    rec.reconcile()
    assert book.snapshot()["positions"][CODE].volume == 500
    assert store.load_position_snapshot()[CODE]["volume"] == 1000


def test_b_vs_c_mismatch_is_warn(store, kill):
    """A==B 但 B≠C (昨仓 1000 + 今日无成交, 本地却 900) → 事件流疑似丢失 WARN。"""
    store.save_position_snapshot(
        {CODE: {"volume": 1000, "can_use": 1000, "avg_cost": 10.0}})
    rec = _reconciler(store, kill, _book_with(900), 900)
    report = rec.reconcile(now_ts=time.time())
    assert report.level == LEVEL_WARN
    assert any("还原" in d.reason for d in report.diffs)


def test_c_side_uses_today_net_trades(store, kill):
    """C = 昨仓 + 当日成交净额: 昨仓 1000 今日买 200 → C=1200, 与 B 一致不告警。"""
    store.save_position_snapshot(
        {CODE: {"volume": 1000, "can_use": 1000, "avg_cost": 10.0}})
    store.save_trade({"traded_id": "T-today", "order_id": "O-x", "code": CODE,
                      "direction": DIRECTION_BUY, "price": 10.0, "qty": 200,
                      "ts": time.time()})
    rec = _reconciler(store, kill, _book_with(1200), 1200)
    report = rec.reconcile(now_ts=time.time())
    assert report.level == LEVEL_NONE


# ═══════════════════════════════════════════════════════════════
# P0-③ 实测修复: 断线空返回 ≠ 持仓清零
# ═══════════════════════════════════════════════════════════════

def test_empty_query_with_local_positions_is_unknown(store, kill):
    """P0-③: 本地有持仓 + 连续空查询 → UNKNOWN (不急停, 不误判清零)。"""
    rec = _reconciler(store, kill, _book_with(500), 0)
    report = rec.reconcile()
    assert report.level == LEVEL_UNKNOWN
    assert not kill.is_active()               # 不触发急停
    assert report.passed                      # 不置 reconciled=False
    rows = store._conn.execute(
        "SELECT level FROM reconcile_log WHERE level='UNKNOWN'").fetchall()
    assert rows
    rows = store._conn.execute(
        "SELECT kind FROM audit WHERE kind='reconcile_unknown'").fetchall()
    assert rows


def test_empty_query_with_empty_book_is_none(store, kill):
    """空查询 + 本地也空 → 真空仓, 正常 NONE。"""
    book = Book()
    rec = _reconciler(store, kill, book, 0)
    report = rec.reconcile()
    assert report.level == LEVEL_NONE


def test_query_retry_recovers(store, kill):
    """重试机制: 前 2 次空第 3 次有 → 按正常对账走 (不冤枉一次抖动)。"""

    class FlakyGw:
        def __init__(self):
            self.calls = 0

        def query_positions(self):
            self.calls += 1
            if self.calls < 3:
                return []
            return [{"code": CODE, "volume": 500, "can_use": 500,
                     "avg_cost": 10.0}]

        def query_trades(self):
            return []   # 手工认领 (2026-07-27 裁决④): 无手工单剧本

    rec = Reconciler(FlakyGw(), _book_with(500), store, kill,
                     retry_interval_sec=0.0)
    report = rec.reconcile()
    assert report.level == LEVEL_NONE
    assert rec._gateway.calls == 3


# ═══════════════════════════════════════════════════════════════
# 2026-07-30: sync_reports 增量同步 (成交补记归因 + 委托状态回写)
# ═══════════════════════════════════════════════════════════════

def _gw():
    gw = FakeGateway()
    gw.connect()
    return gw


def test_adopt_system_order_inherits_remark(store, kill):
    """归因修复: 本地订单簿查得到 order_id = 系统的单 (成交回调丢失),
    策略归属继承该单 remark, audit 记 trade_backfill, 不误标"手工";
    同时以成交硬事实回写 orders 表进度。"""
    gw = _gw()
    book = Book()
    oid = gw.order(CODE, DIRECTION_BUY, 10.0, 100, remark="V0730-B1")
    # 系统下过单: book + store 都有委托记录 (回报链正常走到已报)
    book.apply_order_update(oid, 50, code=CODE, direction=DIRECTION_BUY,
                            price=10.0, qty=100, remark="V0730-B1")
    store.save_order({"order_id": oid, "remark": "V0730-B1", "code": CODE,
                      "direction": DIRECTION_BUY, "price": 10.0, "qty": 100,
                      "status": 50})
    gw.simulate_fill(oid)   # 成交回报丢失的剧本: 网关已成交, book 没见过
    rec = Reconciler(gw, book, store, kill, retry_interval_sec=0.0)
    result = rec.sync_reports()
    assert result["adopted"] == 1
    pos = book.snapshot()["positions"][CODE]
    assert pos.volume == 100 and pos.strategy == "V0730-B1"   # 不是"手工"
    kinds = {r[0] for r in store._conn.execute("SELECT kind FROM audit")}
    assert "trade_backfill" in kinds and "manual_adopt" not in kinds
    row = store._conn.execute(
        "SELECT filled_qty, status FROM orders WHERE order_id=?",
        (oid,)).fetchone()
    assert row == (100, 56)   # 成交硬事实回写: 满量已成
    # 2026-07-30: 系统单补记 source=system (不是 manual)
    assert store._conn.execute(
        "SELECT source FROM trades WHERE order_id=?", (oid,)).fetchone()[0] == "system"


def test_adopt_backfill_writes_fill_reason(store, kill):
    """2026-07-31 (0731 两笔卖出回调缺失实测): 回调丢失走补记时,
    executor fill ctx 仍在, 补记路径同样取走落 trades.reason;
    手工单 (本地无委托) reason 空串。"""
    gw = _gw()
    book = Book()
    oid = gw.order(CODE, DIRECTION_SELL, 35.21, 100, remark="V0731-001X")
    book.apply_order_update(oid, 50, code=CODE, direction=DIRECTION_SELL,
                            price=35.21, qty=100, remark="V0731-001X")
    store.save_order({"order_id": oid, "remark": "V0731-001X", "code": CODE,
                      "direction": DIRECTION_SELL, "price": 35.21, "qty": 100,
                      "status": 50})
    gw.simulate_fill(oid)
    # executor 侧下单时已登记 fill ctx (回调丢失, 没人读)
    ctx = {oid: {"label": "移动止盈"}}
    rec = Reconciler(gw, book, store, kill, retry_interval_sec=0.0,
                     pop_fill_context=lambda o: ctx.get(o, {}),
                     reason_from_ctx=lambda c: c.get("label", ""))
    assert rec.sync_reports()["adopted"] == 1
    assert store._conn.execute(
        "SELECT reason FROM trades WHERE order_id=?", (oid,)).fetchone()[0] == "移动止盈"


def test_adopt_partial_fills_share_reason(store, kill):
    """2026-07-31 (金逸影视 1300 股拆 6 笔实例): 同一订单的部成多笔
    共享同一份原因 —— peek 不删, 订单终态才由 on_order_terminal 回收。"""
    gw = _gw()
    book = Book()
    book.apply_trade("T-seed", "O-seed", CODE, DIRECTION_BUY, 7.0, 2000)
    oid = gw.order(CODE, DIRECTION_SELL, 7.36, 1300, remark="V0731-005X")
    book.apply_order_update(oid, 50, code=CODE, direction=DIRECTION_SELL,
                            price=7.36, qty=1300, remark="V0731-005X")
    store.save_order({"order_id": oid, "remark": "V0731-005X", "code": CODE,
                      "direction": DIRECTION_SELL, "price": 7.36, "qty": 1300,
                      "status": 50})
    # 部成两笔 (400 + 900), 回调全丢
    gw.simulate_fill(oid, qty=400)
    gw.simulate_fill(oid, qty=900)
    ctx = {oid: {"label": "TDX买入"}}
    discarded = []
    rec = Reconciler(gw, book, store, kill, retry_interval_sec=0.0,
                     pop_fill_context=lambda o: ctx.get(o, {}),
                     reason_from_ctx=lambda c: c.get("label", ""),
                     on_order_terminal=discarded.append)
    assert rec.sync_reports()["adopted"] == 2
    rows = store._conn.execute(
        "SELECT qty, reason FROM trades WHERE order_id=? ORDER BY qty", (oid,)
    ).fetchall()
    assert rows == [(400, "TDX买入"), (900, "TDX买入")]   # 两笔都有原因
    # 满量后 _sync_orders 同步到终态(56) → 回收 ctx
    assert discarded == [oid]


def test_adopt_unknown_order_is_manual(store, kill):
    """order_id 本地查不到 = 券商客户端手工单: strategy 标"手工",
    audit 记 manual_adopt。"""
    gw = _gw()
    book = Book()
    oid = gw.order(CODE, DIRECTION_BUY, 10.0, 100)
    gw.simulate_fill(oid)
    rec = Reconciler(gw, book, store, kill, retry_interval_sec=0.0)
    result = rec.sync_reports()
    assert result["adopted"] == 1
    assert book.snapshot()["positions"][CODE].strategy == "手工"
    kinds = {r[0] for r in store._conn.execute("SELECT kind FROM audit")}
    assert "manual_adopt" in kinds
    # 2026-07-30: 手工单 source=manual (前端成交记录"手工"标签的数据源)
    assert store._conn.execute(
        "SELECT source FROM trades WHERE order_id=?", (oid,)).fetchone()[0] == "manual"


def test_sync_orders_writes_back_and_never_regresses_terminal(store, kill):
    """委托状态回写: 新单/状态变化落 book+store; 本地终态不被
    QMT 滞后的非终态查询回退 (成交硬事实 > 查询快照)。"""
    gw = _gw()
    book = Book()
    oid = gw.order(CODE, DIRECTION_BUY, 10.0, 100, remark="V0730-B1")
    rec = Reconciler(gw, book, store, kill, retry_interval_sec=0.0)
    # 本地完全没有这张单 (委托回报也丢了) → 补记
    result = rec.sync_reports()
    assert result["orders_updated"] == 1
    assert book.snapshot()["orders"][oid].status == 50
    assert store._conn.execute(
        "SELECT status FROM orders WHERE order_id=?", (oid,)).fetchone()[0] == 50
    # 无变化 → 第二轮不重写
    assert rec.sync_reports()["orders_updated"] == 0
    # 券商侧推进到已撤 → 回写
    gw._orders[oid] = dict(gw._orders[oid], status=54)
    assert rec.sync_reports()["orders_updated"] == 1
    assert book.snapshot()["orders"][oid].status == 54
    # 本地已终态(54), QMT 查询滞后返回已报(50) → 不回退
    gw._orders[oid] = dict(gw._orders[oid], status=50)
    assert rec.sync_reports()["orders_updated"] == 0
    assert book.snapshot()["orders"][oid].status == 54
    assert store._conn.execute(
        "SELECT status FROM orders WHERE order_id=?", (oid,)).fetchone()[0] == 54


def test_sync_reports_is_idempotent(store, kill):
    """重复跑无副作用: traded_id/order_id 幂等, 第二轮全 0。"""
    gw = _gw()
    book = Book()
    oid = gw.order(CODE, DIRECTION_BUY, 10.0, 100)
    gw.simulate_fill(oid)
    rec = Reconciler(gw, book, store, kill, retry_interval_sec=0.0)
    first = rec.sync_reports()
    assert first["adopted"] == 1
    second = rec.sync_reports()
    assert second == {"adopted": 0, "orders_updated": 0}
    assert book.snapshot()["positions"][CODE].volume == 100   # 不双扣


def test_sync_reports_heals_missing_trade_row(store, kill):
    """A3 (2026-08-01 计划书批次1): 实时 _on_trade 已记 book 但首次
    save_trade 失败 (WAL busy 重试仍败) → "book 有 / trades 表缺"
    遗留态; 下一轮 sync_reports 主动补写 trades 行 (QMT 真相源,
    统一兜底网)。只补 trades 行, 不重放 update_order_filled (累加
    语义, _on_trade 已累过), 不重复飞书通知。"""
    gw = _gw()
    book = Book()
    oid = gw.order(CODE, DIRECTION_BUY, 10.0, 100, remark="V0801-A3")
    book.apply_order_update(oid, 50, code=CODE, direction=DIRECTION_BUY,
                            price=10.0, qty=100, remark="V0801-A3")
    store.save_order({"order_id": oid, "remark": "V0801-A3", "code": CODE,
                      "direction": DIRECTION_BUY, "price": 10.0, "qty": 100,
                      "status": 50})
    trade = gw.simulate_fill(oid)   # 网关已成交, 拿到真实 traded_id
    tid = str(trade["traded_id"])
    # 模拟 _on_trade 路径: book 记账成功, save_trade 失败 (不落库),
    # 但 update_order_filled 已成功 (订单进度已推进, 不可再重放)
    book.apply_trade(tid, oid, CODE, DIRECTION_BUY, 10.0, 100)
    store.update_order_filled(oid, 100)
    notified = []
    rec = Reconciler(gw, book, store, kill, retry_interval_sec=0.0,
                     on_adopted_trade=lambda *a: notified.append(a))
    result = rec.sync_reports()
    assert result["adopted"] == 1          # 补写计一笔
    row = store._conn.execute(
        "SELECT code, qty, source, reason FROM trades WHERE traded_id=?",
        (tid,)).fetchone()
    assert row[0] == CODE and row[1] == 100 and row[2] == "system"
    kinds = {r[0] for r in store._conn.execute("SELECT kind FROM audit")}
    assert "trade_db_backfill" in kinds
    assert book.snapshot()["positions"][CODE].volume == 100   # 不双扣
    # update_order_filled 未重放: filled_qty 仍是 100 不是 200
    assert store._conn.execute(
        "SELECT filled_qty FROM orders WHERE order_id=?",
        (oid,)).fetchone()[0] == 100
    assert notified == []                   # 不重复通知
    # 第二轮幂等: 行已补齐, 不再补写
    assert rec.sync_reports()["adopted"] == 0


# ═══════════════════════════════════════════════════════════════
# 2026-08-04 (600127 双记账急停事件): QMT 盘前/跨日查询会返回
# 前一交易日的成交与委托, 同步两腿只认当日数据
# ═══════════════════════════════════════════════════════════════

def test_adopt_skips_cross_day_trades(store, kill):
    """盘前 QMT 返回昨日成交 + known 只装当日 traded_id → 昨日成交被
    当"新手工单"重复认领 (600127 实测 1700→3400 双扣急停)。
    修复: 非当日 ts 的成交一律跳过, 不入账不落库, audit 留痕。"""
    gw = _gw()
    book = Book()
    trade = gw.simulate_external_trade(CODE, DIRECTION_BUY, 5.67, 1700)
    # 成交时间改成昨天 (模拟盘前 QMT 返回昨日数据的剧本)
    gw._trades[trade["traded_id"]]["ts"] = time.time() - 86400
    rec = Reconciler(gw, book, store, kill, retry_interval_sec=0.0)
    result = rec.sync_reports()
    assert result["adopted"] == 0
    assert CODE not in book.snapshot()["positions"]          # 没重复入账
    assert store._conn.execute(
        "SELECT COUNT(*) FROM trades").fetchone()[0] == 0    # 没落库
    kinds = {r[0] for r in store._conn.execute("SELECT kind FROM audit")}
    assert "sync_stale_skipped" in kinds
    assert "manual_adopt" not in kinds


def test_sync_orders_skips_cross_day_orders(store, kill):
    """同日事件另一条腿: 昨日委托回写会把 orders.updated_ts 盖成今天,
    昨日废单混进"当日委托" (api 按 updated_ts 过滤当日)。
    修复: 非当日 ts 的委托一律跳过, 不回写 book/store。"""
    gw = _gw()
    book = Book()
    oid = gw.order(CODE, DIRECTION_SELL, 14.57, 600, remark="V0803-036X")
    gw._orders[oid] = dict(gw._orders[oid], status=56, filled_qty=600,
                           ts=time.time() - 86400)
    rec = Reconciler(gw, book, store, kill, retry_interval_sec=0.0)
    result = rec.sync_reports()
    assert result["orders_updated"] == 0
    assert oid not in book.snapshot()["orders"]
    assert store._conn.execute(
        "SELECT COUNT(*) FROM orders WHERE order_id=?",
        (oid,)).fetchone()[0] == 0
    kinds = {r[0] for r in store._conn.execute("SELECT kind FROM audit")}
    assert "sync_stale_skipped" in kinds


def test_c_side_same_day_eod_snapshot_not_double_counted(store, kill):
    """C 方双算修复: 15:05 EOD 快照已含当日成交, 净额基准从"当日 0 点"
    改为"快照时点"——成交先于快照归档时不再被加第二遍
    (昨: 快照 1200 + 当日买 200 → C=1400, 与 B=1200 假 WARN)。"""
    store.save_trade({"traded_id": "T-today", "order_id": "O-x", "code": CODE,
                      "direction": DIRECTION_BUY, "price": 10.0, "qty": 200,
                      "ts": time.time() - 10})
    # EOD 快照在成交之后归档, 已含这 200 股
    store.save_position_snapshot(
        {CODE: {"volume": 1200, "can_use": 1200, "avg_cost": 10.0}})
    rec = _reconciler(store, kill, _book_with(1200), 1200)
    report = rec.reconcile(now_ts=time.time())
    assert report.level == LEVEL_NONE


# ═══════════════════════════════════════════════════════════════
# 2026-08-07 审计 HIGH#1: 补记卖出也落 pnl (单源)
# ═══════════════════════════════════════════════════════════════

def test_backfill_sell_records_pnl_high1(store, kill):
    """审计 HIGH#1: 成交回调丢失走 sync_reports 补记时, 卖出成交同样
    落 pnl_amount/pnl_pct (与实时 _on_trade 路径、飞书成交卡
    _on_adopted_trade → _sell_pnl 同口径)。修复前 record 字典不含 pnl
    → save_trade 默认 0 → 日报 realized_pnl 在补记场景静默欠算、/deals
    盈亏列空, 且与同笔飞书卡不一致 (违反"单源 book 成本法")。"""
    gw = _gw()
    book = Book()
    # 建仓 100 股 @ 成本 10 (补记路径 pre_avg_cost 取自 book 持仓快照)
    book.apply_trade("T-seed", "O-seed", CODE, DIRECTION_BUY, 10.0, 100)
    # 卖出回调丢失剧本: 网关已成交, book 没见过成交回报
    oid = gw.order(CODE, DIRECTION_SELL, 12.0, 100, remark="V0807-H1X")
    book.apply_order_update(oid, 50, code=CODE, direction=DIRECTION_SELL,
                            price=12.0, qty=100, remark="V0807-H1X")
    store.save_order({"order_id": oid, "remark": "V0807-H1X", "code": CODE,
                      "direction": DIRECTION_SELL, "price": 12.0, "qty": 100,
                      "status": 50})
    gw.simulate_fill(oid)
    rec = Reconciler(gw, book, store, kill, retry_interval_sec=0.0)
    assert rec.sync_reports()["adopted"] == 1
    row = store._conn.execute(
        "SELECT pnl_amount, pnl_pct FROM trades WHERE order_id=?", (oid,)
    ).fetchone()
    assert row[0] == 200.0   # (12 - 10) * 100
    assert row[1] == 20.0    # (12/10 - 1) * 100


def test_sync_orders_placeholder_order_id_not_merged(store, kill):
    """2026-08-27 (159226 手工单时间错乱): QMT 对手机端手工单回报
    order_id="0" 占位, 按 oid upsert 会把多日多笔手工单合并成一条,
    created_ts 停在旧单时间, 委托页显示"旧时间+新价格"四不像。
    修复: oid="0" 时改用当日唯一合成 id, 各笔手工单独立成行,
    created_ts 用本地首见时刻 (而非不可信的柜面占位时间)。"""
    gw = _gw()
    book = Book()
    rec = Reconciler(gw, book, store, kill, retry_interval_sec=0.0)
    today = time.strftime("%Y%m%d")
    # 伪造两笔 order_id="0" 的手工委托 (同日两只票 / 同票同价同量也各自独立)
    with gw._lock:
        gw._orders["0"] = {
            "order_id": "0", "remark": "", "code": "159226.SZ",
            "direction": DIRECTION_SELL, "price": 1.284, "qty": 195300,
            "filled_qty": 195300, "status": 56,
            "ts": time.time(), "price_type": None}
    rec.sync_reports()
    rows = store._conn.execute(
        "SELECT order_id, code, created_ts FROM orders").fetchall()
    assert len(rows) == 1                          # 合成 id 单行, 不复用 "0"
    assert rows[0][0] == f"manual_{today}_159226.SZ_{DIRECTION_SELL}_1.284_195300"
    assert rows[0][1] == "159226.SZ"
    # created_ts 用本地首见时刻 (落在本次运行窗口内), 不是 QMT 占位时间
    assert abs(rows[0][ 2] - time.time()) < 10
