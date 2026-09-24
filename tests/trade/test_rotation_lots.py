"""ETF 轮动「资金三份错峰」测试 (2026-09-16, 计划书
docs/plan/2026-09-16_ETF轮动资金三份错峰改造_计划书.md §八 测试清单)。

锁住: 份簿记/逐份状态/元数据三张表的存取、signal_day 列表校验、
启动迁移 (按手轮转/碎股归尾/entry_high 继承与回退/已初始化标志)、
逐份执行 (只卖自己那份/止损按份/clear_external_sells 单次/现金按份序)、
在途卖单隔夜核销、簿记-QMT 漂移对账告警、D7 跳过路径状态注入回归。

2026-09-17 追加「买侧在途台账 + 隔夜核销」(计划书
docs/plan/2026-09-17_ETF轮动买侧在途台账与隔夜核销_计划书.md §六):
买侧废单/部成/部撤当轮与隔夜核销 (恰好一次)、查不到保留≤5轮、
台账损坏备份、迁移清台账、少记 fail-closed 闸、对账计入在途买单量。
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
    app._rotation._wait_timeout_buy = 0.05
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


def _audit_msg(app, kind):
    """审计人话文案 (message 列; detail_json 只有机器字段)。"""
    ro = app.store.open_readonly()
    try:
        return [r[0] for r in ro.execute(
            "SELECT message FROM audit WHERE kind=? ORDER BY rowid",
            (kind,)).fetchall()]
    finally:
        ro.close()


def _ledger(app, key="rotation_open_buys"):
    raw = app.store.rotation_meta.get(key)
    return json.loads(raw) if raw else {}


def _seed_lots(app, lots, tranche_target=None):
    """测试注入: 预置份内簿记 (跳过迁移), 可选预置该份目标 (省得等信号日)。"""
    app._rotation._migration_pending = False
    app._rotation._lots = lots
    if tranche_target is not None:
        for i, tgt in enumerate(tranche_target):
            t = app._rotation._tranches[i]
            t["pending_target"] = tgt
            t["has_target"] = True


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


# ═══════════════════════════════════════════════════════════════
# 买侧在途台账 + 核销 (2026-09-17 计划书 §3.2-§3.8, §六 测试映射表)
# ═══════════════════════════════════════════════════════════════

def test_buy_junk_no_phantom_and_audited(tmp_path):
    """E3: 买入废单 (终态 57, filled=0) → 当轮扣回全部幻影; 扣回后该代码
    是真的低配 → 同一 pass 内按缺口重新下单 (自愈), 旧台账条目随扣减移除。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path, days=["friday"]), clock, cash=10_000.0))
    _seed_lots(app, {}, tranche_target=[NASDAQ])
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    app._rotation.on_signals({"signals": {0: {"target": NASDAQ,
                                              "insufficient": False}},
                              "source": "test"})
    buys, _ = _orders_by(app)
    assert len(buys) == 1 and buys[0]["code"] == NASDAQ
    oid = buys[0]["order_id"]
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == 5_000    # 乐观记账
    app.gateway.simulate_reject(oid)                           # 废单 (终态)
    app._rotation.on_signals({"signals": {}, "source": "test"})
    # 核销: 扣回全部 5 千 (旧台账条目随扣减同步移除)
    d = json.loads(_audit(app, "rotation_buy_settle")[0][0])
    assert d["order_id"] == oid and d["filled"] == 0
    assert d["reduce_by"] == 5_000 and d["before"] == 5_000
    assert oid not in _ledger(app)
    # 幻影扣掉后该代码真的低配 → 当轮重新下单 5 千 (新台账条目)
    buys2, _ = _orders_by(app)
    assert len(buys2) == 2
    new_oid = [b["order_id"] for b in buys2 if b["order_id"] != oid][0]
    assert set(_ledger(app)) == {new_oid}
    assert _ledger(app)[new_oid][2] == 5_000
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == 5_000


def test_buy_partial_fill_corrects_qty(tmp_path):
    """E4: 买入部成后撤 (状态 53) → 只扣「记录量 − filled_qty」,
    簿记 == 真实成交 (用新钩子 simulate_partial_cancel)。
    隔离买入侧: 本用例只验核销算术, 免去核销后当轮按缺口补买干扰观测点。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path, days=["friday"]), clock, cash=100_000.0))
    _seed_lots(app, {}, tranche_target=[NASDAQ])
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    app._rotation.on_signals({"signals": {0: {"target": NASDAQ,
                                              "insufficient": False}},
                              "source": "test"})
    buys, _ = _orders_by(app)
    assert len(buys) == 1 and buys[0]["qty"] == 50_000
    oid = buys[0]["order_id"]
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == 50_000
    app.gateway.simulate_fill(oid, qty=20_000)                # 部成 2 万
    app.gateway.simulate_partial_cancel(oid, filled=20_000)   # 部撤 (53)
    app._rotation._buy_lot = lambda *a, **k: (0.0, None)      # 隔离买入侧
    app._rotation.on_signals({"signals": {}, "source": "test"})
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == 20_000   # 幻影 3 万扣回
    assert _ledger(app) == {}
    d = json.loads(_audit(app, "rotation_buy_settle")[0][0])
    assert d["filled"] == 20_000 and d["reduce_by"] == 30_000


def test_buy_open_ledger_survives_restart_and_settles(tmp_path):
    """E5/E11: 当轮未终结的买单入台账; 重启后台账仍在 (同一 tmp_path 建第二个
    TradeApp), 终态/部成回报到了由下一轮开场核销 (自愈)。"""
    clock = [_ts("10:00")]
    cfg = _cfg(tmp_path, days=["friday"])
    app = _start(_app(cfg, clock, cash=10_000.0))
    _seed_lots(app, {}, tranche_target=[NASDAQ])
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    app._rotation.on_signals({"signals": {0: {"target": NASDAQ,
                                              "insufficient": False}},
                              "source": "test"})
    buys, _ = _orders_by(app)
    assert buys and buys[0]["qty"] == 5_000
    oid = buys[0]["order_id"]
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == 5_000      # 乐观记账
    assert oid in _ledger(app)                  # 当轮未终结 → 入台账 (E5)
    app.gateway.simulate_fill(oid, qty=2_000)   # 盘后成交 2 千
    app.gateway.simulate_partial_cancel(oid, filled=2_000)   # 随后部撤 → 终态 53
    leftover_cash = app.gateway.query_asset()["cash"]
    app.stop()

    # ── 重启: 同一库建第二个 TradeApp (台账与簿记都读回) ──
    clock2 = [_ts("10:00")]
    app2 = _start(_app(cfg, clock2, cash=leftover_cash,
                       positions={NASDAQ: {"volume": 2_000, "can_use": 0,
                                           "avg_cost": 1.0}}))
    assert oid in _ledger(app2)                 # 台账跨重启保留
    assert app2._rotation._lots[(0, NASDAQ)]["qty"] == 5_000     # 乐观值
    # QMT 重启后仍能返回该委托 (Fake 网关不会自动带过来, 手工注入部撤终态回报)
    app2.gateway._orders = {oid: {
        "order_id": oid, "code": NASDAQ, "direction": DIRECTION_BUY,
        "price": 1.0, "qty": 5_000, "filled_qty": 2_000,
        "status": 53, "ts": clock[0]}}
    app2.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    app2.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app2.monitor.quote_of(NASDAQ) is not None)
    app2._rotation._tranches[0]["pending_target"] = NASDAQ
    app2._rotation._tranches[0]["has_target"] = True
    app2._rotation._buy_lot = lambda *a, **k: (0.0, None)  # 隔离买入侧
    app2._rotation.on_signals({"signals": {}, "source": "test"})
    # 开场核销: 委托 5 千 / 实成 2 千 → 扣 3 千 → 簿记 == QMT 2 千
    assert app2._rotation._lots[(0, NASDAQ)]["qty"] == 2_000
    assert _ledger(app2) == {}
    assert _audit(app2, "rotation_buy_settle")
    # 核销后簿记与 QMT 一致 → 不再有解释不掉的漂移告警 (§3.5 口径)
    assert not _audit(app2, "rotation_lots_drift")


def test_buy_ledger_missing_order_kept_then_expires(tmp_path):
    """E6: 回报查不到 → 保留并计数 (每轮审计) 最多 5 轮; 超期移除 + 审计,
    **不扣簿记** (计划书 §3.4/M1)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path, days=["friday"]), clock, cash=10_000.0))
    _seed_lots(app, {}, tranche_target=[NASDAQ])
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    app._rotation.on_signals({"signals": {0: {"target": NASDAQ,
                                              "insufficient": False}},
                              "source": "test"})
    buys, _ = _orders_by(app)
    oid = buys[0]["order_id"]
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == 5_000
    # 直接改 gw._orders 模拟「QMT 查不到这笔委托」 (先例 test_reconciler.py:321)
    app.gateway._orders = {}
    app._rotation._buy_lot = lambda *a, **k: (0.0, None)   # 隔离买入侧
    # 保留期内: 每轮都在台账、计数递增、一分不扣 (到期前 5 轮)
    for n in range(1, 6):
        app._rotation.on_signals({"signals": {}, "source": "test"})
        rec = _ledger(app).get(oid)              # 保留在台账 (未超期)
        assert rec is not None, f"第 {n} 轮就被移除了"
        assert rec[5] == n                       # 查不到轮次计数
        assert app._rotation._lots[(0, NASDAQ)]["qty"] == 5_000    # 全程不扣
    assert len(_audit(app, "rotation_buy_ledger_missing")) == 5
    assert not _audit(app, "rotation_buy_ledger_expired")
    # 第 6 轮: 超期 → 移除 + 审计, 仍不扣
    app._rotation.on_signals({"signals": {}, "source": "test"})
    assert oid not in _ledger(app)
    assert _audit(app, "rotation_buy_ledger_expired")
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == 5_000        # 不扣


def test_buy_ledger_corrupt_is_audited_not_lost(tmp_path):
    """E7 (L2): 台账 JSON 损坏 → 写审计 + 备份 .bak, 不静默丢;
    单条格式坏 → 审计 + 原样保留 (不核销不丢)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path, days=["friday"]), clock, cash=10_000.0))
    _seed_lots(app, {}, tranche_target=[NASDAQ])
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    # ① 整体 JSON 坏
    app.store.rotation_meta.set("rotation_open_buys", "{不是JSON")
    app._rotation.on_signals({"signals": {}, "source": "test"})
    assert _audit(app, "rotation_open_ledger_corrupt")
    assert app.store.rotation_meta.get("rotation_open_buys.bak") == "{不是JSON"
    # ② 单条格式坏 (合法 JSON, 条目长度/类型不对)
    app.store.rotation_meta.set("rotation_open_buys",
                                json.dumps({"FAKE000001": ["坏"]}))
    app._rotation.on_signals({"signals": {}, "source": "test"})
    assert _audit(app, "rotation_buy_ledger_corrupt")
    assert "FAKE000001" in _ledger(app)          # 原样保留, 不静默丢
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == 5_000     # 不误扣


def test_settle_query_failure_keeps_ledger(tmp_path):
    """M2 (§3.4): 查询异常 ≠ 查不到 —— 立即 return, 台账与簿记都不动 +
    审计 rotation_settle_query_failed (一次超时不得清空台账)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path, days=["friday"]), clock, cash=10_000.0))
    _seed_lots(app, {}, tranche_target=[NASDAQ])
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    app._rotation.on_signals({"signals": {0: {"target": NASDAQ,
                                              "insufficient": False}},
                              "source": "test"})
    buys, _ = _orders_by(app)
    oid = buys[0]["order_id"]
    before = dict(_ledger(app))
    assert oid in before

    def _boom():
        raise RuntimeError("QMT 查询超时")
    app.gateway.query_orders = _boom
    app._rotation.on_signals({"signals": {}, "source": "test"})
    assert _ledger(app) == before                          # 台账一条不动
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == 5_000     # 一条不扣
    d = json.loads(_audit(app, "rotation_settle_query_failed")[0][0])
    assert d["side"] == "buy" and d["pending"] == 1


def test_buy_ledger_write_failure_never_deducts(tmp_path):
    """H2 (§3.2): 台账落库失败 → 不记账也不扣 (失败方向 = 账本偏多),
    只写审计, 绝不少记 (少记 → 补买超配)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path, days=["friday"]), clock, cash=10_000.0))
    _seed_lots(app, {}, tranche_target=[NASDAQ])
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)

    def _boom(*a, **k):
        raise RuntimeError("rotation_meta 写失败")
    app.store.rotation_meta.set = _boom
    app._rotation.on_signals({"signals": {0: {"target": NASDAQ,
                                              "insufficient": False}},
                              "source": "test"})
    buys, _ = _orders_by(app)
    assert len(buys) == 1                        # 单照下 (台账失败不改下单)
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == 5_000     # 乐观记账保留
    assert _audit(app, "rotation_buy_ledger_write_failed")


def test_two_pass_same_day_deducts_exactly_once(tmp_path):
    """E13 (H1 主锁): 同一交易日两轮 pass, 第一轮的单在第二轮中途转终态 ——
    **恰好扣一次**, pass 末整表替换不得残留导致再扣一次。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path, days=["friday"]), clock, cash=10_000.0))
    _seed_lots(app, {}, tranche_target=[NASDAQ])
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    # ── 第一轮: 下单 → 当轮未终结 → 入台账 + 乐观簿记 5 千 ──
    app._rotation.on_signals({"signals": {0: {"target": NASDAQ,
                                              "insufficient": False}},
                              "source": "test"})
    buys, _ = _orders_by(app)
    oid = buys[0]["order_id"]
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == 5_000
    assert oid in _ledger(app)
    # ── 两轮之间: 废单回报到 (状态推进, 但还没人核销) ──
    app.gateway.simulate_reject(oid)
    app._rotation._buy_lot = lambda *a, **k: (0.0, None)   # 隔离买入侧
    # ── 第二轮: 开场核销扣一次 (扣到 0 删行) ──
    app._rotation.on_signals({"signals": {}, "source": "test"})
    assert (0, NASDAQ) not in app._rotation._lots
    assert _ledger(app) == {}
    assert len(_audit(app, "rotation_buy_settle")) == 1
    # ── 第三轮: 台账已无该条 → 绝不能再扣 (H1: 跨轮残留会双扣 → 补买) ──
    app._rotation.on_signals({"signals": {}, "source": "test"})
    assert (0, NASDAQ) not in app._rotation._lots
    assert _ledger(app) == {}
    assert len(_audit(app, "rotation_buy_settle")) == 1


def test_settle_never_increases_lot_and_blocks_topup(tmp_path):
    """E14 (§3.7 闸): 簿记永不因核销增加; 且 Σ份簿记 < QMT volume 时
    该代码本轮**不补买** + 审计 rotation_lots_understated (fail-closed:
    少记时宁可不买, 绝不补买超配)。"""
    clock = [_ts("10:00")]
    # 极端构造: 票已拿满 25 万份, 台账却报「委托 40 万 / 成交 40 万」
    # (畸形或跨会话回报), 若核销实现成「按成交补记」就会超配 —— 锁死绝不会。
    app = _start(_app(_cfg(tmp_path, days=["friday"]), clock,
                      cash=0,       # 现金 0 → 隔离买入侧, 只看核销方向
                      positions={NASDAQ: {"volume": 250_000,
                                          "can_use": 0,
                                          "avg_cost": 1.0}}))
    _seed_lots(app, {(0, NASDAQ): {"qty": 250_000, "entry_high": 1.0}},
               tranche_target=[NASDAQ])
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    app.store.rotation_meta.set("rotation_open_buys", json.dumps(
        {"FAKEX1": [0, NASDAQ, 400_000, 0, clock[0]]}))
    app.gateway._orders = {"FAKEX1": {
        "order_id": "FAKEX1", "code": NASDAQ, "direction": DIRECTION_BUY,
        "price": 1.0, "qty": 400_000, "filled_qty": 400_000,
        "status": 56, "ts": clock[0]}}
    app._rotation.on_signals({"signals": {}, "source": "test"})
    # 记录量−成交量 = 0 → 不扣; 且绝不增加
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == 250_000
    assert _ledger(app) == {}
    # ── 少记 (簿记 5 万 < QMT 25 万) → 该代码本轮不补买 + 审计 ──
    app.gateway._orders = {}          # 清掉上面注入的畸形单, 隔离本段
    _seed_lots(app, {(0, NASDAQ): {"qty": 50_000, "entry_high": 1.0}},
               tranche_target=[NASDAQ])
    app.gateway._cash = 500_000.0     # 有钱: 若没闸就会补买
    app._rotation.on_signals({"signals": {0: {"target": NASDAQ,
                                              "insufficient": False}},
                              "source": "test"})
    assert _audit(app, "rotation_lots_understated")
    buys, _ = _orders_by(app)
    assert not [b for b in buys if b["code"] == NASDAQ]     # 不补买
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == 50_000  # 簿记不变
    # 文案必须给**可执行**的人工修账入口 + 说明重迁的后果
    # (第一轮审计必修-2 + 第二轮审计必修: 文案不得是"照做不生效"的空指引)
    msg = _audit_msg(app, "rotation_lots_understated")[0]
    assert "rotation_lots" in msg and "lots_initialized" in msg
    assert "非目标腿" in msg
    assert "先停 trade_main" in msg          # 运行中改库会被内存镜像覆盖
    assert "全部行" in msg                    # 表里有行 → 清标志不触发迁移


def test_understated_persistent_escalation(tmp_path):
    """必修-2: 同一代码连续 N=3 轮簿记少记 → 升级审计 (另起 kind), 计数落
    rotation_meta KV; 恢复正常即清零, 下一段持续期可再次升级。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path, days=["friday"]), clock, cash=0,
                      positions={NASDAQ: {"volume": 250_000, "can_use": 0,
                                          "avg_cost": 1.0}}))
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    _seed_lots(app, {(0, NASDAQ): {"qty": 50_000, "entry_high": 1.0}},
               tranche_target=[NASDAQ])
    streak_key = "rotation_understated_streak"
    for n in (1, 2):
        app._rotation.on_signals({"signals": {0: {"target": NASDAQ,
                                                  "insufficient": False}},
                                  "source": "test"})
        assert json.loads(app.store.rotation_meta.get(streak_key)) == {
            NASDAQ: n}
        assert not _audit(app, "rotation_lots_understated_persistent")
    app._rotation.on_signals({"signals": {}, "source": "test"})     # 第 3 轮
    assert json.loads(app.store.rotation_meta.get(streak_key)) == {NASDAQ: 3}
    assert _audit(app, "rotation_lots_understated_persistent")      # 升级
    assert len(_audit(app, "rotation_lots_understated")) == 3       # 基础每轮都在
    assert len(_audit(app, "rotation_lots_understated_persistent")) == 1
    # 恢复正常 (簿记 == QMT) → 计数清零, 且不重复升级
    _seed_lots(app, {(0, NASDAQ): {"qty": 250_000, "entry_high": 1.0}},
               tranche_target=[NASDAQ])
    app._rotation.on_signals({"signals": {}, "source": "test"})
    assert json.loads(app.store.rotation_meta.get(streak_key) or "{}") == {}
    assert len(_audit(app, "rotation_lots_understated_persistent")) == 1
    # 升级文案也必须带**可执行**指引 (第二轮审计: 三份指引曾各写一份且第三份漏改)
    esc = _audit_msg(app, "rotation_lots_understated_persistent")[0]
    assert "先停 trade_main" in esc and "全部行" in esc and "非目标腿" in esc


def test_buy_settle_no_lot_row_audited(tmp_path):
    """E8 (必修-3①): 核销时份内**无对应簿记行** (份数热变更 / 该行已被卖侧
    扣到 0 删行) → 不扣 + 写 `rotation_buy_ledger_no_lot` + 条目按规则移除。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path, days=["friday"]), clock, cash=10_000.0))
    _seed_lots(app, {}, tranche_target=[NASDAQ])
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    app._rotation.on_signals({"signals": {0: {"target": NASDAQ,
                                              "insufficient": False}},
                              "source": "test"})
    buys, _ = _orders_by(app)
    oid = buys[0]["order_id"]
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == 5_000
    app.gateway.simulate_reject(oid)          # 终态: 本该扣回 5 千
    app._rotation._lots = {}                  # 簿记行已不在 (被卖侧扣到 0 删行)
    app._rotation._buy_lot = lambda *a, **k: (0.0, None)   # 隔离买入侧
    app._rotation.on_signals({"signals": {}, "source": "test"})
    assert not app._rotation._lots                       # 无行可扣 → 不凭空造行
    d = json.loads(_audit(app, "rotation_buy_ledger_no_lot")[0][0])
    assert d["order_id"] == oid and d["reduce_by"] == 5_000
    assert oid not in _ledger(app)                       # 条目按规则移除
    # 没有 lot 行 → 不写"已扣"的核销审计 (那条只在真扣了内存时写)
    assert not _audit(app, "rotation_buy_settle")


def test_buy_settle_order_mismatch_skipped(tmp_path):
    """M3 (必修-3②): 回报与台账**代码或方向**不符 (QMT 单号复用) →
    跳过不扣 + 条目**保留** + 写 `rotation_buy_ledger_mismatch`。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path, days=["friday"]), clock, cash=10_000.0))
    _seed_lots(app, {}, tranche_target=[NASDAQ])
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    app._rotation.on_signals({"signals": {0: {"target": NASDAQ,
                                              "insufficient": False}},
                              "source": "test"})
    buys, _ = _orders_by(app)
    oid = buys[0]["order_id"]
    qty = app._rotation._lots[(0, NASDAQ)]["qty"]
    app.gateway.simulate_reject(oid)                  # 终态 (本该扣)
    # ① 代码不符: 台账说 GOLD, 回报是 NASDAQ (同一 order_id 被复用)
    led = _ledger(app)
    led[oid][1] = GOLD
    app.store.rotation_meta.set("rotation_open_buys", json.dumps(led))
    app._rotation.on_signals({"signals": {}, "source": "test"})
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == qty    # 不扣
    assert oid in _ledger(app)                               # 条目保留
    d = json.loads(_audit(app, "rotation_buy_ledger_mismatch")[0][0])
    assert "代码" in d["mismatch"]
    # ② 方向不符: 台账代码改回 NASDAQ, 但回报方向变成卖出
    led = _ledger(app)
    led[oid][1] = NASDAQ
    app.store.rotation_meta.set("rotation_open_buys", json.dumps(led))
    app.gateway._orders[oid]["direction"] = DIRECTION_SELL
    app._rotation.on_signals({"signals": {}, "source": "test"})
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == qty    # 仍不扣
    assert oid in _ledger(app)                               # 条目仍保留
    msgs = _audit(app, "rotation_buy_ledger_mismatch")
    assert len(msgs) == 2
    assert "方向" in json.loads(msgs[1][0])["mismatch"]
    assert not _audit(app, "rotation_buy_settle")            # 全程没扣过


def test_inflight_qty_skips_order_id_reuse(tmp_path):
    """第二轮审计 LOW-2: 在途量归集补上核销路径同款的三项校验 —— 回报的
    代码与台账不符 (QMT 单号被复用成另一张单) 时**不计入在途量**, 否则会把
    别的单的量算到该代码头上, 在漂移对账里造出假告警 (噪声方向)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path, days=["friday"]), clock, cash=10_000.0))
    _seed_lots(app, {}, tranche_target=[NASDAQ])
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    app._rotation.on_signals({"signals": {0: {"target": NASDAQ,
                                              "insufficient": False}},
                              "source": "test"})
    buys, _ = _orders_by(app)
    oid = buys[0]["order_id"]
    # 台账说这笔是 NASDAQ 的在途买单 → 正常计入 (现金 1 万 → 买 5 千份, 与同文件其他用例同口径)
    assert app._rotation._inflight_buy_qty() == {NASDAQ: 5_000}
    # 回报被改成别的代码 (单号复用) → 该单不再计入任何代码的在途量
    app.gateway._orders[oid]["code"] = GOLD
    assert app._rotation._inflight_buy_qty() == {}
    # 方向不符 → 同样不计入
    app.gateway._orders[oid]["code"] = NASDAQ
    app.gateway._orders[oid]["direction"] = DIRECTION_SELL
    assert app._rotation._inflight_buy_qty() == {}


def test_migrate_clears_both_ledgers_with_audit(tmp_path):
    """E9 (§3.6): 迁移清空两侧在途台账键 + 逐条审计列出被丢弃条目。"""
    clock = [_ts("10:00")]
    # 现金/持仓非 0: _execute 对「总资产为 0」直接早退 (迁移根本不跑)
    app = _start(_app(_cfg(tmp_path, days=["friday"]), clock, cash=0,
                      positions={NASDAQ: {"volume": 150, "can_use": 150,
                                          "avg_cost": 1.0}}))
    # 两个 QMT 查不到的买单条目 → 核销期保留在台账 → 迁移时被清空并逐条审计
    app.store.rotation_meta.set("rotation_open_buys", json.dumps(
        {"FAKEB1": [0, NASDAQ, 100, 0, clock[0]],
         "FAKEB2": [0, GOLD, 200, 0, clock[0]]}))
    app.store.rotation_meta.set("rotation_open_sells", json.dumps({}))
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    app._rotation.on_signals({"signals": {}, "source": "test"})   # 空仓首跑 → 迁移
    assert app.store.rotation_meta.get("lots_initialized") == "1"
    assert _ledger(app, "rotation_open_buys") == {}
    assert _ledger(app, "rotation_open_sells") == {}
    rows = _audit(app, "rotation_migrate_discard")
    assert rows, "迁移丢弃在途条目必须逐条留审计"
    detail = json.loads(rows[0][0])["discarded"]
    assert len(detail) == 2                          # 逐条, 不合并
    assert any("FAKEB1" in x for x in detail)
    assert any("FAKEB2" in x for x in detail)


def test_same_code_two_buys_one_junk(tmp_path):
    """E10: 同日同代码两份各下一笔买入, 其中一单废单 → 按 order_id 独立核销,
    只扣废单那笔 (另一份簿记与另一条台账不受影响)。"""
    clock = [_ts("10:00")]
    # 两份 (锚定日不同) → 同一代码同日各下一笔 (计划书 §3.3: 分单不并单)
    app = _start(_app(_cfg(tmp_path, days=["wednesday", "friday"]), clock,
                      cash=100_000.0))
    _seed_lots(app, {}, tranche_target=[NASDAQ, NASDAQ])
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    app._rotation.on_signals({"signals": {0: {"target": NASDAQ,
                                              "insufficient": False},
                                          1: {"target": NASDAQ,
                                              "insufficient": False}},
                              "source": "test"})
    buys, _ = _orders_by(app)
    assert len(buys) == 2                            # 分单不并单 (一份一笔)
    oid1, oid2 = [b["order_id"] for b in buys]
    assert {oid1, oid2} == set(_ledger(app))         # 两笔都在台账
    q1, q2 = _ledger(app)[oid1][2], _ledger(app)[oid2][2]
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == q1
    assert app._rotation._lots[(1, NASDAQ)]["qty"] == q2
    app.gateway.simulate_reject(oid2)                # 只有第二笔废单
    app._rotation._buy_lot = lambda *a, **k: (0.0, None)   # 隔离买入侧
    app._rotation.on_signals({"signals": {}, "source": "test"})
    assert (1, NASDAQ) not in app._rotation._lots    # 废单那笔扣到 0 → 删行
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == q1   # 好那笔一分不动
    # 废单那笔台账条目随扣减移除; 仍在途那笔 (oid1, 状态 50) 继续留着
    assert oid2 not in _ledger(app) and oid1 in _ledger(app)
    assert _ledger(app)[oid1][2] == q1
    settles = _audit(app, "rotation_buy_settle")
    assert len(settles) == 1
    d = json.loads(settles[0][0])
    assert d["order_id"] == oid2 and d["reduce_by"] == q2


def test_reconcile_counts_inflight_buy_as_explained(tmp_path):
    """H4/§3.5: 对账期望值 = QMT volume + Σ在途买单量 —— 在途买单能解释掉的
    差额**不告警**, 解释不掉的才告警 (文案说明已计入在途)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path, days=["friday"]), clock, cash=10_000.0))
    _seed_lots(app, {}, tranche_target=[NASDAQ])
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    app._rotation.on_signals({"signals": {0: {"target": NASDAQ,
                                              "insufficient": False}},
                              "source": "test"})
    buys, _ = _orders_by(app)
    oid = buys[0]["order_id"]                 # 委托 5 千份, 未成交 → 在途
    assert oid in _ledger(app)
    assert app._rotation._lots[(0, NASDAQ)]["qty"] == 5_000
    assert app._rotation._inflight_buy_qty() == {NASDAQ: 5_000}
    # QMT 还没入账这笔 (volume 0) + 在途 5 千 = 期望值 5 千 == 簿记 → 不告警
    # (旧口径: 期望值 = QMT volume 0 → 必然误报「本轮买过就报警」)
    app._rotation._reconcile_lots({}, [NASDAQ])
    assert not _audit(app, "rotation_lots_drift")
    # 解释不掉的差额 (QMT 只有 100 份) 仍然告警, 且文案说明已计入在途
    app._rotation._reconcile_lots({NASDAQ: {"code": NASDAQ,
                                            "volume": 100}}, [NASDAQ])
    rows = _audit(app, "rotation_lots_drift")
    assert rows
    d = json.loads(rows[0][0])
    assert d[NASDAQ]["inflight_buy"] == 5_000      # detail 带在途买单量
    assert "非终态买单" in _audit_msg(app, "rotation_lots_drift")[0]
    # 漂移文案同样给**可执行**修账指引 (第二轮审计: 三份指引收口同一常量, 逐份锁死)
    drift_msg = _audit_msg(app, "rotation_lots_drift")[0]
    assert "先停 trade_main" in drift_msg
    assert "全部行" in drift_msg
    assert "非目标腿" in drift_msg
