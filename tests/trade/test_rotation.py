"""ETF 轮动 + 双池资金分配测试 (2026-08-14).

锁住: 信号纯函数 (三态/数据不足)、状态派生、config 校验、
首买/换档/补仓的订单生成 (方向/代码/数量)、股票池预算帽。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.book import DIRECTION_BUY, DIRECTION_SELL
from trade.config import PositionSizingConfig, RotationConfig, TradeConfig, load_trade_config
from trade.rotation import (
    STATE_FULL_CYB,
    STATE_FULL_GOLD,
    STATE_HALF,
    _derive_state,
    compute_signal,
    target_values,
)
from trade_main import TradeApp

CYB = "159949.SZ"
GOLD = "518880.SH"
NASDAQ = "513100.SH"
INDEX = "399673.SZ"
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


# ═══════════════════════════════════════════════════════════════
# 信号纯函数
# ═══════════════════════════════════════════════════════════════

def test_compute_signal_full_cyb():
    closes = [1000.0 + i for i in range(300)]   # 持续上涨
    s = compute_signal(closes)
    assert s["state"] == STATE_FULL_CYB
    assert s["ma20_direction"] == "up"
    assert s["drawdown"] == pytest.approx(0.0, abs=0.01)


def test_compute_signal_down_gold():
    closes = [5000.0 - i * 5 for i in range(300)]   # 持续下跌
    s = compute_signal(closes)
    assert s["state"] == STATE_FULL_GOLD
    assert s["ma20_direction"] == "down"


def test_compute_signal_half_deep_drawdown():
    # 250 根: 前 228 低位 + 1 根高峰 100 + 最后 21 根慢涨 55→75
    closes = [50.0] * 228 + [100.0] + [55.0 + i for i in range(21)]
    assert len(closes) == 250
    s = compute_signal(closes)
    assert s["ma20_direction"] == "up"          # 最后 20 根在涨
    assert s["drawdown"] == pytest.approx(-0.25, abs=0.01)  # 75/100-1
    assert s["state"] == STATE_HALF             # 回撤 > 20%


def test_compute_signal_insufficient():
    s = compute_signal([1.0, 2.0])
    assert s["state"] is None
    assert "reason" in s


def test_derive_state():
    assert _derive_state(100, 0) == STATE_FULL_CYB
    assert _derive_state(50, 50) == STATE_HALF
    assert _derive_state(0, 100) == STATE_FULL_GOLD
    assert _derive_state(0, 0) is None


def test_target_values_single_and_split():
    pool = 1_000_000.0
    # 单避险 (hedge_etf2 空): 与旧口径一致 —— 满仓避险 = 100% 黄金
    assert target_values(CYB, GOLD, "", 1.0, STATE_FULL_GOLD, pool) == \
        [(CYB, 0.0), (GOLD, pool)]
    # 各半 (hedge_ratio 0.5): 满仓避险 = 黄金 50% + 纳指 50%
    assert target_values(CYB, GOLD, NASDAQ, 0.5, STATE_FULL_GOLD, pool) == \
        [(CYB, 0.0), (GOLD, pool * 0.5), (NASDAQ, pool * 0.5)]
    # 30/70: 黄金 30% + 纳指 70%
    legs = target_values(CYB, GOLD, NASDAQ, 0.3, STATE_FULL_GOLD, pool)
    assert legs[1] == (GOLD, pool * 0.3)
    assert legs[2] == (NASDAQ, pool * 0.7)
    # 半仓态 + 各半: 主腿 50%, 黄金 25%, 纳指 25%
    assert target_values(CYB, GOLD, NASDAQ, 0.5, STATE_HALF, pool) == \
        [(CYB, pool * 0.5), (GOLD, pool * 0.25), (NASDAQ, pool * 0.25)]
    # hedge_etf2 空 + hedge_ratio=0.5 → 归一化回单黄金 (审计② 归一化分支)
    assert target_values(CYB, GOLD, "", 0.5, STATE_FULL_GOLD, pool) == \
        [(CYB, 0.0), (GOLD, pool)]


# ═══════════════════════════════════════════════════════════════
# config 校验
# ═══════════════════════════════════════════════════════════════

def test_rotation_config_defaults():
    c = TradeConfig()
    assert c.rotation.enabled is False           # 未显式配置不开 (fail-safe)
    assert c.rotation.etf_ratio == 0.5
    assert c.rotation.ma_window == 20


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
    with pytest.raises(ValueError, match="signal_index"):
        _load("rotation:\n  signal_index: '999'\n")
    with pytest.raises(ValueError, match="未知字段"):
        _load("rotation:\n  magic: 1\n")
    # 第二避险ETF + 比例 (2026-08-18)
    cfg2 = _load("rotation:\n  hedge_etf2: '513100.SH'\n  hedge_ratio: 0.5\n")
    assert cfg2.rotation.hedge_etf2 == "513100.SH"
    assert cfg2.rotation.hedge_ratio == 0.5
    with pytest.raises(ValueError, match="hedge_ratio"):
        _load("rotation:\n  hedge_ratio: 1.5\n")        # 超 [0,1]
    with pytest.raises(ValueError, match="hedge_ratio"):
        _load("rotation:\n  hedge_ratio: 0.5\n")         # hedge_etf2 空时非 1.0 (审计①)
    with pytest.raises(ValueError, match="hedge_etf2"):
        _load("rotation:\n  hedge_etf2: '999'\n")        # 非 6 位代码
    with pytest.raises(ValueError, match="hedge_etf2"):
        _load("rotation:\n  hedge_etf2: '518880.SH'\n")  # 与 gold_etf 重复


# ═══════════════════════════════════════════════════════════════
# 调仓订单生成
# ═══════════════════════════════════════════════════════════════

def _orders_by(app):
    o = app.gateway.query_orders()
    return ([x for x in o if x["direction"] == DIRECTION_BUY],
            [x for x in o if x["direction"] == DIRECTION_SELL])


def test_first_buy_full_cyb(tmp_path):
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock))
    app.gateway.push_quote(CYB, _quote(CYB, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0))
    assert _wait(lambda: app.monitor.quote_of(CYB) is not None)
    app._rotation.on_signals({"signal": {"state": STATE_FULL_CYB},
                              "source": "test"})
    buys, sells = _orders_by(app)
    assert len(sells) == 0
    assert len(buys) == 1
    assert buys[0]["code"] == CYB
    # pool = 0.5 × 1,000,000 = 500,000; 价 1.0 → 500,000 份
    assert buys[0]["qty"] == 500_000


def test_swap_full_cyb_to_gold(tmp_path):
    """换档卖单未成交 → 只卖不买 (池级预算帽归零, 不超买不花股票池现金)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=500_000,
                      positions={CYB: {"volume": 500_000, "can_use": 500_000,
                                       "avg_cost": 1.0}}))
    app.gateway.push_quote(CYB, _quote(CYB, 1.0, bid=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(CYB) is not None)
    app._rotation.on_signals({"signal": {"state": STATE_FULL_GOLD},
                              "source": "test"})
    buys, sells = _orders_by(app)
    assert [s["code"] for s in sells] == [CYB]
    assert sells[0]["qty"] == 500_000               # 全清创业板
    assert buys == []                               # 卖未成交 → 不超买 (审计 C2)


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
    app._rotation.on_signals({"signal": {"state": STATE_FULL_GOLD},
                              "source": "test"})
    _, sells = _orders_by(app)
    assert sells and sells[0]["qty"] == 500_000
    # 模拟成交: 创业板清仓、现金回笼到 100 万
    app.gateway.simulate_fill(sells[0]["order_id"])
    # 第二轮: 卖已成交 → 池级缺口 50 万 → 补买黄金
    app._rotation.on_signals({"signal": {"state": STATE_FULL_GOLD},
                              "source": "test"})
    buys, _ = _orders_by(app)
    gold_buys = [b for b in buys if b["code"] == GOLD]
    assert gold_buys and gold_buys[0]["qty"] == 250_000  # 50万 @2.0


def test_two_hedge_split_first_buy(tmp_path):
    """两只避险腿各半: 满仓避险首买 → 黄金/纳指各买一半池。"""
    clock = [_ts("10:00")]
    cfg = _cfg(tmp_path, hedge_etf2=NASDAQ, hedge_ratio=0.5)
    app = _start(_app(cfg, clock))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    assert _wait(lambda: app.monitor.quote_of(GOLD) is not None)
    app._rotation.on_signals({"signal": {"state": STATE_FULL_GOLD},
                              "source": "test"})
    buys, sells = _orders_by(app)
    assert len(sells) == 0
    # pool = 50 万; 黄金 25 万 @2.0 = 125,000 份; 纳指 25 万 @1.0 = 250,000 份
    gold_b = [b for b in buys if b["code"] == GOLD]
    nas_b = [b for b in buys if b["code"] == NASDAQ]
    assert gold_b and gold_b[0]["qty"] == 125_000
    assert nas_b and nas_b[0]["qty"] == 250_000


def test_two_hedge_split_half_first_buy(tmp_path):
    """两只避险腿各半 + 半仓态首买: 主腿 50%、黄金/纳指各 25% (审计② 换档买两腿)。"""
    clock = [_ts("10:00")]
    cfg = _cfg(tmp_path, hedge_etf2=NASDAQ, hedge_ratio=0.5)
    app = _start(_app(cfg, clock))
    app.gateway.push_quote(CYB, _quote(CYB, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    app.gateway.push_quote(NASDAQ, _quote(NASDAQ, 1.0, bid=1.0, ask=1.0))
    assert _wait(lambda: app.monitor.quote_of(CYB) is not None)
    app._rotation.on_signals({"signal": {"state": STATE_HALF}, "source": "test"})
    buys, sells = _orders_by(app)
    assert len(sells) == 0
    by_code = {b["code"]: b["qty"] for b in buys}
    # pool=50万; 主腿50%=25万@1.0=250_000; 黄金25%=12.5万@2.0=62_500; 纳指25%=12.5万@1.0=125_000
    assert by_code == {CYB: 250_000, GOLD: 62_500, NASDAQ: 125_000}


def test_half_from_full_cyb(tmp_path):
    """满→半: 卖一半创业板, 卖未成交不买黄金 (池级帽)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=500_000,
                      positions={CYB: {"volume": 500_000, "can_use": 500_000,
                                       "avg_cost": 1.0}}))
    app.gateway.push_quote(CYB, _quote(CYB, 1.0, bid=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0, bid=2.0, ask=2.0))
    assert _wait(lambda: app.monitor.quote_of(CYB) is not None)
    app._rotation.on_signals({"signal": {"state": STATE_HALF}, "source": "test"})
    buys, sells = _orders_by(app)
    assert sells[0]["code"] == CYB and sells[0]["qty"] == 250_000  # 卖一半
    assert buys == []                               # 卖未成交 → 不买黄金


def test_topup_no_state_change(tmp_path):
    """状态不变但 ETF 池低配 → 只补仓不卖 (模型 B 无主动卖)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=750_000,
                      positions={CYB: {"volume": 250_000, "can_use": 250_000,
                                       "avg_cost": 1.0}}))
    app.gateway.push_quote(CYB, _quote(CYB, 1.0, bid=1.0, ask=1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0))
    assert _wait(lambda: app.monitor.quote_of(CYB) is not None)
    app._rotation.on_signals({"signal": {"state": STATE_FULL_CYB},
                              "source": "test"})
    buys, sells = _orders_by(app)
    assert len(sells) == 0                           # 不主动卖
    assert [b["code"] for b in buys] == [CYB]
    assert buys[0]["qty"] == 250_000                 # 补到 50 万


def test_worker_full_path(tmp_path):
    """完整链路: 工作线程拉日线 → 算信号 → 消费者调仓 (首买)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock,
                      closes={INDEX: [1000.0 + i for i in range(300)]}))
    app.gateway.push_quote(CYB, _quote(CYB, 1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0))
    assert _wait(lambda: app.monitor.quote_of(CYB) is not None)
    app._rotation.start("manual")
    # 等调仓下单完成: on_signals 先设 last 再调 _execute (同一线程), 等 last
    # 有竞态 (last 出现时下单还没跑), 改直接等买单出现
    assert _wait(lambda: any(
        b["code"] == CYB and b["direction"] == DIRECTION_BUY
        for b in app.gateway.query_orders()), timeout=5.0)
    assert app._rotation.last["signal"]["state"] == STATE_FULL_CYB
    buys, _ = _orders_by(app)
    assert any(b["code"] == CYB for b in buys)


def test_worker_intraday_uses_realtime_price(tmp_path, monkeypatch):
    """2026-08-16 拍板: 盘中(14:56)用实时价当今日收盘价, 信号日=今日(非昨日)。"""
    from datetime import datetime
    # 钉死"今日是交易日" — 否则真实日历把节后/周一标成非交易日会致测试 flaky
    monkeypatch.setattr("trade.rotation.is_trading_day_cached", lambda d: True)
    clock = [_ts("14:56")]
    closes = [1000.0 + i for i in range(300)]   # 持续上涨 → 昨日信号 full_cyb
    app = _app(_cfg(tmp_path), clock, closes={INDEX: closes})
    # 先推实时价再启动: clock=14:56 恰等于默认 execute_time=14:56, app.start()
    # 的启动补偿 (_startup_catchup) 会补跑一轮 scheduled —— 若此时 quote 为空,
    # 那轮补跑走"回退昨日"分支产出 signal_date=昨日, 与后续 manual 触发竞速污染
    # _last (2026-08-19 审计定位: 该测试自 207f1a1 引入起即因时序缺陷失败)。
    app.gateway.push_quote(INDEX, _quote(INDEX, 900.0))
    _start(app)
    app._rotation.start("manual")
    assert _wait(lambda: app._rotation.last is not None
                 and app._rotation.last.get("signal") is not None, timeout=5.0)
    sig = app._rotation.last["signal"]
    today = datetime.fromtimestamp(clock[0]).strftime("%Y%m%d")
    assert sig["date"] == today, f"盘中实时价应记今日, 实际 {sig['date']}"
    assert sig["close"] == 900.0, f"信号应基于实时价 900, 实际 {sig['close']}"
    # 昨日收盘 1299 是 full_cyb; 追加 900 (当日大跌 31%) 后 MA20 转 down → full_gold
    assert sig["state"] == STATE_FULL_GOLD, f"实时价大跌应转 full_gold, 实际 {sig['state']}"


def test_fetch_closes_qmt_primary(tmp_path):
    """2026-08-17: 取数降级链 — QMT 命中时不降级。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock,
                      closes={INDEX: [1000.0 + i for i in range(300)]}))
    closes, src = app._rotation._fetch_closes(INDEX, 370, 250)
    assert src == "QMT" and len(closes) == 300


def test_fetch_closes_fallback_chain(tmp_path, monkeypatch):
    """2026-08-17: 三级降级 QMT 不足 → TDX; TDX 挂 → 腾讯; 全挂 → none (fail-closed)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock))   # 无 closes → QMT 空
    # mock TDX/腾讯 兜底 (不真连网络)
    monkeypatch.setattr(app._rotation, "_fetch_tdx_closes",
                        lambda code, count: [float(i) for i in range(300)])
    monkeypatch.setattr(app._rotation, "_fetch_tencent_closes",
                        lambda code, count: [float(i) for i in range(300)])
    closes, src = app._rotation._fetch_closes(INDEX, 370, 250)
    assert src == "TDX" and len(closes) == 300
    # TDX 也挂 → 腾讯
    monkeypatch.setattr(app._rotation, "_fetch_tdx_closes",
                        lambda code, count: [])
    closes, src = app._rotation._fetch_closes(INDEX, 370, 250)
    assert src == "腾讯" and len(closes) == 300
    # 全挂 → none (fail-closed, compute_signal 判数据不足)
    monkeypatch.setattr(app._rotation, "_fetch_tencent_closes",
                        lambda code, count: [])
    closes, src = app._rotation._fetch_closes(INDEX, 370, 250)
    assert src == "none" and closes == []


# ═══════════════════════════════════════════════════════════════
# 股票池预算帽
# ═══════════════════════════════════════════════════════════════

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
    # 持仓数已达上限 (1 只股票), 且 max_positions=1
    cfg = _cfg(tmp_path)
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
    app._rotation.on_signals({"signal": {"state": STATE_FULL_CYB}, "source": "test"})
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
    app._rotation.on_signals({"signal": {"state": STATE_FULL_CYB}, "source": "test"})
    buys, _ = _orders_by(app)
    assert buys == []                              # 急停 → 全面拒单
    ro = app.store.open_readonly()
    try:
        row = ro.execute("SELECT COUNT(*) FROM audit WHERE kind='rotation_risk_reject'").fetchone()
    finally:
        ro.close()
    assert row[0] >= 1                             # 拒单留痕


def test_derive_state_boundaries():
    """状态派生 0.9/0.1 容差带边界 (审计 M5#2)。"""
    assert _derive_state(90, 10) == STATE_FULL_CYB    # ratio 0.9 → full_cyb
    assert _derive_state(85, 15) == STATE_HALF        # 0.85 → half
    assert _derive_state(15, 85) == STATE_HALF        # 0.15 → half
    assert _derive_state(10, 90) == STATE_FULL_GOLD   # 0.1 → full_gold


def test_on_signals_error_no_trade(tmp_path):
    """信号失败/数据不足 → 不调仓 (fail-closed 分支, 审计 M5#4)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock))
    app._rotation.on_signals({"signal": None, "error": "取数失败", "source": "test"})
    assert app.gateway.query_orders() == []          # 无任何下单
    assert app._rotation.last["error"] == "取数失败"
    app._rotation.on_signals({"signal": {"state": None, "reason": "数据不足"},
                              "source": "test"})
    assert app.gateway.query_orders() == []          # 数据不足同样不下单


def test_stock_pool_excludes_rotation_etfs(tmp_path):
    """股票池市值排除两只轮动 ETF (审计 M5#5)。"""
    clock = [_ts("10:00")]
    app = _start(_app(_cfg(tmp_path), clock, cash=990_000,
                      positions={CYB: {"volume": 1000, "can_use": 1000, "avg_cost": 1.0},
                                 GOLD: {"volume": 1000, "can_use": 1000, "avg_cost": 2.0},
                                 STOCK: {"volume": 1000, "can_use": 1000, "avg_cost": 10.0}}))
    app.gateway.push_quote(CYB, _quote(CYB, 1.0))
    app.gateway.push_quote(GOLD, _quote(GOLD, 2.0))
    app.gateway.push_quote(STOCK, _quote(STOCK, 10.0))
    assert _wait(lambda: app.monitor.quote_of(STOCK) is not None)
    # 只算股票池 (STOCK 1000×10=1 万), 轮动两只 ETF 不计入
    assert app._stock_pool_value() == pytest.approx(10_000.0, abs=1.0)


def test_api_rotation_endpoints(tmp_path):
    """轮动 API 端点 (审计 M5#3)。"""
    from fastapi.testclient import TestClient

    from trade.api import create_api_app
    clock = [_ts("10:00")]
    # 注入 closes 让 QMT 主源命中, 避免 run 端点触发真实 TDX/akshare 网络降级
    app = _start(_app(_cfg(tmp_path), clock,
                      closes={INDEX: [1000.0 + i for i in range(300)]}))
    client = TestClient(create_api_app(app))
    r = client.get("/api/trade/rotation/last")
    assert r.status_code == 200
    assert r.json()["config"]["enabled"] is True
    r = client.post("/api/trade/rotation/run")
    assert r.status_code == 200
    assert r.json()["accepted"] is True
    app.stop()
