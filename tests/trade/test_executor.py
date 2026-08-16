"""Executor / ClearLock 单元测试 (P1 第二阶段, 2026-07-26).

锁住: 预埋全档位挂出 / 超涨停价档位跳过 / 数量取整与清仓档口径 /
乐观标记 / 风控拒绝不预埋 / 撤单流水线 (锁→撤→ack→刷→卖) /
买一价限价卖出 / 5s 未成交升级逃生通道 (限价) / rebind_order_id 时序 / TTL 兜底。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.book import (
    DIRECTION_BUY,
    DIRECTION_SELL,
    OS_CANCELED,
    PRICE_TYPE_LIMIT,
    PRICE_TYPE_SZ_5LEVEL_CANCEL,
    Book,
)
from trade.config import LadderTpConfig, StopConfig, TradeConfig
from trade.executor import ClearLock, Executor, limit_ratio
from trade.gateway import FakeGateway
from trade.risk import KillSwitch, RiskContext, RiskGate
from trade.store import TradeStore

CODE = "600519.SH"
CYB = "300750.SZ"  # 创业板, 涨停 20%

# 测试默认档位 (聚焦被测逻辑, 不随 TradeConfig 默认值漂移)
_TEST_LADDER = ((0.05, 0.33), (0.10, 0.5), (0.15, 1.0))


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


def _make_executor(store, kill, book, gw, config=None, quotes=None,
                   prev_closes=None, clock=None):
    cfg = config or TradeConfig(
        account_id="TEST",
        stop=StopConfig(ladder_tp=LadderTpConfig(levels=_TEST_LADDER)))
    gate = RiskGate(kill, store, cfg.daily_loss_limit,
                    sizing=cfg.position_sizing)

    def build_ctx():
        return RiskContext(
            reconcile_passed=True, total_asset=1_000_000.0,
            positions=book.snapshot()["positions"],
            day_baseline_equity=1_000_000.0, current_equity=1_000_000.0,
            is_trading_day=True,
        )

    return Executor(
        gw, book, store, gate, cfg, build_ctx,
        get_quote=(quotes or {}).get,
        get_prev_close=(prev_closes or {}).get,
        clock=clock or (lambda: 1000.0),
    )


def _gw_with(volume=1000, can_use=None, cost=10.0, code=CODE):
    gw = FakeGateway(positions={code: {"volume": volume,
                                       "can_use": can_use if can_use is not None else volume,
                                       "avg_cost": cost}})
    gw.connect()
    return gw


# ═══════════════════════════════════════════════════════════════
# 涨停幅度映射
# ═══════════════════════════════════════════════════════════════

def test_limit_ratio_board_mapping():
    assert limit_ratio("600519.SH") == 0.10        # 主板
    assert limit_ratio("000001.SZ") == 0.10
    assert limit_ratio("300750.SZ") == 0.20        # 创业板
    assert limit_ratio("688981.SH") == 0.20        # 科创板
    assert limit_ratio("830799.BJ") == 0.30        # 北交所
    assert limit_ratio("600519.SH", st=True) == 0.05


def test_fill_context_peek_shared_and_discard(store, kill):
    """2026-07-31 (金逸影视 6 笔部成实例): peek 读不删 —— 同一订单
    部成多笔共享同一份 ctx; discard (订单终态) 才清理。"""
    ex = _make_executor(store, kill, Book(), _gw_with())
    ex.register_fill_context("O1", {"label": "TDX买入"})
    assert ex.peek_fill_context("O1") == {"label": "TDX买入"}
    assert ex.peek_fill_context("O1") == {"label": "TDX买入"}   # 读不删
    ex.discard_fill_context("O1")
    assert ex.peek_fill_context("O1") is None
    ex.discard_fill_context("O1")                               # 幂等


# ═══════════════════════════════════════════════════════════════
# 预埋单
# ═══════════════════════════════════════════════════════════════

def test_place_ladder_all_tiers_chuangye(store, kill):
    """创业板 20% 涨停, 三档全挂: 300/500/清仓档卖剩余 200 (合计=整仓)。"""
    book = Book()
    _seed_book(book, CYB, 1000)
    ex = _make_executor(store, kill, book, _gw_with(code=CYB),
                        prev_closes={CYB: 10.0})
    placed = ex.place_ladder("20260726")
    assert len(placed) == 3
    orders = sorted(ex._gw.query_orders(), key=lambda o: o["price"])
    assert [o["price"] for o in orders] == [10.5, 11.0, 11.5]
    assert [o["qty"] for o in orders] == [300, 500, 200]
    # 审计M1修复: remark 的 mmdd 来自发号器时钟 (clock=1000.0 → 1970-01),
    # date_str 只决定 tier 的日期维度, 两个职责别混
    mmdd = time.strftime("%m%d", time.localtime(1000.0))
    assert all(o["remark"].startswith(f"V{mmdd}-") for o in orders)


def test_place_ladder_skips_tier_above_limit_up(store, kill):
    """主板 10% 涨停: 15% 档 (11.5 > 11.0) 跳过, 5%/10% 档照挂。"""
    book = Book()
    _seed_book(book, CODE, 1000)
    ex = _make_executor(store, kill, book, _gw_with(),
                        prev_closes={CODE: 10.0})
    placed = ex.place_ladder("20260726")
    assert len(placed) == 2
    assert [o["price"] for o in ex._gw.query_orders()] == [10.5, 11.0]
    skips = store._conn.execute(
        "SELECT message FROM audit WHERE kind='ladder_skip'").fetchall()
    assert any("超涨停" in m for (m,) in skips)


def test_place_ladder_qty_rounds_to_lot(store, kill):
    """250 股 × 50% = 125 → 100 股整手。"""
    book = Book()
    _seed_book(book, CODE, 250)
    cfg = TradeConfig(account_id="TEST", stop=StopConfig(
        ladder_tp=LadderTpConfig(levels=((0.05, 0.5),))))
    ex = _make_executor(store, kill, book, _gw_with(volume=250),
                        config=cfg, prev_closes={CODE: 10.0})
    ex.place_ladder("20260726")
    assert ex._gw.query_orders()[0]["qty"] == 100


def test_place_ladder_optimistic_tier_mark(store, kill):
    """下单提交成功即标记档位 (落 book + store), 当日重跑不重复挂。"""
    book = Book()
    _seed_book(book, CODE, 1000)
    ex = _make_executor(store, kill, book, _gw_with(),
                        prev_closes={CODE: 10.0})
    ex.place_ladder("20260726")
    assert book.tier_done(CODE, "20260726") == frozenset({0, 1})
    assert store.load_tier_states("20260726")[CODE] == [0, 1]
    assert ex.place_ladder("20260726") == []  # 当日全部已标记, 无新单


def test_place_ladder_day2_not_blocked_by_yesterday(store, kill):
    """审计C1修复: 昨日已标记的档位不阻碍今日预埋 (隔夜重挂是主职责)。"""
    book = Book()
    _seed_book(book, CODE, 1000)
    ex = _make_executor(store, kill, book, _gw_with(),
                        prev_closes={CODE: 10.0})
    day1 = ex.place_ladder("20260726")
    assert len(day1) == 2
    # day2: 昨日标记留痕, 但今日照常全档重挂
    day2 = ex.place_ladder("20260727")
    assert len(day2) == 2
    assert book.tier_done(CODE, "20260726") == frozenset({0, 1})  # 历史留痕
    assert book.tier_done(CODE, "20260727") == frozenset({0, 1})


def test_place_ladder_qty_ratio_rounding(store, kill):
    """审计M2修复: 比例档四舍五入 —— 1000×0.29 = 2.9 手 → 300 股
    (旧 int() 截断只卖 200, 静默少卖)。"""
    book = Book()
    _seed_book(book, CODE, 1000)
    cfg = TradeConfig(account_id="TEST", stop=StopConfig(
        ladder_tp=LadderTpConfig(levels=((0.05, 0.29),))))
    ex = _make_executor(store, kill, book, _gw_with(),
                        config=cfg, prev_closes={CODE: 10.0})
    ex.place_ladder("20260726")
    assert ex._gw.query_orders()[0]["qty"] == 300


def test_place_ladder_price_half_up_rounding(store, kill):
    """审计L10修复: 价格 x.xx5 边界四舍五入 (银行家舍入会差 1 分)。
    成本 10.05 × 1.05 = 10.5525 → 10.55 (0.5525 的第三位 2 不进);
    用 10.045 × 1.05 = 10.54725 → 10.55 验证进位方向。"""
    book = Book()
    _seed_book(book, CODE, 1000, cost=10.045)
    cfg = TradeConfig(account_id="TEST", stop=StopConfig(
        ladder_tp=LadderTpConfig(levels=((0.05, 0.5),))))
    ex = _make_executor(store, kill, book,
                        _gw_with(cost=10.045),
                        config=cfg, prev_closes={CODE: 10.0})
    ex.place_ladder("20260726")
    assert ex._gw.query_orders()[0]["price"] == 10.55


def test_remark_seq_monotonic_no_reset(store, kill):
    """审计M1修复: remark 序号单调递增不重置, 预埋与卖出不重号。"""
    book = Book()
    _seed_book(book, CODE, 1000)
    ex = _make_executor(store, kill, book, _gw_with(),
                        quotes={CODE: {"last": 10.8, "bid1": 10.8, "high": 11.0,
                                       "ts": 1000.0}},
                        prev_closes={CODE: 10.0})
    ex.place_ladder("20260726")
    ex.execute_exit(CODE, "hard_stop")
    remarks = [o["remark"] for o in ex._gw.query_orders()]
    assert len(remarks) == len(set(remarks))  # 全唯一


def test_place_ladder_risk_reject_no_order(store, kill):
    """急停激活 → 风控全拒 → 一张预埋单都不下。"""
    book = Book()
    _seed_book(book, CODE, 1000)
    kill.activate("测试")
    ex = _make_executor(store, kill, book, _gw_with(),
                        prev_closes={CODE: 10.0})
    assert ex.place_ladder("20260726") == []
    assert ex._gw.query_orders() == []


def test_place_ladder_no_prev_close_fail_closed(store, kill):
    """无昨收 → 无法判涨停, 该票本轮不挂 (fail-closed)。"""
    book = Book()
    _seed_book(book, CODE, 1000)
    ex = _make_executor(store, kill, book, _gw_with(), prev_closes={})
    assert ex.place_ladder("20260726") == []


def test_place_ladder_disabled_no_order(store, kill):
    """2026-08-10: 阶梯止盈 enabled=false → 不挂预埋单 (与盘中兜底同步关)。
    函数入口 guard: 返回 [] + 写一条 ladder_skip_disabled audit + 网关零订单。
    覆盖 09:15 定时器 / 手动命令 / 启动补偿三条调用路径的最后一道兜底。"""
    book = Book()
    _seed_book(book, CODE, 1000)
    cfg = TradeConfig(account_id="TEST", stop=StopConfig(
        ladder_tp=LadderTpConfig(enabled=False, levels=_TEST_LADDER)))
    ex = _make_executor(store, kill, book, _gw_with(), config=cfg,
                        prev_closes={CODE: 10.0})
    assert ex.place_ladder("20260810") == []
    assert ex._gw.query_orders() == []
    rows = store._conn.execute(
        "SELECT kind FROM audit WHERE kind='ladder_skip_disabled'").fetchall()
    assert rows == [("ladder_skip_disabled",)]


def test_place_ladder_syncs_can_use_before_place(store, kill):
    """2026-08-06 002155.SZ 事件: 昨日尾盘买入 book.can_use=0 (T+1),
    预埋 (09:15) 早于首次对账 (09:35) —— 预埋前须先从 QMT 全量刷新,
    否则 t1_sellable 闸误拒全部预埋档, 阶梯止盈退化为监控兜底。"""
    book = Book()
    _seed_book(book, CODE, 1000, can_use=0)          # 本地账本: T+1 不可卖
    gw = _gw_with(volume=1000, can_use=1000)          # 券商端: 隔夜后已可卖
    ex = _make_executor(store, kill, book, gw, prev_closes={CODE: 10.0})
    placed = ex.place_ladder("20260806")
    assert len(placed) == 2                           # 主板 15% 档超涨停跳过
    assert book.snapshot()["positions"][CODE].can_use == 1000


def test_place_ladder_sync_fail_falls_back_to_stale(store, kill):
    """预埋前刷新失败 (断线) → fail-closed: 留痕 + 沿用本地旧值,
    本地 can_use=0 时风控拒挂 (宁可漏挂不可错挂)。"""
    book = Book()
    _seed_book(book, CODE, 1000, can_use=0)
    gw = _gw_with(volume=1000, can_use=1000)

    def _boom():
        raise RuntimeError("断线")

    gw.query_positions = _boom
    ex = _make_executor(store, kill, book, gw, prev_closes={CODE: 10.0})
    assert ex.place_ladder("20260806") == []
    assert gw.query_orders() == []
    kinds = [r[0] for r in store._conn.execute(
        "SELECT kind FROM audit WHERE kind='ladder_sync_fail'")]
    assert kinds == ["ladder_sync_fail"]


# ═══════════════════════════════════════════════════════════════
# 撤单流水线
# ═══════════════════════════════════════════════════════════════

def test_execute_exit_pipeline(store, kill):
    """锁→撤→刷→买一价限价卖: 预埋单全撤, 新卖单价=买一。"""
    book = Book()
    _seed_book(book, CODE, 1000)
    ex = _make_executor(store, kill, book, _gw_with(),
                        quotes={CODE: {"last": 10.8, "bid1": 10.8, "high": 11.0, "ts": 1000.0}},
                        prev_closes={CODE: 10.0})
    ex.place_ladder("20260726")
    assert ex.execute_exit(CODE, "hard_stop: 测试")

    orders = ex._gw.query_orders()
    canceled = [o for o in orders if o["status"] == OS_CANCELED]
    sells = [o for o in orders if o["remark"].endswith("X")]
    assert len(canceled) == 2              # 两张预埋单全撤
    assert len(sells) == 1
    assert sells[0]["price"] == 10.8       # 买一价
    assert sells[0]["qty"] == 1000
    assert ex.lock.is_held(CODE)           # 锁留到成交
    kinds = {r[0] for r in store._conn.execute(
        "SELECT kind FROM audit").fetchall()}
    assert {"exit_cancel", "exit_sell"} <= kinds


def test_execute_exit_lock_blocks_reentry(store, kill):
    """清仓锁占用中再次触发 → 拒绝重入 (防双卖)。"""
    book = Book()
    _seed_book(book, CODE, 1000)
    ex = _make_executor(store, kill, book, _gw_with(),
                        quotes={CODE: {"last": 10.8, "bid1": 10.8, "high": 11.0, "ts": 1000.0}},
                        prev_closes={CODE: 10.0})
    assert ex.execute_exit(CODE, "hard_stop")
    assert not ex.execute_exit(CODE, "trailing")   # 锁占用, 拒
    sells = [o for o in ex._gw.query_orders() if o["remark"].endswith("X")]
    assert len(sells) == 1


def test_pending_fill_releases_lock(store, kill):
    """卖单成交 → pending_check 清登记 + 放锁。"""
    book = Book()
    _seed_book(book, CODE, 1000)
    ex = _make_executor(store, kill, book, _gw_with(),
                        quotes={CODE: {"last": 10.8, "bid1": 10.8, "high": 11.0, "ts": 1000.0}},
                        prev_closes={CODE: 10.0})
    ex.execute_exit(CODE, "hard_stop")
    sell_oid = ex._pending[CODE]["order_id"]
    ex._gw.simulate_fill(sell_oid)
    ex.pending_check(now_ts=1001.0)
    assert CODE not in ex._pending
    assert not ex.lock.is_held(CODE)


def test_pending_timeout_escalates_to_cage_limit(store, kill):
    """5s 未成交 → 撤限价单, 沪市盘中升级笼内最凶限价 (买一×98%)。
    2026-08-13: 沪市市价类报单被柜台禁用 (63596), 原对手最优分支移除。"""
    book = Book()
    _seed_book(book, CODE, 1000)
    ex = _make_executor(store, kill, book, _gw_with(),
                        quotes={CODE: {"last": 10.8, "bid1": 10.8, "high": 11.0, "ts": 1000.0}},
                        prev_closes={CODE: 10.0})
    ex.execute_exit(CODE, "hard_stop")
    first_oid = ex._pending[CODE]["order_id"]
    ex.pending_check(now_ts=1006.5)          # >5s
    orders = {o["order_id"]: o for o in ex._gw.query_orders()}
    assert orders[first_oid]["status"] == OS_CANCELED
    new_oid = ex._pending[CODE]["order_id"]
    assert new_oid != first_oid
    assert orders[new_oid]["price_type"] == PRICE_TYPE_LIMIT
    assert orders[new_oid]["price"] == 10.58  # 笼内最凶限价 10.8×0.98
    # audit 也留了升级痕迹
    kinds = {r[0] for r in store._conn.execute(
        "SELECT kind FROM audit").fetchall()}
    assert "exit_escalate" in kinds and "exit_sell_market" in kinds


def test_pending_force_market_after(store, kill):
    """≥14:57 未成交不等 5s, 直接升级 (沪市 → 限价@跌停)。"""
    book = Book()
    _seed_book(book, CODE, 1000)
    ex = _make_executor(store, kill, book, _gw_with(),
                        quotes={CODE: {"last": 10.8, "bid1": 10.8, "high": 11.0, "ts": 1000.0}},
                        prev_closes={CODE: 10.0})
    ex.execute_exit(CODE, "hard_stop")
    ex.pending_check(now_ts=1001.0, now_hhmm="14:58")
    kinds = {r[0] for r in store._conn.execute(
        "SELECT kind FROM audit").fetchall()}
    assert "exit_escalate" in kinds
    orders = {o["order_id"]: o for o in ex._gw.query_orders()}
    new_oid = ex._pending[CODE]["order_id"]
    assert orders[new_oid]["price_type"] == PRICE_TYPE_LIMIT
    assert orders[new_oid]["price"] == 9.0     # 跌停价 10×0.9


def test_pending_escalation_sz_uses_limit_down(store, kill):
    """2026-07-27 实测修复: 深市逃生通道 → 限价@跌停价 (收盘竞价
    只收限价单, 市价必废); 沪市同口径限价@跌停 (2026-08-12 起,
    市价类被柜台禁用)。"""
    SZ = "000001.SZ"
    book = Book()
    _seed_book(book, SZ, 1000)
    gw = FakeGateway(positions={SZ: {"volume": 1000, "can_use": 1000,
                                     "avg_cost": 10.0}})
    gw.connect()
    ex = _make_executor(store, kill, book, gw,
                        quotes={SZ: {"last": 10.8, "bid1": 10.8, "high": 11.0, "ts": 1000.0}},
                        prev_closes={SZ: 10.0})
    ex.execute_exit(SZ, "hard_stop")
    ex.pending_check(now_ts=1001.0, now_hhmm="14:58")
    orders = {o["order_id"]: o for o in gw.query_orders()}
    new_oid = ex._pending[SZ]["order_id"]
    assert orders[new_oid]["price_type"] == 11      # LIMIT 非市价
    assert orders[new_oid]["price"] == 9.0          # 跌停价 10×0.9
    # 沪市对照: 同流程 → 同口径限价@跌停 (市价类被柜台禁用 63596)
    book2 = Book()
    _seed_book(book2, CODE, 1000)
    ex2 = _make_executor(store, kill, book2, _gw_with(),
                         quotes={CODE: {"last": 10.8, "bid1": 10.8,
                                        "high": 11.0, "ts": 1000.0}},
                         prev_closes={CODE: 10.0})
    ex2.execute_exit(CODE, "hard_stop")
    ex2.pending_check(now_ts=1001.0, now_hhmm="14:58")
    sh_orders = {o["order_id"]: o for o in ex2._gw.query_orders()}
    sh_new = sh_orders[ex2._pending[CODE]["order_id"]]
    assert sh_new["price_type"] == PRICE_TYPE_LIMIT
    assert sh_new["price"] == 9.0              # 跌停价 10×0.9


def test_pending_timeout_sz_uses_5level_cancel(store, kill):
    """2026-08-11 002253 + 2026-08-12 复盘: 盘中 5s 超时升级时, 深市不得
    走跌停价 (撞价格笼子 88009 废单, 历史深市升级单全废), 也不走对手最优
    (单档 FOK 盘口量不够整单撤) —— 改走五档即成剩余撤销 (扫买1-买5 IOC,
    成交概率最高)。跌停价逃生通道只在尾盘 force (收盘集合竞价 14:57+) 时用。
    注: SZ_5LEVEL_CANCEL 实盘未实测, 待小单验证。"""
    SZ = "000001.SZ"
    book = Book()
    _seed_book(book, SZ, 1000)
    gw = FakeGateway(positions={SZ: {"volume": 1000, "can_use": 1000,
                                     "avg_cost": 10.0}})
    gw.connect()
    ex = _make_executor(store, kill, book, gw,
                        quotes={SZ: {"last": 10.8, "bid1": 10.8, "high": 11.0, "ts": 1000.0}},
                        prev_closes={SZ: 10.0})
    ex.execute_exit(SZ, "hard_stop")              # 第一笔挂买一价 10.8, ts=1000
    ex.pending_check(now_ts=1006.5)               # 盘中 (无 now_hhmm→非 force) 超时 6.5s
    orders = {o["order_id"]: o for o in gw.query_orders()}
    new_oid = ex._pending[SZ]["order_id"]
    assert orders[new_oid]["price_type"] == PRICE_TYPE_SZ_5LEVEL_CANCEL
    assert orders[new_oid]["price"] == 0.0


def test_in_flight_sells_for_reconciler(store, kill):
    book = Book()
    _seed_book(book, CODE, 1000)
    ex = _make_executor(store, kill, book, _gw_with(),
                        quotes={CODE: {"last": 10.8, "bid1": 10.8, "high": 11.0, "ts": 1000.0}},
                        prev_closes={CODE: 10.0})
    ex.execute_exit(CODE, "hard_stop")
    assert ex.in_flight_sells() == {CODE: 1000}


def test_execute_exit_stale_quote_fail_closed(store, kill):
    """审计M6修复: 买一价陈旧 (>60s) 与无价同等 fail-closed。"""
    book = Book()
    _seed_book(book, CODE, 1000)
    ex = _make_executor(
        store, kill, book, _gw_with(),
        quotes={CODE: {"last": 10.8, "bid1": 10.8, "high": 11.0,
                       "ts": 1000.0 - 120}})  # 快照是 120s 前的
    assert not ex.execute_exit(CODE, "hard_stop")
    assert not [o for o in ex._gw.query_orders() if o["remark"].endswith("X")]
    rows = store._conn.execute(
        "SELECT message FROM audit WHERE kind='exit_fail_closed'").fetchall()
    assert any("陈旧" in m for (m,) in rows)


def test_execute_exit_no_ts_fail_closed(store, kill):
    """2026-08-16 fail-closed 修复: 买一价无 ts 键 (旧实现判"不陈旧"继续卖,
    fail-open) 现与无价/陈旧同等 fail-closed —— 宁可不卖, 不可瞎卖。
    对齐 monitor/rotation 的"无 ts 判陈旧"口径 (单一真相源 quote_stale)。"""
    book = Book()
    _seed_book(book, CODE, 1000)
    ex = _make_executor(
        store, kill, book, _gw_with(),
        quotes={CODE: {"last": 10.8, "bid1": 10.8, "high": 11.0}})  # 无 ts 键
    assert not ex.execute_exit(CODE, "hard_stop")
    assert not [o for o in ex._gw.query_orders() if o["remark"].endswith("X")]
    rows = store._conn.execute(
        "SELECT message FROM audit WHERE kind='exit_fail_closed'").fetchall()
    assert any("无时间戳" in m for (m,) in rows)


def test_delayed_cancel_waits_ack_then_proceeds(store, kill):
    """审计M8修复: 受理≠撤成 —— delayed 模式下等 ack 超时告警,
    流水线不阻塞继续卖; ack 到达后订单落终态。"""
    book = Book()
    _seed_book(book, CODE, 1000)
    gw = _gw_with()
    gw._delayed_cancel = True
    ex = _make_executor(store, kill, book, gw,
                        quotes={CODE: {"last": 10.8, "bid1": 10.8, "high": 11.0, "ts": 1000.0}},
                        prev_closes={CODE: 10.0})
    ex._ack_timeout = 0.3          # 测试不等真 2s
    ex.place_ladder("20260726")
    ladder_oid = ex._gw.query_orders()[0]["order_id"]
    assert ex.execute_exit(CODE, "hard_stop")
    # 撤单 ack 超时告警已写, 但卖出单照挂 (逃生优先)
    rows = store._conn.execute(
        "SELECT kind FROM audit WHERE kind='exit_cancel_timeout'").fetchall()
    assert rows
    sells = [o for o in gw.query_orders() if o["remark"].endswith("X")]
    assert len(sells) == 1
    # ack 到达 → 预埋单落终态
    gw.simulate_cancel_ack(ladder_oid)
    final = {o["order_id"]: o["status"] for o in gw.query_orders()}
    assert final[ladder_oid] == OS_CANCELED


# ═══════════════════════════════════════════════════════════════
# ClearLock 单元
# ═══════════════════════════════════════════════════════════════

def test_lock_rebind_release_by_order_id():
    """先锁后拿 order_id 的时序: rebind 后可按 order_id 反查释放。"""
    lock = ClearLock("test", "ACC", clock=lambda: 100.0)
    assert lock.acquire(CODE)
    assert not lock.acquire(CODE)          # 占用中
    lock.rebind_order_id(CODE, "O-1")      # 下单拿到 id 后补挂
    lock.release_by_order_id("O-1")
    assert lock.acquire(CODE)              # 已释放


def test_lock_ttl_fallback():
    """持锁方崩溃不锁死: TTL 过期后夺锁。"""
    t = [1000.0]
    lock = ClearLock("test", "ACC", ttl_sec=300, clock=lambda: t[0])
    assert lock.acquire(CODE)
    t[0] += 299
    assert not lock.acquire(CODE)          # TTL 内
    t[0] += 2
    assert lock.acquire(CODE)              # TTL 过期夺锁


def test_lock_rebind_clears_old_order_mapping():
    """审计L9修复: 换单 rebind 清旧 order_id 映射 —— 旧单终态不得误放新锁。"""
    lock = ClearLock("test", "ACC", clock=lambda: 100.0)
    assert lock.acquire(CODE)
    lock.rebind_order_id(CODE, "O-old")
    lock.rebind_order_id(CODE, "O-new")      # 换单重挂
    lock.release_by_order_id("O-old")        # 旧映射已清, 不应放锁
    assert not lock.acquire(CODE)
    lock.release_by_order_id("O-new")        # 新映射才有效
    assert lock.acquire(CODE)


# ═══════════════════════════════════════════════════════════════
# 2026-08-01 P0-3 (H1): _on_pending_died — 废单/已撤且持仓在 → 回调
# ═══════════════════════════════════════════════════════════════

class TestPendingDied:
    """pending_check 终态为非成功且持仓仍在时, 应回调 on_pending_died(code)"""

    @pytest.fixture
    def gw(self):
        gw = FakeGateway(positions={CODE: {"volume": 1000, "can_use": 1000,
                                           "avg_cost": 10.0}})
        gw.connect()
        return gw

    @pytest.fixture
    def died_calls(self):
        return []

    def _make(self, store, kill, gw, died_calls, **kw):
        bk = Book()
        _seed_book(bk, CODE, 1000, 10.0, 1000)
        ex = _make_executor(store, kill, bk, gw, **kw)
        ex._on_pending_died = lambda code: died_calls.append(code)
        return ex, bk

    def test_cancelled_with_position_fires(self, store, kill, gw, died_calls):
        """已撤 + 持仓仍在 → 回调解除 _triggered"""
        ex, bk = self._make(store, kill, gw, died_calls)
        ex._sell(CODE, 100, 11.0, 11, "test", "test_audit")
        oid = list(gw._orders)[0]
        gw.simulate_cancel_ack(oid)
        ex.pending_check(now_ts=1005.0)
        assert CODE in died_calls

    def test_junk_with_position_fires(self, store, kill, gw, died_calls):
        """废单 + 持仓仍在 → 回调解除 _triggered"""
        ex, bk = self._make(store, kill, gw, died_calls)
        ex._sell(CODE, 100, 11.0, 11, "test", "test_audit")
        oid = list(gw._orders)[0]
        gw.simulate_reject(oid)
        ex.pending_check(now_ts=1005.0)
        assert CODE in died_calls

    def test_filled_does_not_fire(self, store, kill, gw, died_calls):
        """已成不解除: 持仓已清, re-trigger 无意义"""
        ex, bk = self._make(store, kill, gw, died_calls)
        ex._sell(CODE, 100, 11.0, 11, "test", "test_audit")
        oid = list(gw._orders)[0]
        gw.simulate_fill(oid, 11.0)
        ex.pending_check(now_ts=1005.0)
        assert CODE not in died_calls

    def test_dead_without_position_noop(self, store, kill, gw, died_calls):
        """已撤 + 零持仓 → 不回调 (持仓已清, re-trigger 无标的)"""
        ex, bk = self._make(store, kill, gw, died_calls)
        ex._sell(CODE, 100, 11.0, 11, "test", "test_audit")
        oid = list(gw._orders)[0]
        # 模拟全部卖出清仓 (用 SELL 方向 + 新 traded_id 防幂等拦截)
        bk.apply_trade("T-clear", "O-clear", CODE, DIRECTION_SELL, 10.0, 1000)
        gw.simulate_cancel_ack(oid)
        ex.pending_check(now_ts=1005.0)
        assert CODE not in died_calls
