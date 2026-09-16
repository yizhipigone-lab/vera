"""ETF 轮动「资金三份错峰」测试 (2026-09-16, 计划书
docs/plan/2026-09-16_ETF轮动资金三份错峰改造_计划书.md §八 测试清单)。

锁住: 份簿记/逐份状态/元数据三张表的存取、signal_day 列表校验、
启动迁移 (按手轮转/碎股归尾/entry_high 继承与回退/已初始化标志)、
逐份执行 (只卖自己那份/止损按份/clear_external_sells 单次/现金按份序)、
在途卖单隔夜核销、簿记-QMT 漂移对账告警、D7 跳过路径状态注入回归。
"""
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.book import DIRECTION_BUY, DIRECTION_SELL
from trade.config import RotationConfig, TradeConfig, load_trade_config
from trade.rotation import _is_signal_day
from trade.store import TradeStore
from trade_main import TradeApp

CYB = "159949.SZ"
GOLD = "518880.SH"
NASDAQ = "513100.SH"

WD3 = ["wednesday", "thursday", "friday"]


def _ts(hhmm):
    d = datetime.now().replace(
        hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.timestamp()


def _wait(pred, timeout=4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _cfg(tmp_path, days=WD3, **rot):
    base = {"enabled": True, "etf_ratio": 0.5, "signal_day": tuple(days)}
    base.update(rot)
    return TradeConfig(
        account_id="AB", fake_sdk=True,
        db_path=str(tmp_path / "t.db"),
        raw_log_path=str(tmp_path / "r.jsonl"),
        kill_flag_path=str(tmp_path / "KILL"),
        rotation=RotationConfig(**base),
    )


def _app(cfg, clock, positions=None, cash=1_000_000.0, closes=None):
    app = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                   fake_gateway_kwargs={"cash": cash,
                                        "positions": positions or {},
                                        "daily_closes": closes or {}},
                   selection_runner=lambda f, a, u: [])
    app._rotation._wait_timeout = 0.05
    app._rotation._wait_interval = 0.01
    return app


def _start(app):
    assert app.start(start_timers=False)
    return app


def _quote(code, last, bid=None, ask=None):
    return {"last": last, "bid1": bid if bid is not None else last - 0.01,
            "ask1": ask if ask is not None else last + 0.01, "high": last,
            "prev_close": last}


def _orders_by(app):
    o = app.gateway.query_orders()
    return ([x for x in o if x["direction"] == DIRECTION_BUY],
            [x for x in o if x["direction"] == DIRECTION_SELL])


def _audit(app, kind):
    ro = app.store.open_readonly()
    try:
        return ro.execute(
            "SELECT detail_json FROM audit WHERE kind=? ORDER BY rowid",
            (kind,)).fetchall()
    finally:
        ro.close()


# ═══════════════════════════════════════════════════════════════
# store: 三张新表
# ═══════════════════════════════════════════════════════════════

def test_store_lots_roundtrip(tmp_path):
    st = TradeStore(tmp_path / "t.db", tmp_path / "r.jsonl")
    assert st.rotation_lots.load_all() == {}
    st.rotation_lots.replace_all({
        (0, CYB): {"qty": 100, "entry_high": 1.5},
        (2, NASDAQ): {"qty": 50, "entry_high": 0.0},
        (1, GOLD): {"qty": 0, "entry_high": 0.0},     # qty≤0 → 不落库
    })
    lots = st.rotation_lots.load_all()
    assert lots == {(0, CYB): {"qty": 100, "entry_high": 1.5},
                    (2, NASDAQ): {"qty": 50, "entry_high": 0.0}}
    st.rotation_lots.replace_all({})                   # 整表清空
    assert st.rotation_lots.load_all() == {}
    st.close()


def test_store_tranche_state_and_meta(tmp_path):
    st = TradeStore(tmp_path / "t.db", tmp_path / "r.jsonl")
    st.rotation_tranche.save(0, {"ts": 1.0, "source": "test",
                                 "signal": {"pending_target": CYB}})
    st.rotation_tranche.save(2, {"ts": 2.0, "source": "test",
                                 "signal": {"pending_target": None,
                                            "has_target": True}})
    allst = st.rotation_tranche.load_all()
    assert allst[0]["signal"]["pending_target"] == CYB
    assert allst[2]["signal"]["has_target"] is True
    assert st.rotation_meta.get("lots_initialized") is None
    st.rotation_meta.set("lots_initialized", "1")
    assert st.rotation_meta.get("lots_initialized") == "1"
    st.close()


# ═══════════════════════════════════════════════════════════════
# config: signal_day 列表校验 (计划书 §三)
# ═══════════════════════════════════════════════════════════════

def test_config_signal_day_list(tmp_path):
    def _load(text):
        p = tmp_path / "c.yaml"
        p.write_text(text, encoding="utf-8")
        return load_trade_config(p)
    c = _load("rotation:\n  signal_day: [wednesday, thursday, friday]\n")
    assert c.rotation.signal_day == ("wednesday", "thursday", "friday")
    c2 = _load("rotation:\n  signal_day: friday\n")          # 旧单字符串兼容
    assert c2.rotation.signal_day == ("friday",)
    with pytest.raises(ValueError, match="signal_day"):
        _load("rotation:\n  signal_day: [friday, friday]\n")  # 重复
    with pytest.raises(ValueError, match="signal_day"):
        _load("rotation:\n  signal_day: [sunday]\n")          # 非法枚举
    with pytest.raises(ValueError, match="signal_day"):
        _load("rotation:\n  signal_day: []\n")                # 空列表
    with pytest.raises(ValueError, match="signal_day"):
        _load("rotation:\n  signal_day: [monday, tuesday, wednesday,"
              " thursday, friday, monday]\n")                 # 超 5 项


# ═══════════════════════════════════════════════════════════════
# 迁移初始化 (计划书 D4)
# ═══════════════════════════════════════════════════════════════

def test_migration_splits_round_robin(tmp_path):
    """250 份持仓 → 按手轮转 100/100 + 零头 50 归尾份; entry_high 退实时价。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=0,   # cash=0: 隔离买入侧, 只看迁移分份
                      positions={NASDAQ: {"volume": 250, "can_use": 250,
                                          "avg_cost": 2.0}}))
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 2.2, bid=2.2))
    app.gateway.push_quote(GOLD, _quote(GOLD, 9.0, bid=9.0, ask=9.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    app._rotation.on_signals({"signals": {}, "source": "test"})
    lots = app._rotation._lots
    assert lots[(0, NASDAQ)]["qty"] == 100
    assert lots[(1, NASDAQ)]["qty"] == 100
    assert lots[(2, NASDAQ)]["qty"] == 50               # 碎股归尾份
    assert lots[(0, NASDAQ)]["entry_high"] == pytest.approx(2.2)
    # 份目标派生: 三份都持纳指 → pending 都是 NASDAQ
    for i in range(3):
        assert app._rotation._tranches[i]["pending_target"] == NASDAQ
        assert app._rotation._tranches[i]["has_target"] is True
    assert app.store.rotation_meta.get("lots_initialized") == "1"
    assert _audit(app, "rotation_migrate")
    # 迁移后不再重迁: 第二轮 on_signals lots 不变
    app._rotation.on_signals({"signals": {}, "source": "test"})
    assert app._rotation._lots[(2, NASDAQ)]["qty"] == 50


def test_migration_inherits_legacy_entry_high(tmp_path):
    """旧 rotation_state 有同代码 entry_high → 继承 (保住现有止损保护)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=500_000,
                      positions={NASDAQ: {"volume": 300, "can_use": 300,
                                          "avg_cost": 2.0}}))
    app.store.rotation_signal.save(
        {"ts": 1.0, "source": "legacy",
         "signal": {"entry_high": {NASDAQ: 2.5}}})
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 2.2, bid=2.2))
    app.gateway.push_quote(GOLD, _quote(GOLD, 9.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    app._rotation.on_signals({"signals": {}, "source": "test"})
    assert app._rotation._lots[(0, NASDAQ)]["entry_high"] == pytest.approx(2.5)


def test_migration_guard_flag_blocks_reinit(tmp_path):
    """已初始化标志 + lots 被清空 → 重启不自动重迁 (防误删静默重排)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=500_000,
                      positions={NASDAQ: {"volume": 300, "can_use": 300,
                                          "avg_cost": 2.0}}))
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 2.2))
    app.gateway.push_quote(GOLD, _quote(GOLD, 9.0))
    # 模拟「已初始化过但 lots 表被误删」: 标志在, lots 空
    app.store.rotation_meta.set("lots_initialized", "1")
    app2_rotation = app._rotation
    app2_rotation._lots = {}
    app2_rotation._migration_pending = False   # __init__ 读标志后的状态
    app._rotation.on_signals({"signals": {}, "source": "test"})
    assert app._rotation._lots == {}            # 未自动重迁
    assert not _audit(app, "rotation_migrate")


# ═══════════════════════════════════════════════════════════════
# 逐份执行: 只卖自己那份 / 止损按份
# ═══════════════════════════════════════════════════════════════

def _seed_three(app, eh=1.0):
    app._rotation._migration_pending = False
    app._rotation._lots = {(i, NASDAQ): {"qty": 100_000, "entry_high": eh}
                           for i in range(3)}


def test_tranche_sells_only_own_lot(tmp_path):
    """份2 (周四) 信号日择避险 → 只卖份2 的 1/3, 份1/份3 一股不动。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=0,   # cash=0: 隔离买入侧, 只验「只卖自己那份」
                      positions={NASDAQ: {"volume": 300_000,
                                          "can_use": 300_000,
                                          "avg_cost": 1.0}}))
    _seed_three(app)
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    app._rotation.on_signals({"signals": {1: {
        "target": None, "insufficient": False,
        "momentum": {CYB: -0.1, NASDAQ: -0.03}}}, "source": "test"})
    _, sells = _orders_by(app)
    assert [s["code"] for s in sells] == [NASDAQ]
    assert sum(s["qty"] for s in sells) == 100_000    # 只有份2 那份
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == 100_000
    assert app._rotation._lots[(2, NASDAQ)]["qty"] == 100_000
    assert app._rotation._tranches[1]["pending_target"] is None


def test_trailing_stop_per_tranche(tmp_path):
    """份1 止损基准 1.0 (线 0.85) 触发、份2 基准 0.9 (线 0.765) 不触发:
    同一代码, 只清触发份 (计划书 §八.3)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=700_000,
                      positions={NASDAQ: {"volume": 300_000,
                                          "can_use": 300_000,
                                          "avg_cost": 1.0}}))
    app._rotation._migration_pending = False
    app._rotation._lots = {
        (0, NASDAQ): {"qty": 100_000, "entry_high": 1.0},
        (1, NASDAQ): {"qty": 200_000, "entry_high": 0.9},
    }
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 0.80, bid=0.80))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    app._rotation.on_signals({"signals": {}, "source": "test"})
    _, sells = _orders_by(app)
    assert sum(s["qty"] for s in sells if s["code"] == NASDAQ) == 100_000
    assert app._rotation._tranches[0]["pending_target"] is None
    assert _audit(app, "rotation_stop")


def test_clear_external_sells_once_per_run(tmp_path):
    """clear_external_sells 每次运行只在开头一次 (计划书 D3.3)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=700_000,
                      positions={NASDAQ: {"volume": 300_000,
                                          "can_use": 300_000,
                                          "avg_cost": 1.0}}))
    _seed_three(app)
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    calls = []
    orig = app.executor.clear_external_sells
    app.executor.clear_external_sells = lambda: (calls.append(1), orig())
    app._rotation.on_signals({"signals": {
        0: {"target": None, "insufficient": False},
        1: {"target": None, "insufficient": False}}, "source": "test"})
    assert len(calls) == 1


def test_cash_competition_tranche_order(tmp_path):
    """现金只够一份时, 份序号小者优先 (计划书 §八.6, 确定性)。"""
    clock = [_ts("10:00")]
    # 总资产 = 现金 20 万 → pool = 10 万, 份池 ≈ 3.33 万; 三份都要买纳指
    app = _start(_app(_cfg(tmp_path), clock, cash=40_000,
                      positions={}))
    app._rotation._migration_pending = False
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    app._rotation.on_signals({"signals": {
        0: {"target": NASDAQ, "insufficient": False},
        1: {"target": NASDAQ, "insufficient": False},
        2: {"target": NASDAQ, "insufficient": False}}, "source": "test"})
    buys, _ = _orders_by(app)
    nasdaq_buys = [b for b in buys if b["code"] == NASDAQ]
    # 现金 4 万: 份1 花 ~3.33 万, 份2 只剩 ~0.67 万, 份3 无 → 份1 买到, 其余买不动或更少
    assert nasdaq_buys
    assert app._rotation._lots[(0, NASDAQ)]["qty"] > 0
    assert (app._rotation._lots.get((2, NASDAQ), {}).get("qty", 0)
            <= app._rotation._lots[(0, NASDAQ)]["qty"])


# ═══════════════════════════════════════════════════════════════
# 在途卖单隔夜核销 (自愈) 与漂移对账
# ═══════════════════════════════════════════════════════════════

def test_open_sell_settles_next_run(tmp_path):
    """卖单本轮未成交 → 簿记保留 + 台账在; 成交后下一轮开场核销 → 买黄金。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path, days=["friday"]), clock, cash=500_000,
                      positions={NASDAQ: {"volume": 100_000,
                                          "can_use": 100_000,
                                          "avg_cost": 1.0}}))
    app._rotation._migration_pending = False
    app._rotation._lots = {(0, NASDAQ): {"qty": 100_000, "entry_high": 1.0}}
    app._rotation._tranches[0]["pending_target"] = NASDAQ
    app._rotation._tranches[0]["has_target"] = True
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    app._rotation.on_signals({"signal": {"target": None}, "source": "test"})
    _, sells = _orders_by(app)
    assert sells and sells[0]["qty"] == 100_000
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == 100_000   # 未成交不动簿记
    app.gateway.simulate_fill(sells[0]["order_id"])             # 盘后成交
    app._rotation.on_signals({"signal": {"target": None}, "source": "test"})
    assert app._rotation._lots.get((0, NASDAQ), {}).get("qty", 0) == 0
    buys, _ = _orders_by(app)
    assert any(b["code"] == GOLD for b in buys)                 # 核销后补买黄金


def test_lots_drift_audit_only(tmp_path):
    """人工卖出轮动代码 → 簿记 ≠ QMT → 只告警不改账 (铁律 1)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=0,   # cash=0: 隔离买入侧
                      positions={NASDAQ: {"volume": 200_000,
                                          "can_use": 200_000,
                                          "avg_cost": 1.0}}))
    _seed_three(app)   # 簿记 300_000 vs QMT 200_000 → 漂移
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    app._rotation.on_signals({"signals": {}, "source": "test"})
    rows = _audit(app, "rotation_lots_drift")
    assert rows
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == 100_000   # 不改账


# ═══════════════════════════════════════════════════════════════
# D7 回归: 跳过路径不覆盖好状态 (2026-09-10 事故)
# ═══════════════════════════════════════════════════════════════

def test_skip_paths_preserve_state(tmp_path):
    """盘后手动触发 (非连续竞价被拦) + signal_only 跳过 →
    落库的 pending_target/entry_high/has_target 不丢 (9-10 事故回归)。"""
    clock = [_ts("18:00")]   # 盘后 → 非连续竞价
    app = _start(_app(_cfg(tmp_path, days=["friday"]), clock))
    app._rotation._migration_pending = False
    app._rotation._lots = {(0, NASDAQ): {"qty": 100_000, "entry_high": 2.2}}
    t0 = app._rotation._tranches[0]
    t0["pending_target"] = NASDAQ
    t0["has_target"] = True
    # 路径 1: 盘后手动触发, 执行被「非连续竞价」拦下
    app._rotation.on_signals({"signal": {"target": CYB}, "source": "manual_api"})
    rec = app.store.rotation_tranche.load_all()[0]
    assert rec["signal"]["pending_target"] == NASDAQ          # 不被目标信号覆盖
    assert rec["signal"]["entry_high"] == {NASDAQ: 2.2}
    assert rec["signal"]["has_target"] is True
    # 路径 2: signal_only 跳过 (非交易日/首信号未算)
    app._rotation.on_signals({"signals": {}, "signal_only": True,
                              "note": "非交易日, 不动作", "source": "scheduled"})
    rec = app.store.rotation_tranche.load_all()[0]
    assert rec["signal"]["pending_target"] == NASDAQ
    assert rec["signal"]["entry_high"] == {NASDAQ: 2.2}


# ═══════════════════════════════════════════════════════════════
# 信号日多锚定 (计划书 §八.1)
# ═══════════════════════════════════════════════════════════════

def test_is_signal_day_multi_anchor(monkeypatch):
    """周三锚在周三休市周的正确前移; 同一周五对三锚的不同判定。"""
    monkeypatch.setattr("trade.rotation.is_trading_day_cached", lambda d: True)

    def _nxt(d):
        d = d + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d
    monkeypatch.setattr("trade.rotation.next_trading_day", _nxt)
    wed, thu, fri = (date(2026, 8, 19), date(2026, 8, 20), date(2026, 8, 21))
    assert _is_signal_day(wed, "wednesday") is True
    assert _is_signal_day(wed, "friday") is False
    assert _is_signal_day(thu, "thursday") is True
    assert _is_signal_day(fri, "friday") is True
    assert _is_signal_day(fri, "thursday") is False   # 已过周四锚定


# ═══════════════════════════════════════════════════════════════
# llm_review 逐份形态
# ═══════════════════════════════════════════════════════════════

def test_llm_review_multi_tranche():
    from trade.llm_review import format_daily_data
    payload = {"rotation": [
        {"anchor": "wednesday", "target": NASDAQ,
         "momentum": {CYB: -0.1, NASDAQ: 0.03},
         "entry_high": {NASDAQ: 2.236}},
        {"anchor": "friday", "target": None,
         "momentum": {CYB: -0.1, NASDAQ: -0.02}},
    ]}
    out = format_daily_data(payload)
    assert "份1(wednesday)" in out and "份2(friday)" in out
    assert "避险篮子(黄金)" in out
    # 单 dict 旧形态仍认 (N=1 无前缀)
    out2 = format_daily_data({"rotation": {
        "target": NASDAQ, "momentum": {CYB: 0.01, NASDAQ: 0.03}}})
    assert "轮动信号" in out2 and "份1" not in out2
