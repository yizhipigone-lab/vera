"""Monitor 监控腿单元测试 (P1 第二阶段, 2026-07-26).

锁住: 订阅 tick 驱动评估 / 心跳超时降级轮询并恢复 / 无价 fail-closed /
移动止盈 (合成峰值序列) / 硬止损 / 时间止损 / 触发后调 execute_exit
(Stub 单测 + FakeGateway 全链路各一)。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.book import DIRECTION_BUY, Book
from trade.config import (
    CostStopConfig,
    LadderTpConfig,
    StopConfig,
    TradeConfig,
    TrailingStopConfig,
)
from trade.executor import Executor
from trade.gateway import FakeGateway
from trade.monitor import Monitor
from trade.risk import KillSwitch, RiskContext, RiskGate
from trade.store import TradeStore

# 时段感知后 (2026-07-27 ETF 误卖事件裁决①): 自动规则只在连续竞价
# 评估, 测试时钟必须落在工作日盘中 (2024-01-02 周二 10:00)
_T0 = time.mktime(time.strptime("2024-01-02 10:00", "%Y-%m-%d %H:%M"))

CODE = "600519.SH"


class StubExecutor:
    """记录调用的 executor 替身 —— monitor 单测不背执行细节。
    succeed=False 时模拟 execute_exit 合法 fail-closed 返回 (H1 用)。"""

    def __init__(self, succeed=True):
        self.exits: list[tuple[str, str]] = []
        self.qtys: list[int | None] = []
        self.pending_calls = 0
        self.succeed = succeed

    def execute_exit(self, code, reason, qty=None):
        self.exits.append((code, reason))
        self.qtys.append(qty)
        return self.succeed

    def pending_check(self, now_hhmm=None):
        self.pending_calls += 1


@pytest.fixture()
def store(tmp_path):
    s = TradeStore(tmp_path / "trade.db", tmp_path / "raw.jsonl")
    yield s
    s.close()


def _book_with(code=CODE, volume=1000, cost=10.0):
    book = Book()
    book.apply_trade(f"T-{code}", f"O-{code}", code, DIRECTION_BUY, cost, volume)
    book.set_can_use(code, volume)
    return book


def _make_monitor(store, book, stub, clock, cfg=None, hold_days=None, gw=None):
    config = cfg or TradeConfig(
        account_id="TEST", tick_heartbeat_sec=15,
        # 测试档: 回撤 5% (比默认 1% 好构造序列), 激活线走默认 3.5%
        stop=StopConfig(
            trailing_stop=TrailingStopConfig(drawdown=0.05),
            cost_stop=CostStopConfig(threshold=-0.12),
        ),
    )
    gateway = gw or FakeGateway()
    gateway.connect()
    # 测试里 clock 习惯传 [ts] 列表便于推进, 统一包成 callable
    clock_fn = clock if callable(clock) else (lambda: clock[0])
    return Monitor(gateway, book, stub, store, config,
                   hold_days=hold_days or (lambda code: 0),
                   clock=clock_fn), gateway


def test_tick_drives_evaluation_no_trigger(store):
    """正常 tick: 缓存更新, 价在规则内不触发 (10.5 不破 ladder 档 10.6,
    trailing 峰值回撤线 9.975 也不破)。"""
    t = [_T0]
    mon, _ = _make_monitor(store, _book_with(), StubExecutor(), t)
    mon.on_quote(CODE, {"last": 10.5, "bid1": 10.4, "high": 10.5})
    assert mon.quote_of(CODE)["last"] == 10.5
    assert mon.scan_once() == []
    assert mon.is_healthy()


def test_quote_caches_prev_close(store):
    """2026-07-31: 昨收随 tick 缓存 (持仓页当日涨跌的数据源);
    后续 tick 缺 prev_close 时保留已有值, 不被 0 覆盖。"""
    t = [_T0]
    mon, _ = _make_monitor(store, _book_with(), StubExecutor(), t)
    mon.on_quote(CODE, {"last": 10.5, "bid1": 10.4, "prev_close": 10.0})
    assert mon.quote_of(CODE)["prev_close"] == 10.0
    mon.on_quote(CODE, {"last": 10.6, "bid1": 10.5})   # 无昨收字段
    q = mon.quote_of(CODE)
    assert q["last"] == 10.6 and q["prev_close"] == 10.0


def test_heartbeat_timeout_degrades_to_polling(store):
    """无 tick 超心跳 → 降级轮询 (audit 留痕), 快照走同一评估路径。"""
    t = [_T0]
    stub = StubExecutor()
    gw = FakeGateway()
    gw.connect()
    # 快照只在网关侧 (订阅断了, 轮询兜底才拿得到)
    gw.push_quote(CODE, {"last": 10.5, "bid1": 10.4, "high": 10.5})
    mon, _ = _make_monitor(store, _book_with(), stub, t, gw=gw)
    t[0] += 100  # 远超 15s 心跳
    assert mon.scan_once() == []
    assert not mon.is_healthy()
    # 轮询兜底把快照灌进了缓存 (on_quote 同一路径)
    assert mon.quote_of(CODE)["last"] == 10.5
    kinds = {r[0] for r in store._conn.execute("SELECT kind FROM audit").fetchall()}
    assert "monitor_degrade" in kinds


def test_recovery_after_tick_returns(store):
    """降级后 tick 恢复 → 自动切回订阅驱动 + audit。"""
    t = [_T0]
    mon, _ = _make_monitor(store, _book_with(), StubExecutor(), t)
    t[0] += 100
    mon.scan_once()
    assert not mon.is_healthy()
    mon.on_quote(CODE, {"last": 10.5, "bid1": 10.4, "high": 10.5})
    mon.scan_once()
    assert mon.is_healthy()
    kinds = {r[0] for r in store._conn.execute("SELECT kind FROM audit").fetchall()}
    assert "monitor_recover" in kinds


def test_no_quote_fail_closed(store):
    """持仓票无行情 (订阅轮询都没有) → 本轮跳过 + WARN, 绝不按无数据处理。"""
    t = [_T0]
    stub = StubExecutor()
    mon, _ = _make_monitor(store, _book_with(), stub, t)
    t[0] += 100
    assert mon.scan_once() == []
    assert stub.exits == []
    rows = store._conn.execute(
        "SELECT kind FROM audit WHERE kind='monitor_no_quote'").fetchall()
    assert rows


def test_trailing_stop_triggered_by_synthetic_peak(store):
    """合成峰值序列: 冲到 12 后回落破 峰值×(1-5%)=11.4 → 移动止盈。
    (档位已预标记 = 实盘正常日状态: ladder 由预埋单覆盖,
    monitor 只评动态腿, 否则 ladder 兜底腿会先抢答)"""
    t = [_T0]
    stub = StubExecutor()
    book = _book_with()
    today = time.strftime("%Y%m%d", time.localtime(_T0))
    book.mark_tier(CODE, 0, today)
    book.mark_tier(CODE, 1, today)
    mon, _ = _make_monitor(store, book, stub, t)
    mon.on_quote(CODE, {"last": 12.0, "bid1": 11.9, "high": 12.0})
    assert mon.scan_once() == []                      # 峰值上不触发
    mon.on_quote(CODE, {"last": 11.5, "bid1": 11.4, "high": 11.5})
    assert mon.scan_once() == []                      # 11.5 > 11.4 线
    mon.on_quote(CODE, {"last": 11.3, "bid1": 11.2, "high": 11.3})
    triggers = mon.scan_once()                        # 11.3 < 11.4, 峰值仍是 12
    assert len(triggers) == 1 and "trailing" in triggers[0][1]
    assert stub.exits[0][0] == CODE
    # 2026-07-31: 正文自然语言带关键数字 (最高/激活线/回撤/阈值)
    body = triggers[0][1]
    assert "最高 12.00" in body and "激活线" in body
    assert "回撤" in body and "阈值" in body


def test_cost_stop_triggered(store):
    """现价 8.7 ≤ 成本 10×(1-12%)=8.8 → 成本止损 (threshold 负值口径)。"""
    t = [_T0]
    stub = StubExecutor()
    mon, _ = _make_monitor(store, _book_with(), stub, t)
    mon.on_quote(CODE, {"last": 8.7, "bid1": 8.6, "high": 8.7})
    triggers = mon.scan_once()
    assert len(triggers) == 1 and "cost_stop" in triggers[0][1]


def test_auto_sell_disabled_no_trigger(store):
    """2026-08-16 卖出总开关关闭 → 监控腿不评估任何卖出规则 (不新触发)。"""
    t = [_T0]
    stub = StubExecutor()
    cfg = TradeConfig(account_id="TEST", tick_heartbeat_sec=15,
                      auto_sell_enabled=False,
                      stop=StopConfig(cost_stop=CostStopConfig(threshold=-0.12)))
    mon, _ = _make_monitor(store, _book_with(), stub, t, cfg=cfg)
    # 现价 8.7 本应触发成本止损, 但总开关关 → 不触发
    mon.on_quote(CODE, {"last": 8.7, "bid1": 8.6, "high": 8.7})
    assert mon.scan_once() == []


def test_time_stop_triggered(store):
    """持有 20 天到点即走 (裁决①后回测口径: 无收益门槛)。"""
    t = [_T0]
    stub = StubExecutor()
    mon, _ = _make_monitor(store, _book_with(), stub, t,
                           hold_days=lambda code: 20)
    mon.on_quote(CODE, {"last": 10.05, "bid1": 10.0, "high": 10.05})
    triggers = mon.scan_once()
    assert len(triggers) == 1 and "time_stop" in triggers[0][1]


def test_triggered_code_not_retriggered(store):
    """同票当日只触发一次 (防连环触发轰炸执行层)。"""
    t = [_T0]
    stub = StubExecutor()
    mon, _ = _make_monitor(store, _book_with(), stub, t)
    mon.on_quote(CODE, {"last": 8.7, "bid1": 8.6, "high": 8.7})
    mon.scan_once()
    assert mon.scan_once() == []
    assert len(stub.exits) == 1


def test_h1_failed_exit_not_armed_and_retried(store):
    """审计H1修复: 执行返回 False → 不入 _triggered, 写 WARN, 下轮再评
    (旧实现先标记 = 一跳行情延迟换一整天无保护)。"""
    t = [_T0]
    stub = StubExecutor(succeed=False)
    mon, _ = _make_monitor(store, _book_with(), stub, t)
    mon.on_quote(CODE, {"last": 8.7, "bid1": 8.6, "high": 8.7})
    assert mon.scan_once() == []              # 未武装, 不在触发列表
    assert CODE not in mon._triggered
    rows = store._conn.execute(
        "SELECT kind FROM audit WHERE kind='exit_arm_fail'").fetchall()
    assert rows
    # 下轮执行恢复成功 → 正常触发武装
    stub.succeed = True
    triggers = mon.scan_once()
    assert len(triggers) == 1 and CODE in mon._triggered


def test_h2_triggered_cleared_next_day(store):
    """审计H2修复: _triggered 跨日清空 —— 昨日触发的票今日照常评估。"""
    t = [_T0]
    stub = StubExecutor()
    mon, _ = _make_monitor(store, _book_with(), stub, t)
    mon.on_quote(CODE, {"last": 8.7, "bid1": 8.6, "high": 8.7})
    mon.scan_once()
    assert CODE in mon._triggered
    t[0] += 86400                              # 跨日
    mon.on_quote(CODE, {"last": 8.7, "bid1": 8.6, "high": 8.7})
    triggers = mon.scan_once()
    assert len(triggers) == 1                  # 今日重新评估触发
    assert len(stub.exits) == 2


def test_m4_stale_tick_dropped(store):
    """审计M4修复: tick 事件 ts 与本地时钟偏差 >30s → 丢弃 + WARN,
    不盖心跳 (断线重连后积压事件不得冒充新鲜行情)。"""
    t = [_T0]
    mon, _ = _make_monitor(store, _book_with(), StubExecutor(), t)
    mon.on_quote(CODE, {"last": 12.0, "bid1": 11.9, "high": 12.0},
                 event_ts=_T0 - 120)
    assert mon.quote_of(CODE) is None          # 缓存没被污染
    rows = store._conn.execute(
        "SELECT kind FROM audit WHERE kind='monitor_stale_tick'").fetchall()
    assert rows


def test_m6_stale_quote_skipped_in_scan(store):
    """审计M6修复: 快照超 quote_stale_sec 视为陈旧 → fail-closed 跳过 + WARN
    (旧实现 fail-closed 只覆盖"无价"不覆盖"陈旧价")。"""
    t = [_T0]
    stub = StubExecutor()
    mon, _ = _make_monitor(store, _book_with(), stub, t)
    mon.on_quote(CODE, {"last": 8.7, "bid1": 8.6, "high": 8.7})
    t[0] += 120                                # 快照变陈旧 (阈值 60s)
    assert mon.scan_once() == []
    assert stub.exits == []                    # 8.7 本是硬止损价, 但价旧不卖
    rows = store._conn.execute(
        "SELECT kind FROM audit WHERE kind='monitor_stale_quote'").fetchall()
    assert rows


def test_full_chain_trigger_to_fake_gateway_fill(store, tmp_path):
    """全链路: 成本止损触发 → 真 Executor → FakeGateway 卖出成交 → 清锁。"""
    kill = KillSwitch(store, tmp_path / "KILL")
    book = _book_with()
    gw = FakeGateway(positions={CODE: {"volume": 1000, "can_use": 1000,
                                       "avg_cost": 10.0}})
    gw.connect()
    cfg = TradeConfig(account_id="TEST", tick_heartbeat_sec=15,
                      stop=StopConfig(
                          trailing_stop=TrailingStopConfig(drawdown=0.05),
                          cost_stop=CostStopConfig(threshold=-0.12)))
    gate = RiskGate(kill, store, cfg.daily_loss_limit,
                    sizing=cfg.position_sizing)

    def build_ctx():
        return RiskContext(
            reconcile_passed=True, total_asset=1_000_000.0,
            positions=book.snapshot()["positions"],
            day_baseline_equity=1_000_000.0, current_equity=1_000_000.0,
            is_trading_day=True)

    clock = [_T0]
    # 注: executor 走共享 fail-closed 判定, 裸 quote 无 ts 键会判"陈旧"拒卖;
    # 此处补 ts=_T0 使快照新鲜 (clock 即 _T0)。
    quotes = {CODE: {"last": 8.7, "bid1": 8.7, "high": 8.7, "ts": _T0}}
    ex = Executor(gw, book, store, gate, cfg, build_ctx,
                  get_quote=quotes.get, clock=lambda: clock[0])
    mon = Monitor(gw, book, ex, store, cfg, clock=lambda: clock[0])
    mon.on_quote(CODE, quotes[CODE])
    triggers = mon.scan_once()
    assert triggers and "cost_stop" in triggers[0][1]
    sells = [o for o in gw.query_orders() if o["direction"] == 24]
    assert len(sells) == 1 and sells[0]["price"] == 8.7  # 买一价限价
    # 成交 → 下一轮扫描清登记放锁
    gw.simulate_fill(sells[0]["order_id"])
    clock[0] += 1
    mon.pending_check()
    assert CODE not in ex._pending
    assert not ex.lock.is_held(CODE)
    # Fake 侧持仓清零, 回款到账
    assert gw.query_positions() == []
    assert gw.query_asset()["cash"] == 1_000_000.0 + 8.7 * 1000


# ═══════════════════════════════════════════════════════════════
# 阶梯兜底部分卖 (2026-08-06 002155.SZ 事件: 兜底不再一锅端)
# ═══════════════════════════════════════════════════════════════

def _ladder_cfg(levels):
    return TradeConfig(
        account_id="TEST", tick_heartbeat_sec=15,
        stop=StopConfig(ladder_tp=LadderTpConfig(levels=levels)))


def test_ladder_fallback_partial_sell_marks_tier_not_armed(store):
    """未预埋档兜底 = 按档位比例部分卖: 乐观标档但不武装 _triggered,
    剩余仓位继续受其余规则保护; 清仓档兜底仍全卖+武装。"""
    t = [_T0]
    stub = StubExecutor()
    book = _book_with(volume=400)
    cfg = _ladder_cfg(((0.05, 0.5), (0.10, 1.0)))
    mon, _ = _make_monitor(store, book, stub, t, cfg=cfg)
    today = time.strftime("%Y%m%d", time.localtime(_T0))
    # 档1 (+5% = 10.50) 涨破 → 卖一半 200 股, 标档0, 不武装
    mon.on_quote(CODE, {"last": 10.4, "bid1": 10.4, "high": 10.5})
    triggers = mon.scan_once()
    assert len(triggers) == 1 and "ladder_tp" in triggers[0][1]
    assert stub.qtys == [200]
    assert book.tier_done(CODE, today) == frozenset({0})
    assert CODE not in mon._triggered
    # 档2 (+10% = 11.00) 涨破 → 清仓档 qty=None (卖全部), 武装
    mon.on_quote(CODE, {"last": 10.9, "bid1": 10.9, "high": 11.0})
    triggers = mon.scan_once()
    assert len(triggers) == 1
    assert stub.qtys == [200, None]
    assert CODE in mon._triggered


def test_ladder_fallback_qty_lot_rounding(store):
    """250 股 × 50% = 1.25 手 → 1 手 100 股 (对齐 place_ladder 口径)。"""
    t = [_T0]
    stub = StubExecutor()
    book = _book_with(volume=250)
    mon, _ = _make_monitor(store, book, stub, t,
                           cfg=_ladder_cfg(((0.05, 0.5),)))
    mon.on_quote(CODE, {"last": 10.5, "bid1": 10.5, "high": 10.5})
    assert len(mon.scan_once()) == 1
    assert stub.qtys == [100]


def test_ladder_fallback_tiny_position_sells_all(store):
    """比例档算不出整手 (50 股 × 50% = 0 手) → qty=None 卖全部,
    兜底语义宁可全卖不漏卖。"""
    t = [_T0]
    stub = StubExecutor()
    book = _book_with(volume=50)
    mon, _ = _make_monitor(store, book, stub, t,
                           cfg=_ladder_cfg(((0.05, 0.5),)))
    mon.on_quote(CODE, {"last": 10.5, "bid1": 10.5, "high": 10.5})
    assert len(mon.scan_once()) == 1
    assert stub.qtys == [None]
    assert CODE in mon._triggered
