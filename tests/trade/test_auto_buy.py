"""尾盘自动选股买入 MVP 测试 (2026-07-27).

锁住: 过滤矩阵 (持仓/ETF/涨停/现金/风控/上限)、数量计算 (取整手)、
卖一价限价 vs 14:57 后对手最优、remark 格式、audit 汇总、
工作线程不堵消费者、config 校验、api 两端点。
"""
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient

from trade.api import create_api_app
from trade.book import PRICE_TYPE_LIMIT
from trade.config import AutoBuyConfig, TradeConfig, load_trade_config
from trade.events import EVENT_TICK, Event
from trade_main import TradeApp

CODE = "000001.SZ"
ETF = "510300.SH"


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


@pytest.fixture()
def cfg(tmp_path):
    return TradeConfig(
        account_id="AB", fake_sdk=True,
        db_path=str(tmp_path / "t.db"),
        raw_log_path=str(tmp_path / "r.jsonl"),
        kill_flag_path=str(tmp_path / "KILL"),
    )


def _make_app(cfg, clock, runner=None, positions=None, cash=1_000_000.0):
    app = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                   fake_gateway_kwargs={"cash": cash,
                                        "positions": positions or {}},
                   selection_runner=runner or (lambda f, a, u: []))
    # disposition 终态轮询: 测试不等生产 2s×10s (Fake 已报非终态,
    # 会跑满 timeout), 注小值; 终态用例里 fill/reject 后提前结束
    # (2026-08-01: TradeApp 委托缝已删, 直注 feature 实例属性)
    app._auto_buy._await_interval = 0.01
    app._auto_buy._await_timeout = 0.05
    return app


def _start(app):
    assert app.start(start_timers=False)
    return app


def _push(app, code, last, ask1, prev_close=10.0):
    app.gateway.push_quote(code, {"last": last, "bid1": last - 0.01,
                                  "ask1": ask1, "high": last,
                                  "prev_close": prev_close})
    assert _wait(lambda: app.monitor.quote_of(code) is not None)


def _run_signals(app, signals):
    """直接驱动结果处理 (跳过工作线程, 执行路径确定性测试)。"""
    app._auto_buy.on_signals({"signals": signals, "source": "test"})


# ═══════════════════════════════════════════════════════════════
# config 校验
# ═══════════════════════════════════════════════════════════════

def test_auto_buy_config_defaults():
    c = TradeConfig()
    assert c.auto_buy.enabled is False          # 未显式配置不开 (fail-safe)
    assert c.auto_buy.time == "14:52"
    assert c.auto_buy.amount_per_stock == 10000.0


def test_auto_buy_config_validation(tmp_path):
    def _load(text):
        p = tmp_path / "c.yaml"
        p.write_text(text, encoding="utf-8")
        return load_trade_config(p)
    cfg = _load("auto_buy:\n  enabled: true\n  time: '14:52'\n"
                "  amount_per_stock: 8000\n  max_buys_per_day: 3\n")
    assert cfg.auto_buy.enabled and cfg.auto_buy.amount_per_stock == 8000
    with pytest.raises(ValueError, match="HH:MM"):
        _load("auto_buy:\n  time: '25:99'\n")
    with pytest.raises(ValueError, match="max_buys_per_day"):
        _load("auto_buy:\n  max_buys_per_day: 101\n")
    with pytest.raises(ValueError, match="未知字段"):
        _load("auto_buy:\n  magic: 1\n")


# ═══════════════════════════════════════════════════════════════
# 买入执行: 过滤矩阵 + 定价 + 数量
# ═══════════════════════════════════════════════════════════════

def test_buy_happy_path_ask1_limit(cfg):
    """正常买入: 卖一价限价, 数量取整手, remark 走发号器, audit 齐全。"""
    clock = [_ts("14:52")]
    app = _start(_make_app(cfg, clock))
    _push(app, CODE, 25.5, 25.51, prev_close=25.0)
    _run_signals(app, [{"code": CODE, "select_date": "20260727"}])
    orders = app.gateway.query_orders()
    assert len(orders) == 1
    o = orders[0]
    assert o["direction"] == 23 and o["price"] == 25.51
    assert o["qty"] == 300            # floor(10000/25.51/100)*100
    assert o["remark"].endswith("B") and o["remark"].startswith("V")
    kinds = {r[0] for r in app.store._conn.execute(
        "SELECT kind FROM audit").fetchall()}
    assert {"auto_buy", "auto_buy_summary"} <= kinds
    last = app._auto_buy.last
    assert last["selected"] == 1 and last["bought"] == 1
    assert last["dispositions"][0]["action"] == "buy"


def test_new_codes_subscribed_before_pricing(cfg):
    """2026-08-07 (实盘 0807 废单事件): 信号票定价前先补订阅 ——
    裸快照缺盘口会走"对手最优"市价兜底, 券商通道拒单 (4/4 全灭)。"""
    clock = [_ts("14:54")]
    app = _start(_make_app(cfg, clock))
    calls = []
    orig = app.gateway.subscribe_quotes

    def spy(codes):
        calls.append(list(codes))
        return orig(codes)
    app.gateway.subscribe_quotes = spy
    _push(app, CODE, 25.5, 25.51, prev_close=25.0)
    _run_signals(app, [{"code": CODE, "select_date": "20260807"}])
    assert [CODE] in calls
    assert app._auto_buy.last["bought"] == 1


def test_missing_ask1_refetched_before_pricing(cfg):
    """首轮快照缺 ask1 → 定向补查补上 → 按卖一价限价, 不走对手最优。"""
    clock = [_ts("14:54")]
    app = _start(_make_app(cfg, clock))
    _push(app, CODE, 25.5, 25.51, prev_close=25.0)
    real_q = app.gateway.query_quotes
    state = {"n": 0}

    def flaky(codes):
        state["n"] += 1
        out = real_q(codes)
        if state["n"] == 1:               # 首轮掐掉 ask1, 模拟快照缺盘口
            return {c: dict(q, ask1=0.0) for c, q in out.items()}
        return out
    app.gateway.query_quotes = flaky
    _run_signals(app, [{"code": CODE, "select_date": "20260807"}])
    assert state["n"] == 2                # 首轮 + 定向补查各一次
    o = app.gateway.query_orders()[0]
    assert o["price"] == 25.51            # 卖一价限价, 非市价单 0.0
    d = app._auto_buy.last["dispositions"][0]
    assert d["action"] == "buy" and d["reason"] == "卖一价"


def test_skip_matrix(cfg):
    """过滤矩阵: 已持仓 / ETF / 涨停 / 现金不足一手 / 达每日上限。"""
    clock = [_ts("14:52")]
    held = "600519.SH"
    app = _start(_make_app(cfg, clock, positions={
        held: {"volume": 500, "can_use": 500, "avg_cost": 10.0}}, cash=1000.0))
    _push(app, held, 10.5, 10.51)
    _push(app, ETF, 4.0, 4.01, prev_close=4.0)
    _push(app, "300750.SZ", 11.0, 11.01, prev_close=10.0)   # 创业板涨停 20%? 11<12 不涨停
    _push(app, "000002.SZ", 11.0, 11.01, prev_close=10.0)   # 主板涨停 10%: 11≥11 涨停
    _push(app, "000003.SZ", 50.0, 50.01, prev_close=49.0)   # 现金 3000 不够一手
    _push(app, "000004.SZ", 10.5, 10.51)
    _push(app, CODE, 25.5, 25.51, prev_close=25.0)
    _run_signals(app, [{"code": c, "select_date": "x"} for c in
                       [held, ETF, "000002.SZ", "000003.SZ",
                        "000004.SZ", CODE]])
    last = app._auto_buy.last
    reasons = {d["code"]: d.get("reason") for d in last["dispositions"]}
    assert reasons[held] == "已持仓"
    assert reasons[ETF] == "ETF不管理"
    assert reasons["000002.SZ"] == "涨停拒买"
    assert reasons["000003.SZ"] == "现金不足一手"
    # 现金 1000: 000004 (1051/手) 与 CODE (2551/手) 都买不起
    assert last["bought"] == 0


def test_budget_cap_insufficient_reason(cfg):
    """2026-08-17: 双池预算帽把股票池额度压到 0 时, 报"股票池预算不足"
    而非"现金不足一手" (账户有钱, 只是那钱归 ETF 池)。"""
    clock = [_ts("14:52")]
    app = _start(_make_app(cfg, clock, cash=500_000.0))
    app._auto_buy._budget_provider = lambda: 0.0   # 股票池额度已满
    _push(app, CODE, 25.5, 25.51, prev_close=25.0)
    _run_signals(app, [{"code": CODE, "select_date": "x"}])
    last = app._auto_buy.last
    assert last["bought"] == 0
    assert last["dispositions"][0]["reason"] == "股票池预算不足"


def test_amount_per_stock_below_lot_reason(tmp_path):
    """2026-08-17: 单票金额上限低于一手价 (高价股) 时, 报"单票上限低于一手",
    区分于现金不足 (这是配置问题, 调高 amount_per_stock 即可)。"""
    cfg = TradeConfig(
        account_id="AB", fake_sdk=True,
        db_path=str(tmp_path / "t.db"),
        raw_log_path=str(tmp_path / "r.jsonl"),
        kill_flag_path=str(tmp_path / "KILL"),
        auto_buy=AutoBuyConfig(enabled=True, amount_per_stock=1000.0))
    clock = [_ts("14:52")]
    app = _start(_make_app(cfg, clock, cash=1_000_000.0))
    _push(app, CODE, 25.5, 25.51, prev_close=25.0)   # 一手 2551 > 上限 1000
    _run_signals(app, [{"code": CODE, "select_date": "x"}])
    last = app._auto_buy.last
    assert last["bought"] == 0
    assert last["dispositions"][0]["reason"] == "单票上限低于一手"


def test_max_buys_per_day_cap(cfg):
    """达 max_buys_per_day 停止, 后续标"达每日上限"。"""
    clock = [_ts("14:52")]
    app = _start(_make_app(cfg, clock))
    codes = [f"00000{i}.SZ" for i in range(1, 8)]
    for c in codes:
        _push(app, c, 10.0, 10.01)
    _run_signals(app, [{"code": c, "select_date": "x"} for c in codes])
    last = app._auto_buy.last
    assert last["bought"] == 5                     # 默认上限 5
    skips = [d for d in last["dispositions"] if d["action"] == "skip"]
    assert all(d["reason"] == "达每日上限" for d in skips)
    assert len(skips) == 2


def test_sz_force_market_uses_limit_up_price(cfg):
    """2026-08-01 P0-2 修复: ≥force_market_after 全板块禁市价单。
    .SZ 深主板 → 限价@涨停价; 创业/科创板 → min(涨停, 卖一×1.02) 贴笼子。"""
    clock = [_ts("14:58")]
    app = _start(_make_app(cfg, clock))
    _push(app, CODE, 25.5, 25.51, prev_close=25.0)       # 深主板
    _push(app, "300750.SZ", 10.5, 10.51, prev_close=10.0)  # 创业板
    _run_signals(app, [{"code": CODE, "select_date": "x"},
                       {"code": "300750.SZ", "select_date": "x"}])
    orders = {o["code"]: o for o in app.gateway.query_orders()}
    sz = orders[CODE]
    assert sz["price_type"] == PRICE_TYPE_LIMIT  # LIMIT, 不是对手最优
    assert sz["price"] == 27.5                   # 深主板涨停价 25×1.1
    cyb = orders["300750.SZ"]
    assert cyb["price_type"] == PRICE_TYPE_LIMIT  # LIMIT, 不是对手最优
    # 创业板 20% 涨停=12.0, 但 > 卖一×1.02=10.5×1.02=10.71→round_price=10.72
    assert cyb["price"] == 10.72                 # 贴笼子上限
    reasons = {d["code"]: d["reason"] for d in app._auto_buy.last["dispositions"]}
    assert "收盘竞价" in reasons[CODE]


def test_sh_force_market_uses_limit_price(cfg):
    """2026-08-01 P0-2 修复: 沪市 ≥force_market_after 也禁市价单
    (沪市 2018 年起收盘为集合竞价, 不接受市价单, 07-31 实测 6/6 废单)。
    改为限价@涨停价。"""
    clock = [_ts("14:58")]
    app = _start(_make_app(cfg, clock))
    _push(app, "600519.SH", 25.5, 25.51, prev_close=25.0)
    _run_signals(app, [{"code": "600519.SH", "select_date": "x"}])
    o = app.gateway.query_orders()[0]
    assert o["price_type"] == PRICE_TYPE_LIMIT     # LIMIT, 不再是市价单
    assert o["price"] == 27.5                      # 涨停价 25×1.1


def test_688_force_market_cage_limit(cfg):
    """2026-08-01 修复: 科创板 688.SH 同样是 20% 涨停 + 2% 笼子,
    不能靠 is_sz 判定 (688 是沪市, is_sz 恒 False)。
    应走 min(涨停价, 卖一×1.02) 笼子上限分支。"""
    clock = [_ts("14:58")]
    app = _start(_make_app(cfg, clock))
    _push(app, "688099.SH", 20.5, 20.51, prev_close=20.0)
    _run_signals(app, [{"code": "688099.SH", "select_date": "x"}])
    o = app.gateway.query_orders()[0]
    assert o["price_type"] == PRICE_TYPE_LIMIT
    # 科创板涨停 20×1.2=24.0, 但 > 卖一×1.02=20.51×1.02=20.9202→round_price=20.92
    assert o["price"] == 20.92                     # 贴笼子上限, 不是涨停价 24.0


def test_disposition_final_status(cfg):
    """disposition 补终态: 已成@均价 / 废单(状态码) / 在途。"""
    clock = [_ts("14:52")]
    app = _start(_make_app(cfg, clock))
    _push(app, CODE, 25.5, 25.51, prev_close=25.0)
    _push(app, "000004.SZ", 10.5, 10.51, prev_close=10.0)
    _push(app, "000005.SZ", 10.5, 10.51, prev_close=10.0)
    _run_signals(app, [{"code": c, "select_date": "x"}
                       for c in [CODE, "000004.SZ", "000005.SZ"]])
    # 下单后: 一票成交, 一票废单, 一票留在已报 (在途)
    orders = app.gateway.query_orders()
    by_code = {o["code"]: o for o in orders}
    app.gateway.simulate_fill(by_code[CODE]["order_id"])
    app.gateway.simulate_reject(by_code["000004.SZ"]["order_id"])
    # 重新跑一次终态补记 (直接调内部, 避免再下一批单)
    last = app._auto_buy.last
    app._auto_buy._await_and_fill_dispositions(last["dispositions"])
    status = {d["code"]: d.get("status") for d in last["dispositions"]}
    assert status[CODE].startswith("已成@")
    assert status["000004.SZ"] == "废单(状态57)"
    assert status["000005.SZ"] == "在途"


def test_second_run_same_day_skips_bought(cfg):
    """当日已买过 (本批 placed + store 当日成交) 不重复买。"""
    clock = [_ts("14:52")]
    app = _start(_make_app(cfg, clock))
    _push(app, CODE, 25.5, 25.51, prev_close=25.0)
    _run_signals(app, [{"code": CODE, "select_date": "x"}])
    assert len(app.gateway.query_orders()) == 1
    _run_signals(app, [{"code": CODE, "select_date": "x"}])   # 再来一次
    assert len(app.gateway.query_orders()) == 1               # 不重买
    assert app._auto_buy.last["dispositions"][0]["reason"] == "今日已买过"


def test_risk_reject_blocks_buy(cfg, tmp_path):
    """急停激活 → 风控拒, 该票标"风控拒"且不下单。"""
    clock = [_ts("14:52")]
    app = _make_app(cfg, clock)
    app.kill.activate("测试")
    _start(app)
    _push(app, CODE, 25.5, 25.51, prev_close=25.0)
    _run_signals(app, [{"code": CODE, "select_date": "x"}])
    assert app.gateway.query_orders() == []
    assert "风控拒" in app._auto_buy.last["dispositions"][0]["reason"]


def test_selection_error_path(cfg):
    """选股失败: error 事件 → audit + last.error, 不误买入。"""
    clock = [_ts("14:52")]
    app = _start(_make_app(cfg, clock))
    app._auto_buy.on_signals({"signals": None, "error": "TDX 连接失败", "source": "test"})
    assert app._auto_buy.last["error"] == "TDX 连接失败"
    rows = app.store._conn.execute(
        "SELECT kind FROM audit WHERE kind='auto_buy_error'").fetchall()
    assert rows
    assert app.gateway.query_orders() == []


# ═══════════════════════════════════════════════════════════════
# 工作线程不堵消费者 (唯一写者不能被 TDX 卡死)
# ═══════════════════════════════════════════════════════════════

def test_worker_thread_does_not_block_consumer(cfg):
    """慢选股 (1s) 期间, 其他事件 (tick) 照常消费。"""
    clock = [_ts("14:52")]
    tick_seen = threading.Event()

    def slow_runner(f, a, u):
        time.sleep(1.0)
        return [{"code": CODE, "select_date": "x"}]

    app = _start(_make_app(cfg, clock, runner=slow_runner))
    # tick handler 钩子: monitor quote 缓存被更新即证明消费者活着。
    # ts 给注入时钟值 —— 与生产 _wire 同源, 否则默认 time.time() 与
    # 锚定假时钟偏差 >30s 会被 M4 当过旧丢弃 (测试自己踩的坑)
    app._engine.put(Event(type=EVENT_TICK, ts=clock[0],
                          data={"code": CODE, "last": 25.5, "ask1": 25.51}))
    app._auto_buy.start("manual")
    t0 = time.time()
    got = _wait(lambda: app.monitor.quote_of(CODE) is not None, timeout=0.8)
    assert got, "慢选股期间消费者线程被堵"
    assert time.time() - t0 < 1.0
    # 选股完成后买入照常
    assert _wait(lambda: app._auto_buy.last is not None, timeout=4.0)
    app.stop()


def test_auto_buy_reentry_guard(cfg):
    """上一次还在跑 → 重复发起被拦 (audit 留痕)。"""
    clock = [_ts("14:52")]
    gate = threading.Event()

    def blocking_runner(f, a, u):
        gate.wait(2.0)
        return []

    app = _start(_make_app(cfg, clock, runner=blocking_runner))
    app._auto_buy.start("manual")
    assert _wait(lambda: app._auto_buy.running)
    app._auto_buy.start("manual")   # 重入
    rows = app.store._conn.execute(
        "SELECT kind FROM audit WHERE kind='auto_buy_skip'").fetchall()
    gate.set()
    assert rows
    app.stop()


# ═══════════════════════════════════════════════════════════════
# api 端点
# ═══════════════════════════════════════════════════════════════

def test_api_auto_buy_endpoints(cfg):
    clock = [_ts("14:52")]
    app = _start(_make_app(cfg, clock))
    c = TestClient(create_api_app(app))
    try:
        r = c.post("/api/trade/auto_buy")
        assert r.status_code == 200 and r.json()["accepted"] is True
        assert _wait(lambda: app._auto_buy.last is not None)
        d = c.get("/api/trade/auto_buy/last").json()
        assert d["config"]["formula_name"] == "QUANTQQ"
        assert d["last"]["selected"] == 0 and d["last"]["bought"] == 0
    finally:
        app.stop()


# ═══════════════════════════════════════════════════════════════
# 2026-08-01 P0-4 (H4): EOD 空快照守卫
# ═══════════════════════════════════════════════════════════════

def test_eod_empty_positions_skips_snapshot(cfg, tmp_path):
    """query_positions 空列表 → 跳过归档, audit 留痕 eod_skip,
    日报仍推送 (日报读 asset, 与持仓查询独立)。"""
    clock = [_ts("15:05")]
    # 零持仓启动 → query_positions 返回空
    app = _start(_make_app(cfg, clock, positions={}))
    app._on_eod(notify_daily=True)
    rows = app.store._conn.execute(
        "SELECT kind, message FROM audit WHERE kind IN ('eod_skip', 'eod')"
    ).fetchall()
    kinds = [r[0] for r in rows]
    assert "eod_skip" in kinds, "空持仓应跳过归档 (eod_skip)"
    assert "eod" not in kinds, "空持仓不应写入 position_snapshot (eod)"
    # 日报不受影响: _notify_daily 查 asset 而非 positions
    app.stop()


def test_eod_with_positions_saves_snapshot(cfg, tmp_path):
    """正常有持仓 → 归档 position_snapshot, audit 记 eod。"""
    clock = [_ts("15:05")]
    app = _start(_make_app(cfg, clock,
                           positions={CODE: {"volume": 500, "can_use": 500,
                                             "avg_cost": 10.0}}))
    app._on_eod(notify_daily=False)  # 不发日报, 不依赖网关 asset 查询
    rows = app.store._conn.execute(
        "SELECT kind FROM audit WHERE kind='eod'"
    ).fetchall()
    # 启动时 _startup_catchup 可能也触发了一次 eod (hhmm=15:05),
    # 所以 >= 1 即可 —— 关键是快照落地了
    assert len(rows) >= 1
    # 快照落地
    snap = app.store.load_position_snapshot()
    assert CODE in snap
    app.stop()
