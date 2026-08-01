"""端到端: FakeGateway 驱动全日流程 (P1 第三阶段, 2026-07-26).

不依赖 QMT。覆盖任务书 8 步: 组装启动对账 → 预埋 (超涨停跳过 +
remark 格式) → 第一档成交 (JSONL 先落盘 / book 递减 / tier 标记 /
store 一致) → 移动止盈触发撤单流水线 + 5s 升级逃生通道 (深市限价@跌停)
→ 对账偏差
急停 + 买入被拒 → 人工 unkill 恢复 → EOD 归档 → 重启恢复档位与持仓。
附: 断线重连 (退避→重订阅→全量对账) 独立用例。
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.config import LadderTpConfig, StopConfig, TradeConfig
from trade.events import EVENT_EOD, EVENT_RECONCILE, EVENT_TIMER_SCAN, Event
from trade_main import TradeApp

CYB = "300750.SZ"   # 创业板 (涨停 20%, 三档全挂)
SH = "600519.SH"    # 主板 (涨停 10%, 15% 档跳过)

# e2e 档位固定 (聚焦流程, 不随 TradeConfig 默认值漂移)
_E2E_LADDER = ((0.05, 0.33), (0.10, 0.5), (0.15, 1.0))


def _wait(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture()
def clock():
    # 时段感知后 (2026-07-27 裁决①②): 自动规则/心跳只在连续竞价跑,
    # 假时钟必须锚在"最近一个工作日 10:00"; tick 事件 ts (真实
    # time.time) 与假时钟偏差 >30s 会被当过旧丢弃, 所以锚当天的工作日
    from datetime import datetime, timedelta
    d = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return [d.timestamp()]


@pytest.fixture()
def cfg(tmp_path):
    return TradeConfig(
        account_id="E2E", fake_sdk=True,
        db_path=str(tmp_path / "trade.db"),
        raw_log_path=str(tmp_path / "raw.jsonl"),
        kill_flag_path=str(tmp_path / "KILL"),
        stop=StopConfig(ladder_tp=LadderTpConfig(levels=_E2E_LADDER)),
    )


def _today(clock) -> str:
    return time.strftime("%Y%m%d", time.localtime(clock[0]))


def _positions_seed():
    return {
        CYB: {"volume": 1000, "can_use": 1000, "avg_cost": 10.0},
        SH: {"volume": 1000, "can_use": 1000, "avg_cost": 10.0},
    }


@pytest.fixture()
def app(cfg, clock):
    a = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                 fake_gateway_kwargs={
                     "cash": 1_000_000.0, "positions": _positions_seed()})
    yield a
    a.stop()


def _put_scan(app, hhmm="10:30"):
    app._engine.put(Event(type=EVENT_TIMER_SCAN, data={"hhmm": hhmm}))


def test_e2e_full_day(app, cfg, clock):
    gw = app.gateway

    # ── 1. 启动: connect → 冷启动灌仓 → 启动对账通过 ──
    assert app.start(start_timers=False)
    assert app.reconciled
    snap = app.book.snapshot()["positions"]
    assert snap[CYB].volume == 1000 and snap[SH].volume == 1000

    # 灌入行情 (昨收是预埋单涨停判定的上游)
    gw.push_quote(CYB, {"last": 10.2, "bid1": 10.2, "high": 10.3, "prev_close": 10.0})
    gw.push_quote(SH, {"last": 10.2, "bid1": 10.2, "high": 10.3, "prev_close": 10.0})
    assert _wait(lambda: app.monitor.quote_of(SH) is not None)

    # ── 2. 预埋: 创业板三档全挂, 主板 15% 档超涨停跳过, remark 格式 ──
    today = _today(clock)
    app.submit_command({"action": "place_ladder", "date_str": today})
    assert _wait(lambda: len(gw.query_orders()) >= 5)
    orders = gw.query_orders()
    cyb_orders = sorted([o for o in orders if o["code"] == CYB],
                        key=lambda o: o["price"])
    sh_orders = [o for o in orders if o["code"] == SH]
    assert [o["price"] for o in cyb_orders] == [10.5, 11.0, 11.5]
    assert [o["qty"] for o in cyb_orders] == [300, 500, 200]
    assert len(sh_orders) == 2                      # 11.5 > 涨停 11.0 跳过
    mmdd = time.strftime("%m%d", time.localtime(clock[0]))
    assert all(o["remark"].startswith(f"V{mmdd}-") for o in orders)
    assert all(len(o["remark"]) <= 24 for o in orders)
    # 档 2 (11.5 > 涨停 11.0) 跳过写在 SH 循环末尾, 在 5 张订单之后,
    # 订单数齐全不代表 skip 已落 audit —— 等它, 不赌时序
    assert _wait(lambda: any(
        "超涨停" in r[0] for r in app.store._conn.execute(
            "SELECT message FROM audit WHERE kind='ladder_skip'").fetchall()))

    # ── 3. 第一档成交: JSONL 先落盘 + book 递减 + tier 标记 + store 一致 ──
    tier0 = cyb_orders[0]
    trade = gw.simulate_fill(tier0["order_id"])
    # append_raw 在回调里同步发生 (先落盘再入队), 不等消费者即可见
    raw_lines = Path(cfg.raw_log_path).read_text(encoding="utf-8").splitlines()
    kinds = [json.loads(l)["kind"] for l in raw_lines]
    assert "trade_fill" in kinds and "order_update" in kinds

    assert _wait(lambda: app.book.snapshot()["positions"][CYB].volume == 700)
    assert app.book.tier_done(CYB, today) == frozenset({0, 1, 2})   # 乐观标记
    assert app.store.load_tier_states(today)[CYB] == [0, 1, 2]
    row = app.store._conn.execute(
        "SELECT qty, price FROM trades WHERE traded_id=?",
        (trade["traded_id"],)).fetchone()
    assert row == (300, 10.5)

    # ── 4. 回撤触发移动止盈 → 撤单流水线 → 买一价限价卖 ──
    gw.push_quote(CYB, {"last": 11.9, "bid1": 11.9, "high": 12.0})
    gw.push_quote(CYB, {"last": 11.8, "bid1": 11.8, "high": 11.8})  # 破 12×0.99
    _put_scan(app)
    assert _wait(lambda: CYB in app.executor._pending)
    sell = [o for o in gw.query_orders()
            if o["code"] == CYB and o["remark"].endswith("X")]
    assert len(sell) == 1 and sell[0]["price"] == 11.8 and sell[0]["qty"] == 700
    # 剩余两张预埋单已撤 (tier0 已成不在撤销范围)
    canceled = [o for o in gw.query_orders()
                if o["code"] == CYB and o["status"] == 54]
    assert len(canceled) == 2
    # ── 5. 5s 未成交 → 升级逃生通道: 深市限价@跌停价 ──
    # 2026-07-31: monitor 缓存昨收后, 深市逃生通道按设计走限价@跌停
    # (prev_close 10.0 × (1-20%) = 8.0); 此前 tick 丢昨收才落对手最优
    clock[0] += 6
    _put_scan(app, "10:31")
    assert _wait(lambda: any(
        o["code"] == CYB and o["remark"].endswith("X") and o["price"] == 8.0
        for o in gw.query_orders()))
    market = [o for o in gw.query_orders()
              if o["code"] == CYB and o["remark"].endswith("X")
              and o["price"] == 8.0][0]
    # 升级单成交, 清空在途 (为对账 CRITICAL 让路: 在途差异会降级 WARN)
    gw.simulate_fill(market["order_id"])
    assert _wait(lambda: app.book.snapshot()["positions"][CYB].volume == 0)
    _put_scan(app, "10:32")
    assert _wait(lambda: CYB not in app.executor._pending)

    # ── 6. 对账偏差 → 急停; 后续买入命令被拒 ──
    gw._positions[SH]["volume"] = 100      # QMT 侧莫名少了 900 股
    app._engine.put(Event(type=EVENT_RECONCILE, data={}))
    assert _wait(lambda: app.kill.is_active())
    app.submit_command({"action": "manual_buy", "code": SH, "qty": 100, "price": 10.0})
    assert _wait(lambda: app.store._conn.execute(
        "SELECT COUNT(*) FROM audit WHERE kind='risk_reject'").fetchone()[0] > 0)
    assert not [o for o in gw.query_orders() if o["direction"] == 23]  # 无一买单

    # ── 7. 人工核对修正数据 → unkill → 恢复 ──
    gw._positions[SH]["volume"] = 1000     # 人工核实: 数据已恢复一致
    app.submit_command({"action": "kill_off"})
    assert _wait(lambda: not app.kill.is_active() and app.reconciled)

    # ── 8. EOD 归档 → 重启恢复 ──
    app._engine.put(Event(type=EVENT_EOD, data={}))
    assert _wait(lambda: app.store.load_position_snapshot())
    snap_row = app.store.load_position_snapshot()
    assert snap_row[SH]["volume"] == 1000 and CYB not in snap_row
    app.stop()

    # 进程"重启": 同一份 db, 券商端 (FakeGateway) 持仓如昨
    app2 = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                    fake_gateway_kwargs={
                        "cash": 1_000_000.0,
                        "positions": {SH: {"volume": 1000, "can_use": 1000,
                                           "avg_cost": 10.0}}})
    try:
        assert app2.start(start_timers=False)
        assert app2.book.snapshot()["positions"][SH].volume == 1000
        # 档位状态从 store 恢复当日标记 (昨日乐观标记不丢也不串日)
        assert app2.book.tier_done(SH, _today(clock)) == frozenset({0, 1})
    finally:
        app2.stop()


def test_disconnect_reconnect_recovers(app):
    """断线 → (Fake 立即) 重连 → 重新订阅 → 全量对账, audit 留痕。"""
    assert app.start(start_timers=False)
    app.gateway.simulate_disconnect("e2e 模拟断线")
    assert _wait(lambda: app.connected)
    assert _wait(lambda: app.store._conn.execute(
        "SELECT COUNT(*) FROM audit WHERE kind='reconnect'").fetchone()[0] > 0)


# ═══════════════════════════════════════════════════════════════
# 审计M5修复: 定时任务启动补偿
# ═══════════════════════════════════════════════════════════════

def _clock_at(hour: int, minute: int) -> list:
    """今日某时刻的时钟 (审计M4 约束: 必须贴真实日期)。"""
    from datetime import datetime
    return [datetime.now().replace(
        hour=hour, minute=minute, second=0, microsecond=0).timestamp()]


def _clock_last_trading_day(hour: int, minute: int) -> list:
    """最近交易日某时刻的时钟 (D5, 2026-08-01: 风险闸"非交易日禁止
    卖出"生效后, 预埋卖出场景只在交易日有意义; 尽量贴真实日期,
    仅向前回退到最近交易日)。"""
    from datetime import datetime, timedelta
    from trade.monitor import is_trading_day_cached
    d = datetime.now().replace(hour=hour, minute=minute,
                               second=0, microsecond=0)
    while not is_trading_day_cached(d.date()):
        d -= timedelta(days=1)
    return [d.timestamp()]


def test_startup_catchup_ladder_after_0925(cfg):
    """09:25 后启动且当日未预埋 → 自动补偿预埋 (audit 留痕)。"""
    clock = _clock_last_trading_day(10, 0)
    app = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                   fake_gateway_kwargs={
                       "cash": 1_000_000.0,
                       "positions": {SH: {"volume": 1000, "can_use": 1000,
                                          "avg_cost": 10.0}}})
    try:
        # 昨收经轮询兜底通道注入 (engine 未启动时 monitor 缓存还空)
        app.gateway.push_quote(SH, {"last": 10.2, "bid1": 10.2,
                                    "high": 10.3, "prev_close": 10.0})
        assert app.start(start_timers=False)
        rows = app.store._conn.execute(
            "SELECT kind FROM audit WHERE kind='ladder_catchup'").fetchall()
        assert rows
        ladder = [o for o in app.gateway.query_orders()
                  if o["remark"].startswith("V")]
        assert len(ladder) == 2        # 主板 15% 档超涨停, 挂 2 张
    finally:
        app.stop()


def test_startup_catchup_eod_after_1505(cfg):
    """15:05 后启动且当日无 EOD 快照 → 补 EOD 归档 (对账 C 方次日基准)。"""
    clock = _clock_at(15, 10)
    app = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                   fake_gateway_kwargs={
                       "cash": 1_000_000.0,
                       "positions": {SH: {"volume": 1000, "can_use": 1000,
                                          "avg_cost": 10.0}}})
    try:
        assert app.start(start_timers=False)
        rows = app.store._conn.execute(
            "SELECT kind FROM audit WHERE kind='eod_catchup'").fetchall()
        assert rows
        assert app.store.load_position_snapshot()[SH]["volume"] == 1000
    finally:
        app.stop()


def test_startup_no_catchup_before_0925(cfg):
    """09:25 前启动: 不补偿 (预埋是定时器的活, 轮不到启动兜底)。"""
    clock = _clock_at(9, 0)
    app = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                   fake_gateway_kwargs={
                       "cash": 1_000_000.0,
                       "positions": {SH: {"volume": 1000, "can_use": 1000,
                                          "avg_cost": 10.0}}})
    try:
        assert app.start(start_timers=False)
        assert app.gateway.query_orders() == []
        rows = app.store._conn.execute(
            "SELECT kind FROM audit WHERE kind='ladder_catchup'").fetchall()
        assert not rows
    finally:
        app.stop()


# ═══════════════════════════════════════════════════════════════
# P0-③ 实测修复: 心跳是断线检测主触发源 (on_disconnected 不可靠)
# ═══════════════════════════════════════════════════════════════

def test_heartbeat_disconnect_triggers_reconnect(app, clock):
    """连续 2 次扫描不健康 (盘中断流) → 触发重连路径。
    on_disconnected 在"关闭客户端"场景不触发 (P0-③ 9 分钟零事件),
    这条心跳链路才是主触发源。"""
    assert app.start(start_timers=False)
    # 先有过 tick (盘中), 然后断流: P0-③ 真实场景是推送停 + 查询也空
    # (只停推送而轮询兜底有数, 是"订阅断交易在", 不该判断线)
    app.gateway.push_quote(SH, {"last": 10.2, "bid1": 10.2, "high": 10.3})
    assert _wait(lambda: app.monitor.has_tick())
    clock[0] += 100                      # 远超 15s 心跳阈值
    app.gateway._quotes.clear()          # 轮询兜底也拿不到数据 = 真断线
    app._on_scan({"hhmm": "10:00"})      # 第 1 次不健康: 只计数, 不重连
    assert not app.store._conn.execute(
        "SELECT kind FROM audit WHERE kind='disconnect'").fetchall()
    app._on_scan({"hhmm": "10:01"})      # 第 2 次: 触发重连路径
    rows = app.store._conn.execute(
        "SELECT message FROM audit WHERE kind='disconnect'").fetchall()
    assert rows and "心跳" in rows[0][0]
    assert app.connected                 # Fake 重连即成功
    assert app.store._conn.execute(
        "SELECT kind FROM audit WHERE kind='reconnect'").fetchall()


def test_no_reconnect_before_first_tick(app, clock):
    """从未收到 tick 的"不健康"是盘前静默, 不触发重连 (has_tick 守卫)。"""
    assert app.start(start_timers=False)
    clock[0] += 100
    for _ in range(3):
        app._on_scan({"hhmm": "09:00"})
    assert not app.store._conn.execute(
        "SELECT kind FROM audit WHERE kind='disconnect'").fetchall()
    assert app.connected


def test_reconcile_unknown_keeps_reconciled(app):
    """P0-③: 断线空查询 → UNKNOWN, reconciled 维持现状不急停。"""
    assert app.start(start_timers=False)
    assert app.reconciled
    app.reconciler._retry_interval = 0.0   # 测试不等真实重试间隔
    app.gateway._positions.clear()         # 模拟断线: 查询静默返回空
    app._on_reconcile()
    assert app.reconciled                  # 维持 True, 不置 False
    assert not app.kill.is_active()


# ═══════════════════════════════════════════════════════════════
# 2026-07-31 方案C: 重连挪出消费者线程 (修 while True 死锁)
# ═══════════════════════════════════════════════════════════════

def test_reconnect_failure_does_not_deadlock_consumer(app):
    """connect 持续失败时 _on_connection_lost 不再 while True 死锁消费者:
    失败写 reconnect_failed audit, 消费者线程继续处理后续事件 (队列归零)。"""
    assert app.start(start_timers=False)

    def boom():
        raise OSError("QMT down")
    app.gateway.connect = bomb = boom
    app.gateway.simulate_disconnect("test 连不上")
    # _on_connection_lost → 立即 _try_reconnect 一次失败 → reconnect_failed audit
    assert _wait(lambda: app.store._conn.execute(
        "SELECT COUNT(*) FROM audit WHERE kind='reconnect_failed'").fetchone()[0] >= 1)
    # 关键: 消费者未死锁 —— 后续 timer_scan 能被消费 (队列归零) + 线程存活
    app._engine.put(Event(type=EVENT_TIMER_SCAN, data={"hhmm": "10:00"}))
    assert _wait(lambda: app._engine._queue.empty())
    assert app._engine._thread is not None and app._engine._thread.is_alive()
    assert not app.connected               # 重连未成功, 仍断线
    del bomb                               # 静态分析保活, 无实义


def test_reconnect_recovery_via_scan(app, clock):
    """H2 (审计补测): connect 先失败 → timer_scan 接力 → connect 恢复 → 重连成功。
    回归方案C 的核心恢复契约 —— 现有测要么立即成功要么持续失败, 恰好绕过接力。"""
    assert app.start(start_timers=False)
    fails = {"n": 0}

    def maybe_fail():
        fails["n"] += 1
        if fails["n"] == 1:
            raise OSError("首次连不上")
        return True                          # 第 2 次起恢复

    app.gateway.connect = maybe_fail
    app.gateway.simulate_disconnect("test 抖动")
    # 首次 _on_connection_lost 立即 _try_reconnect 失败 (reconnect_failed)
    assert _wait(lambda: app.store._conn.execute(
        "SELECT COUNT(*) FROM audit WHERE kind='reconnect_failed'").fetchone()[0] >= 1)
    assert not app.connected
    # clock 越过 backoff 节流, timer_scan 接力 → _try_reconnect 成功
    clock[0] += 3
    app._engine.put(Event(type=EVENT_TIMER_SCAN, data={"hhmm": "10:00"}))
    assert _wait(lambda: app.connected)
    assert app.store._conn.execute(
        "SELECT COUNT(*) FROM audit WHERE kind='reconnect'").fetchone()[0] >= 1
