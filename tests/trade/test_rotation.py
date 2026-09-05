"""ETF 轮动 + 双池资金分配测试 (2026-08-14; 2026-08-20 动量改造重写).

锁住: 动量信号纯函数 (择腿/全≤0避险/数据不足)、周频信号日判定、
动量目标市值拆分、config 新字段校验、首买/换档/补仓的订单生成、
日频移动止损、can_use 回填回归、股票池预算帽。
"""
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.book import DIRECTION_BUY, DIRECTION_SELL
from trade.config import PositionSizingConfig, RotationConfig, TradeConfig, load_trade_config
from trade.risk import OrderIntent
from trade.rotation import (
    _is_signal_day,
    compute_momentum_signal,
    momentum_target_values,
)
from trade_main import TradeApp

CYB = "159949.SZ"
GOLD = "518880.SH"
NASDAQ = "513100.SH"     # 风险腿2 (纳指)
BOND = "511010.SH"       # 避险腿2 (国债ETF, 测试用)
STOCK = "000001.SZ"


def _ts(hhmm):
    from datetime import datetime, timedelta
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


def _cfg(tmp_path, **rot):
    base = {"enabled": True, "etf_ratio": 0.5}
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


# ═══════════════════════════════════════════════════════════════
# 动量信号纯函数
# ═══════════════════════════════════════════════════════════════

def test_momentum_signal_cyb_wins():
    closes = {CYB: [1.0] * 21 + [1.2], NASDAQ: [1.0] * 21 + [0.8]}
    s = compute_momentum_signal(closes)
    assert s["target"] == CYB
    assert s["insufficient"] is False
    assert s["momentum"][CYB] == pytest.approx(0.2, abs=1e-3)
    assert s["momentum"][NASDAQ] == pytest.approx(-0.2, abs=1e-3)


def test_momentum_signal_nasdaq_wins():
    closes = {CYB: [1.0] * 21 + [1.05], NASDAQ: [1.0] * 21 + [1.30]}
    s = compute_momentum_signal(closes)
    assert s["target"] == NASDAQ


def test_momentum_signal_all_negative_hedge():
    closes = {CYB: [1.0] * 21 + [0.9], NASDAQ: [1.0] * 21 + [0.95]}
    s = compute_momentum_signal(closes)
    assert s["target"] is None
    assert s["insufficient"] is False
    assert s["reason"] == "两腿动量均 ≤ 0"


def test_momentum_signal_insufficient():
    closes = {CYB: [1.0] * 20, NASDAQ: []}     # 都 ≤ momentum_window 根
    s = compute_momentum_signal(closes)
    assert s["target"] is None
    assert s["insufficient"] is True
    assert "reason" in s


def test_momentum_signal_one_leg_insufficient():
    # 一腿数据不足 (不参与择腿), 另一腿有动量 → 选有动量的腿
    closes = {CYB: [1.0] * 10, NASDAQ: [1.0] * 21 + [1.1]}
    s = compute_momentum_signal(closes)
    assert s["target"] == NASDAQ
    assert s["momentum"][CYB] is None


def test_momentum_target_values():
    pool = 1_000_000.0
    # 择创业板: 创业板满池, 纳指/黄金 0
    assert momentum_target_values(CYB, NASDAQ, GOLD, "", 1.0, CYB, pool) == \
        [(CYB, pool), (NASDAQ, 0.0), (GOLD, 0.0)]
    # 择纳指
    assert momentum_target_values(CYB, NASDAQ, GOLD, "", 1.0, NASDAQ, pool) == \
        [(CYB, 0.0), (NASDAQ, pool), (GOLD, 0.0)]
    # 全避险单黄金
    assert momentum_target_values(CYB, NASDAQ, GOLD, "", 1.0, None, pool) == \
        [(CYB, 0.0), (NASDAQ, 0.0), (GOLD, pool)]
    # 全避险双避险各半 (黄金 50% + 国债 50%)
    assert momentum_target_values(CYB, NASDAQ, GOLD, BOND, 0.5, None, pool) == \
        [(CYB, 0.0), (NASDAQ, 0.0), (GOLD, pool * 0.5), (BOND, pool * 0.5)]
    # 单风险腿退化 (risk_etf2 空)
    assert momentum_target_values(CYB, "", GOLD, "", 1.0, CYB, pool) == \
        [(CYB, pool), (GOLD, 0.0)]
    assert momentum_target_values(CYB, "", GOLD, "", 1.0, None, pool) == \
        [(CYB, 0.0), (GOLD, pool)]
    # hedge_etf2 空 + hedge_ratio≠1 → 归一化回单黄金
    assert momentum_target_values(CYB, NASDAQ, GOLD, "", 0.5, None, pool) == \
        [(CYB, 0.0), (NASDAQ, 0.0), (GOLD, pool)]


# ═══════════════════════════════════════════════════════════════
# 周频信号日判定
# ═══════════════════════════════════════════════════════════════

def test_is_signal_day(monkeypatch):
    monkeypatch.setattr("trade.rotation.is_trading_day_cached", lambda d: True)

    def _nxt(d):
        d = d + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d
    monkeypatch.setattr("trade.rotation.next_trading_day", _nxt)

    fri = date(2026, 8, 21)   # 周五
    mon = date(2026, 8, 17)   # 周一
    thu = date(2026, 8, 20)   # 周四
    assert _is_signal_day(fri, "friday") is True
    assert _is_signal_day(mon, "friday") is False
    assert _is_signal_day(thu, "friday") is False   # 周四后还有周五 → 非最后
    assert _is_signal_day(mon, "monday") is True
    assert _is_signal_day(fri, "monday") is False   # 已过周一锚定


def test_is_signal_day_friday_holiday(monkeypatch):
    """周五休市 → 周四成为每周最后一个交易日 ≤ 周五 (计划书 §八.3)。"""
    fri = date(2026, 8, 21)
    thu = date(2026, 8, 20)

    def _trading(d):
        return d != fri
    monkeypatch.setattr("trade.rotation.is_trading_day_cached", _trading)

    def _nxt(d):
        d = d + timedelta(days=1)
        while d.weekday() >= 5 or not _trading(d):
            d += timedelta(days=1)
        return d
    monkeypatch.setattr("trade.rotation.next_trading_day", _nxt)

    assert _is_signal_day(thu, "friday") is True
    assert _is_signal_day(fri, "friday") is False


# ═══════════════════════════════════════════════════════════════
# config 校验
# ═══════════════════════════════════════════════════════════════

def test_rotation_config_defaults():
    c = TradeConfig()
    assert c.rotation.enabled is False           # 未显式配置不开 (fail-safe)
    assert c.rotation.etf_ratio == 0.5
    assert c.rotation.risk_etf2 == "513100.SH"
    assert c.rotation.momentum_window == 20
    assert c.rotation.trailing_stop_pct == 0.15
    assert c.rotation.signal_day == "friday"


def test_rotation_config_validation(tmp_path):
    def _load(text):
        p = tmp_path / "c.yaml"
        p.write_text(text, encoding="utf-8")
        return load_trade_config(p)
    cfg = _load("rotation:\n  enabled: true\n  etf_ratio: 0.3\n"
                "  execute_time: '10:00'\n")
    assert cfg.rotation.enabled and cfg.rotation.etf_ratio == 0.3
    with pytest.raises(ValueError, match="etf_ratio"):
        _load("rotation:\n  etf_ratio: 0.0\n")     # 0 不合法 (单池退化)
    with pytest.raises(ValueError, match="etf_ratio"):
        _load("rotation:\n  etf_ratio: 1.0\n")     # 1 不合法
    with pytest.raises(ValueError, match="HH:MM"):
        _load("rotation:\n  execute_time: '25:99'\n")
    # 新增字段校验 (2026-08-20 动量改造)
    with pytest.raises(ValueError, match="risk_etf2"):
        _load("rotation:\n  risk_etf2: '999'\n")          # 非 6 位代码
    with pytest.raises(ValueError, match="risk_etf2"):
        _load("rotation:\n  risk_etf2: '518880.SH'\n")     # 与 gold_etf 重复
    with pytest.raises(ValueError, match="momentum_window"):
        _load("rotation:\n  momentum_window: 1\n")          # < 2
    with pytest.raises(ValueError, match="trailing_stop_pct"):
        _load("rotation:\n  trailing_stop_pct: 0.0\n")      # 不在 (0,1)
    with pytest.raises(ValueError, match="trailing_stop_pct"):
        _load("rotation:\n  trailing_stop_pct: 1.5\n")
    with pytest.raises(ValueError, match="signal_day"):
        _load("rotation:\n  signal_day: 'sunday'\n")        # 非法枚举
    # 废弃字段 (MA20 规则专用) → 未知字段报错
    with pytest.raises(ValueError, match="未知字段"):
        _load("rotation:\n  signal_index: '399673.SZ'\n")
    with pytest.raises(ValueError, match="未知字段"):
        _load("rotation:\n  ma_window: 20\n")
    # 第二避险ETF + 比例 (2026-08-18, 回归保持)
    cfg2 = _load("rotation:\n  hedge_etf2: '511010.SH'\n  hedge_ratio: 0.5\n")
    assert cfg2.rotation.hedge_etf2 == "511010.SH"
    assert cfg2.rotation.hedge_ratio == 0.5
    with pytest.raises(ValueError, match="hedge_ratio"):
        _load("rotation:\n  hedge_ratio: 1.5\n")        # 超 [0,1]
    with pytest.raises(ValueError, match="hedge_ratio"):
        _load("rotation:\n  hedge_ratio: 0.5\n")         # hedge_etf2 空时非 1.0 (审计①)
    with pytest.raises(ValueError, match="hedge_etf2"):
        _load("rotation:\n  hedge_etf2: '999'\n")        # 非 6 位代码


# ═══════════════════════════════════════════════════════════════
# 调仓订单生成
# ═══════════════════════════════════════════════════════════════

def test_first_buy_cyb(tmp_path):
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock))
    app.gateway.push_quote(CYB, _quote(CYB, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0))
    assert _wait(lambda: app.monitor.quote_of(CYB) is not None)
    app._rotation.on_signals({"signal": {"target": CYB}, "source": "test"})
    buys, sells = _orders_by(app)
    assert len(sells) == 0
    assert [b["code"] for b in buys] == [CYB]
    # pool = 0.5 × 1,000,000 = 500,000; 价 1.0 → 500,000 份
    assert buys[0]["qty"] == 500_000


def test_swap_cyb_to_hedge(tmp_path):
    """换档卖单未成交 → 只卖不买 (池级预算帽归零, 不超买不花股票池现金)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=500_000,
                      positions={CYB: {"volume": 500_000, "can_use": 500_000,
                                       "avg_cost": 1.0}}))
    app.gateway.push_quote(CYB, _quote(CYB, 1.0, bid=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(CYB) is not None)
    app._rotation.on_signals({"signal": {"target": None}, "source": "test"})
    buys, sells = _orders_by(app)
    assert [s["code"] for s in sells] == [CYB]
    assert sells[0]["qty"] == 500_000               # 全清创业板
    assert buys == []                               # 卖未成交 → 不超买 (审计 C2)


def test_swap_cyb_to_risk_etf2(tmp_path):
    """风险腿 A→B 换档: 卖创业板, 卖未成交不买纳指。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=500_000,
                      positions={CYB: {"volume": 500_000, "can_use": 500_000,
                                       "avg_cost": 1.0}}))
    app.gateway.push_quote(CYB, _quote(CYB, 1.0, bid=1.0))
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    assert _wait(lambda: app.monitor.quote_of(CYB) is not None)
    app._rotation.on_signals({"signal": {"target": NASDAQ}, "source": "test"})
    buys, sells = _orders_by(app)
    assert [s["code"] for s in sells] == [CYB]
    assert buys == []                               # 卖未成交 → 不买纳指


def test_swap_sell_refreshes_stale_can_use(tmp_path):
    """2026-08-19 (159949 事件续): 轮动卖出前须从 QMT 回填 can_use。

    账本 book 的 can_use 在买入时不动 (T+1 设计), 昨尾盘买入的票
    book.can_use 陈旧 (少了当日解禁量), 风控闸4(t1_sellable) 用旧值
    误拒满仓避险的清仓卖单。修复后 _execute 先把 QMT 的 can_use 刷进
    book, 清仓卖单应正常生成。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=500_000,
                      positions={CYB: {"volume": 274700, "can_use": 274700,
                                       "avg_cost": 1.0}}))
    app.gateway.push_quote(CYB, _quote(CYB, 1.0, bid=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(CYB) is not None)
    # 模拟账本 can_use 陈旧: QMT 已全解禁(274700), book 还停在 263200
    app.book.set_can_use(CYB, 263200)
    app._rotation.on_signals({"signal": {"target": None}, "source": "test"})
    buys, sells = _orders_by(app)
    assert [s["code"] for s in sells] == [CYB]
    assert sells[0]["qty"] == 274700               # 回填后按 QMT 全量清仓, 不再被误拒


def test_swap_sell_filled_then_buy_next_run(tmp_path):
    """换档卖单成交后, 再跑一轮 → 按池级预算补买黄金 (状态派生自持仓, 自愈)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=500_000,
                      positions={CYB: {"volume": 500_000, "can_use": 500_000,
                                       "avg_cost": 1.0}}))
    app.gateway.push_quote(CYB, _quote(CYB, 1.0, bid=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(CYB) is not None)
    # 第一轮: 卖创业板 (未成交, 不买)
    app._rotation.on_signals({"signal": {"target": None}, "source": "test"})
    _, sells = _orders_by(app)
    assert sells and sells[0]["qty"] == 500_000
    # 模拟成交: 创业板清仓、现金回笼到 100 万
    app.gateway.simulate_fill(sells[0]["order_id"])
    # 第二轮: 卖已成交 → 池级缺口 50 万 → 补买黄金
    app._rotation.on_signals({"signal": {"target": None}, "source": "test"})
    buys, _ = _orders_by(app)
    gold_buys = [b for b in buys if b["code"] == GOLD]
    assert gold_buys and gold_buys[0]["qty"] == 250_000  # 50万 @2.0


def test_two_hedge_split_first_buy(tmp_path):
    """两只避险腿各半: 满仓避险首买 → 黄金/国债各买一半池。"""
    clock = [_ts("10:00")]
    cfg = _cfg(tmp_path, hedge_etf2=BOND, hedge_ratio=0.5)
    app = _start(_app(cfg, clock))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    app.gateway.push_quote(BOND, _quote(BOND, 1.0, bid=1.0, ask=1.0))
    assert _wait(lambda: app.monitor.quote_of(GOLD) is not None)
    app._rotation.on_signals({"signal": {"target": None}, "source": "test"})
    buys, sells = _orders_by(app)
    assert len(sells) == 0
    # pool = 50 万; 黄金 25 万 @2.0 = 125,000 份; 国债 25 万 @1.0 = 250,000 份
    gold_b = [b for b in buys if b["code"] == GOLD]
    bond_b = [b for b in buys if b["code"] == BOND]
    assert gold_b and gold_b[0]["qty"] == 125_000
    assert bond_b and bond_b[0]["qty"] == 250_000


def test_topup_same_target(tmp_path):
    """目标不变但 ETF 池低配 → 只补仓不卖 (模型 B 无主动卖)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=750_000,
                      positions={CYB: {"volume": 250_000, "can_use": 250_000,
                                       "avg_cost": 1.0}}))
    app.gateway.push_quote(CYB, _quote(CYB, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0))
    assert _wait(lambda: app.monitor.quote_of(CYB) is not None)
    app._rotation.on_signals({"signal": {"target": CYB}, "source": "test"})
    buys, sells = _orders_by(app)
    assert len(sells) == 0                           # 不主动卖
    assert [b["code"] for b in buys] == [CYB]
    assert buys[0]["qty"] == 250_000                 # 补到 50 万


def test_overweight_target_leg_no_trim(tmp_path):
    """模型B回归 (2026-08-25 纳指削腿事件): 目标腿超配 → 不削不卖, 漂移被接受。

    2026-08-20 动量改造(c08b43b)误删 state_changed 守卫后, 每日 _execute 会把
    超出池上限的当前目标腿削回比例 (8-25 实盘: 纳指腿市值 527,140 > 池 523,204,
    被削 1700 股) — 违反模型B铁律「绝不主动卖来凑比例, 比例是上限不是强制目标」。
    修复后: 超配只影响买入侧 (pool_gap=0 → 不补买), 卖出零动作。"""
    clock = [_ts("10:00")]
    # 总资产 = 现金 50万 + 纳指市值 55万 = 105万 → 池 = 52.5万 < 持仓 55万 (超配)
    app = _start(_app(_cfg(tmp_path), clock, cash=500_000,
                      positions={NASDAQ: {"volume": 550_000, "can_use": 550_000,
                                          "avg_cost": 1.0}}))
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(CYB, _quote(CYB, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(NASDAQ) is not None)
    app._rotation.on_signals({"signal": {"target": NASDAQ}, "source": "test"})
    buys, sells = _orders_by(app)
    assert sells == []    # 模型B: 超配不削 (修复前会卖 25,000 份凑回池)
    assert buys == []     # pool_gap = max(0, 52.5万-55万) = 0 → 不补买


def test_rotation_order_records_decision(tmp_path):
    """2026-08-21: 交易记录带详细决策原因 (动量数据 + 判断依据)。"""
    import json
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=500_000,
                      positions={GOLD: {"volume": 250_000, "can_use": 250_000,
                                        "avg_cost": 2.0}}))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0))
    app.gateway.push_quote(CYB, _quote(CYB, 1.0, bid=1.0, ask=1.0))
    assert _wait(lambda: app.monitor.quote_of(GOLD) is not None)
    # 信号日择腿: 目标 cyb (带动量数据), 从避险(黄金)切到 cyb
    app._rotation.on_signals({"signal": {
        "target": CYB, "insufficient": False,
        "momentum": {CYB: 0.05, NASDAQ: 0.03}}, "source": "test"})
    # 卖出黄金的 rotation_order audit 应带 decision + 动量明细
    ro = app.store.open_readonly()
    try:
        rows = ro.execute(
            "SELECT detail_json FROM audit WHERE kind='rotation_order' "
            "AND detail_json LIKE '%动量择腿%'").fetchall()
    finally:
        ro.close()
    assert rows, "rotation_order 审计应带决策原因"
    d = json.loads(rows[0][0])
    assert d["decision"].startswith("动量择腿")
    assert "159949.SZ +5.0%" in d["decision"]
    assert "513100.SH +3.0%" in d["decision"]
    # 卖出黄金的成交原因 (fill_context.detail) 也带决策原因, 进 trades.reason
    _, sells = _orders_by(app)
    assert sells, "应有一笔卖出黄金"
    oid = sells[0]["order_id"]
    detail = (app.executor.peek_fill_context(oid) or {}).get("detail", "")
    assert "ETF轮动卖出" in detail and "动量择腿" in detail


# ═══════════════════════════════════════════════════════════════
# 日频移动止损
# ═══════════════════════════════════════════════════════════════

def test_trailing_stop_switches_to_hedge(tmp_path):
    """持仓期最高 1.0, 现价 0.80 回撤 20% > 15% → 止损切避险 (清仓风险腿)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=500_000,
                      positions={CYB: {"volume": 500_000, "can_use": 500_000,
                                       "avg_cost": 1.0}}))
    app.gateway.push_quote(CYB, _quote(CYB, 0.80, bid=0.80))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(CYB) is not None)
    app._rotation._entry_high = {CYB: 1.0}
    app._rotation.on_signals({"signal": {"target": CYB}, "source": "test"})
    buys, sells = _orders_by(app)
    assert [s["code"] for s in sells] == [CYB]
    assert sells[0]["qty"] == 500_000               # 止损 → 清仓风险腿
    assert app._rotation._pending_target is None    # §八.4: 清空 pending_target


def test_trailing_stop_not_triggered(tmp_path):
    """现价 0.90 回撤 10% < 15% → 不止损 (无卖单)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=500_000,
                      positions={CYB: {"volume": 500_000, "can_use": 500_000,
                                       "avg_cost": 1.0}}))
    app.gateway.push_quote(CYB, _quote(CYB, 0.90, bid=0.90, ask=0.90))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0))
    assert _wait(lambda: app.monitor.quote_of(CYB) is not None)
    app._rotation._entry_high = {CYB: 1.0}
    app._rotation.on_signals({"signal": {"target": CYB}, "source": "test"})
    _, sells = _orders_by(app)
    assert sells == []                              # 未触发止损 → 不卖


# ═══════════════════════════════════════════════════════════════
# 工作线程 (周频信号 / 日频执行)
# ═══════════════════════════════════════════════════════════════

def test_worker_signal_day_executes_immediately(monkeypatch, tmp_path):
    """信号日: 算动量 → **当日尾盘直接执行** (T 日执行, 非 T+1 次日)。"""
    monkeypatch.setattr("trade.rotation.is_trading_day_cached", lambda d: True)
    monkeypatch.setattr("trade.rotation._is_signal_day", lambda d, sd: True)
    monkeypatch.setattr("trade.monitor.is_trading_day_cached", lambda d: True)
    clock = [_ts("10:00")]
    closes = {CYB: [1.0] * 25, NASDAQ: [1.0] * 25}
    app = _start(_app(_cfg(tmp_path), clock, closes=closes))
    app.gateway.push_quote(CYB, _quote(CYB, 1.2, bid=1.2, ask=1.2))
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 0.8))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0))
    app._rotation.start("manual")
    assert _wait(lambda: any(
        b["code"] == CYB and b["direction"] == DIRECTION_BUY
        for b in app.gateway.query_orders()), timeout=5.0)
    # 信号日直接下单: cyb 动量 0.2 > nasdaq -0.2 → 当日买 cyb (不是次日)
    buys, _ = _orders_by(app)
    assert any(b["code"] == CYB for b in buys)
    # 买单之后 _execute 还有几步微秒级收尾 (写 pending_target), 用 _wait 等落定
    assert _wait(lambda: app._rotation._pending_target == CYB, timeout=2.0)


def test_worker_executes_pending_target(monkeypatch, tmp_path):
    """非信号日: 用当前生效 target 执行 (首买)。"""
    monkeypatch.setattr("trade.rotation.is_trading_day_cached", lambda d: True)
    monkeypatch.setattr("trade.rotation._is_signal_day", lambda d, sd: False)
    monkeypatch.setattr("trade.monitor.is_trading_day_cached", lambda d: True)
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock))
    app.gateway.push_quote(CYB, _quote(CYB, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0))
    assert _wait(lambda: app.monitor.quote_of(CYB) is not None)
    # 预置上个信号日已算好的当前生效 target
    app._rotation._pending_target = CYB
    app._rotation._has_target = True
    app._rotation.start("manual")
    assert _wait(lambda: any(
        b["code"] == CYB and b["direction"] == DIRECTION_BUY
        for b in app.gateway.query_orders()), timeout=5.0)
    buys, _ = _orders_by(app)
    assert any(b["code"] == CYB for b in buys)


# ═══════════════════════════════════════════════════════════════
# 股票池预算帽
# ═══════════════════════════════════════════════════════════════

def test_daily_loss_tolerates_inflight_sell_returns(tmp_path, monkeypatch):
    """2026-08-21: QMT 卖出回款在途 (cash 未+回款) 不应导致日亏闸误拒。

    实盘: 尾盘轮动卖创业板+黄金成交后, QMT cash 未及时回款而 market_value
    已扣持仓 → total_asset 少 51 万 → 日亏闸误判"当日亏 48%"拒掉买纳指。
    修复: current_equity 补回当日卖出成交额 (在途回款容错, 只读不改账)。"""
    clock = [_ts("14:54")]
    app = _start(_app(_cfg(tmp_path), clock))
    # 模拟 QMT: total_asset 被低估 (卖出回款未入账), 但 query_trades 有今日卖出
    monkeypatch.setattr(app.gateway, "query_asset",
                        lambda: {"cash": 534778.0, "frozen_cash": 0.0,
                                 "market_value": 0.0,
                                 "total_asset": 534778.0})
    monkeypatch.setattr(app.gateway, "query_trades",
                        lambda: [{"traded_id": "T1", "order_id": "O1",
                                  "code": "159949.SZ",
                                  "direction": DIRECTION_SELL,
                                  "price": 1.676, "qty": 274700,
                                  "amount": 460397.2, "ts": clock[0]}])
    app._day_baseline = 1033275.0    # 盘前基准 103万, 熔断线 = 87.8万
    # 未容错: 53.5万 < 87.8万 → 误拒; 容错后 ≈ 53.5万+46万 = 99.5万 → 放行
    ctx = app._build_risk_ctx()
    assert ctx.current_equity == pytest.approx(534778.0 + 460397.2, abs=1.0)
    intent = OrderIntent(code="513100.SH", direction=DIRECTION_BUY,
                         price=2.196, qty=8400)
    ok, why = app.risk.check(intent, ctx)
    assert ok, why
    # 无今日卖出成交时容错为 0 (普通交易日口径不变)
    monkeypatch.setattr(app.gateway, "query_trades", lambda: [])
    ctx2 = app._build_risk_ctx()
    assert ctx2.current_equity == pytest.approx(534778.0, abs=1.0)


def test_daily_loss_tolerates_sell_orders_ahead_of_trades(tmp_path, monkeypatch):
    """2026-08-24 (实盘 8-21 现场复现): query_orders 已见卖单终态而 query_trades
    成交回报晚 ~1s 时, 日亏闸补回必须用 orders 的已成交量兜住。

    实盘时序: 14:54:00.9 轮动挂卖 159949+518880, _wait_fills 从 orders 见终态
    放行, 14:54:01.96 买 513100 时 query_trades 尚无当日卖出 → 补回 = 0 →
    534778 < 87.8万 误拒, 52 万现金空仓过周末。修复: 补回取
    max(trades 金额, orders 当日卖出已成交量×委托价)。"""
    clock = [_ts("14:54")]
    app = _start(_app(_cfg(tmp_path), clock))
    # QMT total_asset 被低估 (卖出回款 T+1 未入账), trades 回报尚未到
    monkeypatch.setattr(app.gateway, "query_asset",
                        lambda: {"cash": 534778.0, "frozen_cash": 0.0,
                                 "market_value": 0.0,
                                 "total_asset": 534778.0})
    monkeypatch.setattr(app.gateway, "query_trades", lambda: [])
    # orders 已见终态 (已成, 含已成交量) —— 8-21 现场窗口
    monkeypatch.setattr(app.gateway, "query_orders", lambda: [
        {"order_id": "O1", "code": "159949.SZ", "direction": DIRECTION_SELL,
         "price": 1.676, "qty": 274700, "filled_qty": 274700,
         "status": 56, "ts": clock[0]},
        {"order_id": "O2", "code": "518880.SH", "direction": DIRECTION_SELL,
         "price": 9.384, "qty": 5600, "filled_qty": 5600,
         "status": 56, "ts": clock[0]}])
    app._day_baseline = 1033275.0    # 熔断线 = 87.8万
    expect = 534778.0 + 274700 * 1.676 + 5600 * 9.384
    ctx = app._build_risk_ctx()
    assert ctx.current_equity == pytest.approx(expect, abs=1.0)
    intent = OrderIntent(code="513100.SH", direction=DIRECTION_BUY,
                         price=2.196, qty=8400)
    ok, why = app.risk.check(intent, ctx)
    assert ok, why
    # 两路回报都齐: orders 与 trades 一致, 取 max 不重复计 (仍在途金额不变)
    monkeypatch.setattr(app.gateway, "query_trades", lambda: [
        {"traded_id": "T1", "order_id": "O1", "code": "159949.SZ",
         "direction": DIRECTION_SELL, "price": 1.676, "qty": 274700,
         "amount": 460397.2, "ts": clock[0]},
        {"traded_id": "T2", "order_id": "O2", "code": "518880.SH",
         "direction": DIRECTION_SELL, "price": 9.384, "qty": 5600,
         "amount": 52550.4, "ts": clock[0]}])
    ctx2 = app._build_risk_ctx()
    assert ctx2.current_equity == pytest.approx(expect, abs=1.0)

def test_stock_budget_cap(tmp_path):
    """轮动启用: 股票池预算 = (1-etf_ratio)×总资产 − 股票市值。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=990_000,
                      positions={STOCK: {"volume": 1000, "can_use": 1000,
                                         "avg_cost": 10.0}}))
    app.gateway.push_quote(STOCK, _quote(STOCK, 10.0))
    assert _wait(lambda: app.monitor.quote_of(STOCK) is not None)
    # total = 990k + 1000×10 = 1M; S_target = 0.5M; stock_val = 10k
    assert app._stock_budget() == pytest.approx(490_000.0, abs=1.0)


def test_stock_budget_disabled(tmp_path):
    """轮动关闭: 预算帽返回 None (auto_buy 走原口径)。"""
    clock = [_ts("10:00")]
    cfg = _cfg(tmp_path, enabled=False)
    app = _start(_app(cfg, clock))
    assert app._stock_budget() is None


# ═══════════════════════════════════════════════════════════════
# ETF 报价档位 (审计 HIGH#1) 与风控绕过 (审计 HIGH#2)
# ═══════════════════════════════════════════════════════════════

def test_round_price_etf_three_decimals():
    from trade.rotation import round_price_etf
    assert round_price_etf(2.004) == 2.004          # ETF 0.001 档, 不被压到 0.01
    assert round_price_etf(2.009) == 2.009
    assert round_price_etf(1.2345) == 1.235         # 千分位四舍五入


def test_rotation_buy_bypasses_max_positions(tmp_path):
    """rotation=True 绕过持仓数上限 (满仓时仍能买新 ETF)。"""
    clock = [_ts("10:00")]
    cfg = TradeConfig(
        account_id="AB", fake_sdk=True,
        db_path=str(tmp_path / "t2.db"), raw_log_path=str(tmp_path / "r2.jsonl"),
        kill_flag_path=str(tmp_path / "K2"),
        position_sizing=PositionSizingConfig(max_positions=1),
        rotation=RotationConfig(enabled=True, etf_ratio=0.5),
    )
    app = _start(_app(cfg, clock, positions={STOCK: {"volume": 100, "can_use": 100,
                                                     "avg_cost": 10.0}}))
    app.gateway.push_quote(CYB, _quote(CYB, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0))
    app.gateway.push_quote(STOCK, _quote(STOCK, 10.0))
    assert _wait(lambda: app.monitor.quote_of(CYB) is not None)
    app._rotation.on_signals({"signal": {"target": CYB}, "source": "test"})
    buys, _ = _orders_by(app)
    assert any(b["code"] == CYB for b in buys)      # 持仓数满仍放行 ETF 买入


def test_rotation_buy_rejected_kill_switch(tmp_path):
    """急停激活 → 轮动买入被闸1拒, 不下单 + 写 rotation_risk_reject。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock))
    app.gateway.push_quote(CYB, _quote(CYB, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0))
    assert _wait(lambda: app.monitor.quote_of(CYB) is not None)
    app.kill.activate("test")
    app._rotation.on_signals({"signal": {"target": CYB}, "source": "test"})
    buys, _ = _orders_by(app)
    assert buys == []                              # 急停 → 全面拒单
    ro = app.store.open_readonly()
    try:
        row = ro.execute("SELECT COUNT(*) FROM audit WHERE kind='rotation_risk_reject'").fetchone()
    finally:
        ro.close()
    assert row[0] >= 1                             # 拒单留痕


def test_on_signals_error_no_trade(tmp_path):
    """信号失败/数据不足 → 不调仓 (fail-closed 分支, 审计 M5#4)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock))
    app._rotation.on_signals({"signal": None, "error": "取数失败", "source": "test"})
    assert app.gateway.query_orders() == []          # 无任何下单
    assert app._rotation.last["error"] == "取数失败"
    app._rotation.on_signals({"signal": {"insufficient": True, "reason": "数据不足"},
                              "source": "test"})
    assert app.gateway.query_orders() == []          # 数据不足同样不下单


def test_stock_pool_excludes_rotation_etfs(tmp_path):
    """股票池市值排除全部轮动 ETF (含风险腿2, 2026-08-20)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=990_000,
                      positions={CYB: {"volume": 1000, "can_use": 1000, "avg_cost": 1.0},
                                 NASDAQ: {"volume": 1000, "can_use": 1000, "avg_cost": 1.0},
                                 GOLD: {"volume": 1000, "can_use": 1000, "avg_cost": 2.0},
                                 STOCK: {"volume": 1000, "can_use": 1000, "avg_cost": 10.0}}))
    app.gateway.push_quote(CYB, _quote(CYB, 1.0))
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0))
    app.gateway.push_quote(STOCK, _quote(STOCK, 10.0))
    assert _wait(lambda: app.monitor.quote_of(STOCK) is not None)
    # 只算股票池 (STOCK 1000×10=1 万), 三只轮动 ETF 都不计入
    assert app._stock_pool_value() == pytest.approx(10_000.0, abs=1.0)


def test_api_rotation_endpoints(tmp_path):
    """轮动 API 端点 (审计 M5#3)。"""
    from fastapi.testclient import TestClient

    from trade.api import create_api_app
    clock = [_ts("10:00")]
    # 注入 closes 让 QMT 主源命中, 避免 run 端点触发真实 TDX/akshare 网络降级
    app = _start(_app(_cfg(tmp_path), clock,
                      closes={CYB: [1.0] * 25, NASDAQ: [1.0] * 25}))
    client = TestClient(create_api_app(app))
    r = client.get("/api/trade/rotation/last")
    assert r.status_code == 200
    assert r.json()["config"]["enabled"] is True
    assert "risk_etf2" in r.json()["config"]        # 新字段暴露 (2026-08-20)
    r = client.post("/api/trade/rotation/run")
    assert r.status_code == 200
    assert r.json()["accepted"] is True
    app.stop()
