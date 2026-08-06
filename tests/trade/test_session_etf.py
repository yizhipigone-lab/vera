"""2026-07-27 ETF 误卖事件五项联动测试 (裁决①②③④).

锁住: 时段判定边界矩阵 / 非连续竞价自动规则全静默 + 人工放行 /
tick 缺时间戳 fail-closed / ETF 排除 (ladder+monitor, 对账照盖) /
手工成交认领 (认领→不拉闸→幂等; 解释不了仍 CRITICAL)。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.book import DIRECTION_BUY, Book, is_etf
from trade.config import LadderTpConfig, StopConfig, TradeConfig
from trade.executor import Executor
from trade.gateway import FakeGateway
from trade.monitor import Monitor, trading_session
from trade.reconciler import LEVEL_CRITICAL, LEVEL_NONE, Reconciler
from trade.risk import KillSwitch, RiskContext, RiskGate
from trade.store import TradeStore

CODE = "600519.SH"
ETF = "510300.SH"   # 沪市 ETF (51 前缀)

# 2026-07-27 是周一, 时段矩阵都用它
DAY = "2026-07-27"


def _ts(hhmm, day=DAY):
    return time.mktime(time.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M"))


@pytest.fixture()
def store(tmp_path):
    s = TradeStore(tmp_path / "t.db", tmp_path / "r.jsonl")
    yield s
    s.close()


# ═══════════════════════════════════════════════════════════════
# 1. trading_session 边界矩阵 (裁决①)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("hhmm,expected", [
    ("09:14", "pre_open"),
    ("09:15", "auction"),
    ("09:29", "auction"),
    ("09:30", "continuous"),
    ("11:29", "continuous"),
    ("11:30", "lunch"),
    ("12:59", "lunch"),
    ("13:00", "continuous"),
    ("14:59", "continuous"),
    ("15:00", "closed"),
    ("23:30", "closed"),
])
def test_session_boundaries(hhmm, expected):
    assert trading_session(_ts(hhmm)) == expected


def test_session_weekend_is_closed():
    # 2026-07-25 周六 / 2026-07-26 周日, 盘中时刻也算 closed
    assert trading_session(_ts("10:00", "2026-07-25")) == "closed"
    assert trading_session(_ts("10:00", "2026-07-26")) == "closed"


# ═══════════════════════════════════════════════════════════════
# 1b. D5 (2026-08-01): 节假日日历接入 trading_session
# ═══════════════════════════════════════════════════════════════

import trade.monitor as monitor_mod  # noqa: E402
from trade.monitor import is_trading_day_cached  # noqa: E402


@pytest.fixture()
def clear_td_cache():
    monitor_mod._TRADING_DAY_CACHE.clear()
    yield
    monitor_mod._TRADING_DAY_CACHE.clear()


def test_session_holiday_is_closed(clear_td_cache):
    """D5: 法定节假日盘中时刻也判 closed (2026-01-01 周四元旦,
    精确历/降级表两路径 2026 口径一致); 相邻交易日不受影响。"""
    assert trading_session(_ts("10:00", "2026-01-01")) == "closed"
    assert trading_session(_ts("10:00", "2026-01-05")) == "continuous"  # 周一


def test_is_trading_day_cached_memoizes(clear_td_cache, monkeypatch):
    """D5: 按日 memoize —— 同日重复调用只查一次日历, 跨日各查一次。"""
    import datetime as dt
    calls = []
    real = monitor_mod._cal_is_trading_day

    def spy(d):
        calls.append(d)
        return real(d)

    monkeypatch.setattr(monitor_mod, "_cal_is_trading_day", spy)
    d1 = dt.date(2026, 7, 27)
    assert is_trading_day_cached(d1) is True
    assert is_trading_day_cached(d1) is True
    assert len(calls) == 1
    assert is_trading_day_cached(dt.date(2026, 7, 28)) is True
    assert len(calls) == 2


def test_is_trading_day_cached_fallback_on_error(clear_td_cache, monkeypatch):
    """D5: 日历异常回落周末判定 (fail-open, 不压制盘中规则)。"""
    import datetime as dt

    def boom(d):
        raise RuntimeError("日历故障")

    monkeypatch.setattr(monitor_mod, "_cal_is_trading_day", boom)
    assert is_trading_day_cached(dt.date(2026, 7, 27)) is True   # 周一
    assert is_trading_day_cached(dt.date(2026, 7, 25)) is False  # 周六
    # 回落结果同样进缓存 (当日不再反复 warning)
    assert is_trading_day_cached(dt.date(2026, 7, 27)) is True


# ═══════════════════════════════════════════════════════════════
# 2. 非连续竞价: 自动规则静默, 人工放行 (裁决①②)
# ═══════════════════════════════════════════════════════════════

class StubExecutor:
    def __init__(self):
        self.exits = []
        self.pending_calls = 0

    def execute_exit(self, code, reason, manual=False, qty=None):
        # 对齐真实签名 trade/executor.py:304 (manual + qty), 防 scan 非空时
        # 调用方传参 TypeError (审计 2026-08-06 HIGH#6/0.1)
        self.exits.append((code, reason))
        return True

    def pending_check(self, now_hhmm=None):
        self.pending_calls += 1


def _book_with(store_code=CODE, volume=1000, cost=10.0):
    book = Book()
    book.apply_trade(f"T-{store_code}", f"O-{store_code}", store_code,
                     DIRECTION_BUY, cost, volume)
    book.set_can_use(store_code, volume)
    return book


def _monitor(store, stub, clock, book=None, gw=None):
    cfg = TradeConfig(account_id="TEST")
    gateway = gw or FakeGateway()
    gateway.connect()
    return Monitor(gateway, book or _book_with(), stub, store, cfg,
                   clock=lambda: clock[0])


def test_lunch_scan_silent(store):
    """午休: 硬止损价也不评估 (自动规则只在连续竞价)。"""
    t = [_ts("12:00")]
    stub = StubExecutor()
    mon = _monitor(store, stub, t)
    mon.on_quote(CODE, {"last": 8.0, "bid1": 8.0, "high": 8.0})
    assert mon.scan_once() == []
    assert stub.exits == []


def test_closed_pending_not_escalated(store):
    """收盘后: pending_check 不升级 (14:57 规则也只在盘中生效)。"""
    t = [_ts("15:30")]
    stub = StubExecutor()
    mon = _monitor(store, stub, t)
    mon.pending_check(now_hhmm="15:30")
    assert stub.pending_calls == 0


def test_heartbeat_not_judged_outside_continuous(store):
    """午休/收盘: is_healthy 返回 None (不适用), 不写降级 audit。"""
    t = [_ts("12:00")]
    mon = _monitor(store, StubExecutor(), t)
    mon.on_quote(CODE, {"last": 10.5, "bid1": 10.4, "high": 10.5})
    t[0] += 7200                      # 两小时后 (14:00 前是午休→收盘)
    t[0] = _ts("12:30")               # 仍午休, 心跳早超时
    assert mon.is_healthy() is None
    assert not store._conn.execute(
        "SELECT kind FROM audit WHERE kind='monitor_degrade'").fetchall()
    t[0] = _ts("10:00")               # 回到盘中: 正常判定
    assert mon.is_healthy() is True


def test_manual_sell_allowed_when_closed(store, tmp_path):
    """裁决①: 收盘后人工卖出放行 (manual=True, 无戳买一价也不拦)。"""
    kill = KillSwitch(store, tmp_path / "KILL")
    book = _book_with()
    gw = FakeGateway(positions={CODE: {"volume": 1000, "can_use": 1000,
                                       "avg_cost": 10.0}})
    gw.connect()
    cfg = TradeConfig(account_id="TEST")
    gate = RiskGate(kill, store, cfg.daily_loss_limit,
                    sizing=cfg.position_sizing)
    ctx = lambda: RiskContext(
        reconcile_passed=True, total_asset=1_000_000.0,
        positions=book.snapshot()["positions"],
        day_baseline_equity=1_000_000.0, current_equity=1_000_000.0,
        is_trading_day=True)
    clock = [_ts("21:00")]            # 晚上 (用户要测试的场景)
    ex = Executor(gw, book, store, gate, cfg, ctx,
                  get_quote=lambda c: {"bid1": 10.5, "ts": None},
                  clock=lambda: clock[0])
    assert ex.execute_exit(CODE, "manual_sell: 人工卖出", manual=True)
    sells = [o for o in gw.query_orders() if o["direction"] == 24]
    assert len(sells) == 1 and sells[0]["price"] == 10.5


# ═══════════════════════════════════════════════════════════════
# 3. tick 缺时间戳 → 自动规则 fail-closed (裁决③)
# ═══════════════════════════════════════════════════════════════

def test_none_ts_quote_fail_closed(store):
    """显式 ts=None 的 tick: 缓存标记缺戳, 扫描 fail-closed 跳过。"""
    t = [_ts("10:00")]
    stub = StubExecutor()
    mon = _monitor(store, stub, t)
    mon.on_quote(CODE, {"last": 8.0, "bid1": 8.0, "high": 8.0, "ts": None})
    assert mon.scan_once() == []
    assert stub.exits == []
    rows = store._conn.execute(
        "SELECT message FROM audit WHERE kind='monitor_stale_quote'").fetchall()
    assert any("时间戳" in m for (m,) in rows)


def test_auto_exit_blocked_by_none_ts(store, tmp_path):
    """自动触发 (manual=False): 买一价 ts=None → fail-closed 不卖。"""
    kill = KillSwitch(store, tmp_path / "KILL")
    book = _book_with()
    gw = FakeGateway(positions={CODE: {"volume": 1000, "can_use": 1000,
                                       "avg_cost": 10.0}})
    gw.connect()
    cfg = TradeConfig(account_id="TEST")
    gate = RiskGate(kill, store, cfg.daily_loss_limit,
                    sizing=cfg.position_sizing)
    ctx = lambda: RiskContext(
        reconcile_passed=True, total_asset=1_000_000.0,
        positions=book.snapshot()["positions"],
        day_baseline_equity=1_000_000.0, current_equity=1_000_000.0,
        is_trading_day=True)
    ex = Executor(gw, book, store, gate, cfg, ctx,
                  get_quote=lambda c: {"bid1": 10.5, "ts": None},
                  clock=lambda: _ts("10:00"))
    assert not ex.execute_exit(CODE, "cost_stop: 测试")
    assert not [o for o in gw.query_orders() if o["direction"] == 24]


# ═══════════════════════════════════════════════════════════════
# 4. ETF 排除 (裁决③)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("code,expected", [
    ("510300.SH", True),    # 沪 51
    ("560050.SH", True),    # 沪 56
    ("588000.SH", True),    # 沪 58
    ("159226.SZ", True),    # 深 15
    ("161128.SZ", True),    # 深 16
    ("180301.SZ", True),    # 深 18
    ("000001.SZ", False),
    ("600519.SH", False),
    ("300750.SZ", False),
    ("830799.BJ", False),
])
def test_is_etf_prefix_matrix(code, expected):
    assert is_etf(code) is expected


def test_ladder_skips_etf(store, tmp_path):
    """ETF 持仓不挂预埋单, audit 每票一条不刷屏; 股票照常。"""
    kill = KillSwitch(store, tmp_path / "KILL")
    book = _book_with(CODE)
    book.apply_trade("T-etf", "O-etf", ETF, DIRECTION_BUY, 4.0, 1000)
    book.set_can_use(ETF, 1000)
    gw = FakeGateway(positions={
        CODE: {"volume": 1000, "can_use": 1000, "avg_cost": 10.0},
        ETF: {"volume": 1000, "can_use": 1000, "avg_cost": 4.0}})
    gw.connect()
    cfg = TradeConfig(account_id="TEST",
                      stop=StopConfig(ladder_tp=LadderTpConfig(
                          levels=((0.05, 0.5),))))
    gate = RiskGate(kill, store, cfg.daily_loss_limit,
                    sizing=cfg.position_sizing)
    ctx = lambda: RiskContext(
        reconcile_passed=True, total_asset=1_000_000.0,
        positions=book.snapshot()["positions"],
        day_baseline_equity=1_000_000.0, current_equity=1_000_000.0,
        is_trading_day=True)
    ex = Executor(gw, book, store, gate, cfg, ctx,
                  get_prev_close=lambda c: 10.0 if c == CODE else 4.0,
                  clock=lambda: _ts("10:00"))
    placed = ex.place_ladder("20260727")
    codes = {o["code"] for o in gw.query_orders()}
    assert codes == {CODE}                       # ETF 一张都没挂
    rows = store._conn.execute(
        "SELECT kind FROM audit WHERE kind='ladder_skip_etf'").fetchall()
    assert len(rows) == 1                        # 每票一条, 不刷屏


def test_monitor_skips_etf(store):
    """ETF 持仓: 硬止损价也不评估。"""
    t = [_ts("10:00")]
    stub = StubExecutor()
    mon = _monitor(store, stub, t, book=_book_with(ETF, 1000, 4.0))
    mon.on_quote(ETF, {"last": 3.0, "bid1": 3.0, "high": 3.0})
    assert mon.scan_once() == []
    assert stub.exits == []


def test_reconciler_still_covers_etf(store, tmp_path):
    """ETF 不自动管理 ≠ 不对账: ETF 差异照样 CRITICAL。"""
    kill = KillSwitch(store, tmp_path / "KILL")
    book = _book_with(ETF, 1000, 4.0)
    gw = FakeGateway(positions={ETF: {"volume": 100, "can_use": 100,
                                      "avg_cost": 4.0}})
    gw.connect()
    rec = Reconciler(gw, book, store, kill, retry_interval_sec=0.0)
    report = rec.reconcile()
    assert report.level == LEVEL_CRITICAL
    assert kill.is_active()


# ═══════════════════════════════════════════════════════════════
# 5. 手工成交认领 (裁决④)
# ═══════════════════════════════════════════════════════════════

def test_manual_trade_adopted_no_kill(store, tmp_path):
    """客户端手工买入 → 对账认领 (book 补记+audit) → 不拉闸。"""
    kill = KillSwitch(store, tmp_path / "KILL")
    book = Book()                                  # 本地空
    gw = FakeGateway(positions={})
    gw.connect()
    # 用户在客户端手工买了 500 股 (券商端变了, 本系统不知情)
    gw.simulate_external_trade(CODE, DIRECTION_BUY, 10.0, 500)
    rec = Reconciler(gw, book, store, kill, retry_interval_sec=0.0)
    report = rec.reconcile()
    assert report.level == LEVEL_NONE
    assert report.adopted == 1
    assert not kill.is_active()
    pos = book.snapshot()["positions"][CODE]
    assert pos.volume == 500 and pos.strategy == "手工"
    rows = store._conn.execute(
        "SELECT kind, message FROM audit WHERE kind='manual_adopt'").fetchall()
    assert rows and "认领" in rows[0][1]


def test_manual_adopt_idempotent(store, tmp_path):
    """重复 reconcile 不重复认领 (store + book 双保险判重)。"""
    kill = KillSwitch(store, tmp_path / "KILL")
    book = Book()
    gw = FakeGateway(positions={})
    gw.connect()
    gw.simulate_external_trade(CODE, DIRECTION_BUY, 10.0, 500)
    rec = Reconciler(gw, book, store, kill, retry_interval_sec=0.0)
    rec.reconcile()
    report2 = rec.reconcile()
    assert report2.adopted == 0
    assert book.snapshot()["positions"][CODE].volume == 500
    assert store._conn.execute(
        "SELECT COUNT(*) FROM audit WHERE kind='manual_adopt'"
    ).fetchone()[0] == 1


def test_unexplained_diff_still_critical(store, tmp_path):
    """认领解释不了的差异 (无对应成交记录) → 照旧 CRITICAL 急停。"""
    kill = KillSwitch(store, tmp_path / "KILL")
    book = _book_with(CODE, 1000)
    gw = FakeGateway(positions={CODE: {"volume": 100, "can_use": 100,
                                       "avg_cost": 10.0}})
    gw.connect()
    rec = Reconciler(gw, book, store, kill, retry_interval_sec=0.0)
    report = rec.reconcile()
    assert report.level == LEVEL_CRITICAL and report.adopted == 0
    assert kill.is_active()
