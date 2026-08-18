"""trade/api.py 端点测试 (审计L12修复, 2026-07-26).

TestClient + FakeGateway 装配。锁住: 读端点形状、code 入参正则
(畸形 422 不进队列)、qty 整手校验、命令端点只 put 不执行
(accepted 语义)。token 鉴权留 P2 (审计L12④裁决: 不引入 server.py
改动, 本文件无 token 用例)。
"""
import datetime as _dt
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient

from trade.api import create_api_app
from trade.book import DIRECTION_BUY, DIRECTION_SELL
from trade.config import TradeConfig
from trade_main import TradeApp

SH = "600519.SH"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 2026-08-18: 锁盘前分支不随真实钟点漂移 —— 默认走连续竞价(非盘前),
    # 盘前行为由 test_positions_day_pnl_pre_open 单独覆盖。
    monkeypatch.setattr("trade.monitor.trading_session",
                        lambda now=None: "continuous")
    cfg = TradeConfig(
        account_id="API", fake_sdk=True,
        db_path=str(tmp_path / "t.db"),
        raw_log_path=str(tmp_path / "r.jsonl"),
        kill_flag_path=str(tmp_path / "KILL"),
    )
    app = TradeApp(cfg, fake=True, fake_gateway_kwargs={
        "cash": 1_000_000.0,
        "positions": {SH: {"volume": 1000, "can_use": 1000, "avg_cost": 10.0}}},
        config_path=str(tmp_path / "trade.yaml"))
    app.start(start_timers=False)
    yield TestClient(create_api_app(app)), app
    app.stop()


def _wait(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _pin_trading_day(monkeypatch, y, m, d):
    """钉死"最近交易日"锚点 = y-m-d, 返回 (start, end) epoch 秒。

    当日盈亏测试据此脱离真实"今天" —— 用 time.time() 灌买点会在周末/
    节假日落不进锚定日 (真实今天不是交易日), 导致当日盈亏误按昨收算。
    """
    start = _dt.datetime(y, m, d).timestamp()
    end = (_dt.datetime(y, m, d) + _dt.timedelta(days=1)).timestamp()
    monkeypatch.setattr("trade.api._last_trading_day_range",
                        lambda now=None: (start, end))
    return start, end


def test_status_shape(client):
    c, app = client
    d = c.get("/api/trade/status").json()
    assert d["connected"] is True
    assert d["kill_active"] is False
    assert d["reconciled"] is True
    assert d["monitor_healthy"] in (True, False, None)
    # 2026-07-27 裁决②: 时段 + 人话原因字段
    assert d["session"] in ("pre_open", "auction", "continuous",
                            "lunch", "closed")
    assert isinstance(d["monitor_reason"], str) and d["monitor_reason"]


def test_positions_shape(client):
    c, _ = client
    d = c.get("/api/trade/positions").json()
    assert d["positions"][0]["code"] == SH
    assert d["positions"][0]["volume"] == 1000
    assert "tiers_done" in d["positions"][0]
    # 2026-07-27 裁决③: etf/managed 字段
    assert d["positions"][0]["etf"] is False
    assert d["positions"][0]["managed"] is True
    # 2026-07-31: 当日涨跌字段存在 (无行情快照时为 None, 不崩)
    assert "day_chg_pct" in d["positions"][0]
    assert "day_chg_amt" in d["positions"][0]


def test_positions_day_change(client):
    """2026-07-31: 有昨收+现价时, 当日涨幅=(现价/昨收-1),
    当日盈亏额=(现价-昨收)×数量。"""
    c, app = client
    app.monitor.on_quote(SH, {"last": 11.0, "bid1": 10.9, "prev_close": 10.0})
    d = c.get("/api/trade/positions").json()
    p = d["positions"][0]
    assert p["day_chg_pct"] == 10.0
    assert p["day_chg_amt"] == (11.0 - 10.0) * p["volume"]


def test_positions_day_pnl_today_only(monkeypatch, client):
    """2026-08-12 (300119 事件): 尾盘新建仓的票, 当日盈亏按买入价算。
    现价11/买入@11/昨收10 → 当日盈亏=0 (旧逻辑会错误地得 (11-10)*vol);
    但当日涨幅仍是客观价格口径 10% (现价/昨收-1), 不被买入时机清零。"""
    c, app = client
    SZ = "300119.SZ"
    day_start, _ = _pin_trading_day(monkeypatch, 2026, 8, 14)
    app.store.save_trade({"traded_id": "tb_r", "order_id": "ob_r", "code": SZ,
        "direction": DIRECTION_BUY, "price": 11.0, "qty": 1000,
        "amount": 11000.0, "ts": day_start + 3600})
    app.book.apply_trade("tb_r", "ob_r", SZ, DIRECTION_BUY, 11.0, 1000, strategy="测试")
    app.monitor.on_quote(SZ, {"last": 11.0, "bid1": 10.9, "prev_close": 10.0})
    d = c.get("/api/trade/positions").json()
    p = {x["code"]: x for x in d["positions"]}[SZ]
    assert p["day_chg_amt"] == 0.0     # 全今仓: 买入价=现价 → 0 (不是 +1000)
    assert p["day_chg_pct"] == 10.0    # 客观涨幅 (11/10-1), 与买入时机无关


def test_positions_day_pnl_mixed(monkeypatch, client):
    """2026-08-12: 混合仓精确化。昨仓500@9 + 今买500@11, 现价11/昨收10。
    当日盈亏 = 昨仓(11-10)*500 + 今买(11-11)*500 = 500;
    当日涨幅仍是客观价格口径 10% (11/10-1), 与持仓结构无关。"""
    c, app = client
    SZ = "300120.SZ"
    day_start, _ = _pin_trading_day(monkeypatch, 2026, 8, 14)
    app.store.save_trade({"traded_id": "tb_y", "order_id": "ob_y", "code": SZ,
        "direction": DIRECTION_BUY, "price": 9.0, "qty": 500,
        "amount": 4500.0, "ts": day_start - 86400})    # 锚定日之前的老仓
    app.store.save_trade({"traded_id": "tb_t", "order_id": "ob_t", "code": SZ,
        "direction": DIRECTION_BUY, "price": 11.0, "qty": 500,
        "amount": 5500.0, "ts": day_start + 3600})     # 锚定日新买
    app.book.apply_trade("tb_y", "ob_y", SZ, DIRECTION_BUY, 9.0, 500, strategy="测试")
    app.book.apply_trade("tb_t", "ob_t", SZ, DIRECTION_BUY, 11.0, 500, strategy="测试")
    app.monitor.on_quote(SZ, {"last": 11.0, "bid1": 10.9, "prev_close": 10.0})
    d = c.get("/api/trade/positions").json()
    p = {x["code"]: x for x in d["positions"]}[SZ]
    assert p["day_chg_amt"] == 500.0
    assert p["day_chg_pct"] == 10.0   # 客观涨幅 (11/10-1), 与持仓结构无关


def test_last_trading_day_range_walks_back(monkeypatch):
    """2026-08-16: 最近交易日锚点 —— 周末 (非交易日) 应回退到最近周五,
    而不是用墙钟今天 (周日)。日历只认 2026-08-14(周五) 是交易日。"""
    from trade import api
    now = _dt.datetime(2026, 8, 16, 18, 0, 0).timestamp()   # 周日
    monkeypatch.setattr(
        "trade.monitor.is_trading_day_cached",
        lambda d: d == _dt.date(2026, 8, 14))
    lo, hi = api._last_trading_day_range(now)
    assert lo == _dt.datetime(2026, 8, 14).timestamp()
    assert hi == _dt.datetime(2026, 8, 15).timestamp()


def test_positions_day_pnl_last_trading_day(monkeypatch, client):
    """2026-08-16 (中捷精工事件) 回归: 上一交易日尾盘买入的票, 今天虽是
    非交易日 (周末), 当日盈亏也要按买入均价算 (≈0), 不能按 (现价-昨收)
    ×数量 把买入前当天的涨幅算成盈利 (旧 bug 得 +1770); 但当日涨幅保持
    客观价格口径 (现价/昨收-1 ≈ 9.95%), 不被尾盘买入清零。"""
    c, app = client
    SZ = "301072.SZ"
    fri_start, _ = _pin_trading_day(monkeypatch, 2026, 8, 14)
    # 周五 14:54 尾盘买入 500@39.13
    app.store.save_trade({"traded_id": "tb_w", "order_id": "ob_w", "code": SZ,
        "direction": DIRECTION_BUY, "price": 39.13, "qty": 500,
        "amount": 19565.0, "ts": fri_start + 14 * 3600 + 54 * 60})
    app.book.apply_trade("tb_w", "ob_w", SZ, DIRECTION_BUY, 39.13, 500,
                         strategy="测试")
    # 现价 39.13 = 买入价, 昨收 35.59 → 锚错会得 (39.13-35.59)*500 = 1770
    app.monitor.on_quote(SZ, {"last": 39.13, "bid1": 39.12,
                              "prev_close": 35.59})
    d = c.get("/api/trade/positions").json()
    p = {x["code"]: x for x in d["positions"]}[SZ]
    assert p["day_chg_amt"] == 0.0
    # 客观涨幅 = (39.13/35.59-1)*100 ≈ 9.95%, 不是 0
    assert p["day_chg_pct"] == round((39.13 / 35.59 - 1) * 100, 2)


def test_positions_day_pnl_pre_open(monkeypatch, client):
    """2026-08-18 (159949 事件): 盘前(还没开盘)当日盈亏应为 0, 不能把
    前一交易日的全天涨幅算进来 —— QMT prev_close 未翻日(仍是前前交易日
    收盘), 直接 (last-prev_close) 会误得 +13950。"""
    c, app = client
    SZ = "159949.SZ"
    # 前一个交易日尾盘买入 263200@1.766 (锚定日 08-18 之前)
    day_start, _ = _pin_trading_day(monkeypatch, 2026, 8, 18)
    app.store.save_trade({"traded_id": "tb_p", "order_id": "ob_p", "code": SZ,
        "direction": DIRECTION_BUY, "price": 1.766, "qty": 263200,
        "amount": 464811.2, "ts": day_start - 86400})
    app.book.apply_trade("tb_p", "ob_p", SZ, DIRECTION_BUY, 1.766, 263200,
                         strategy="测试")
    # 现价=08-17收盘1.765, 昨收未翻日=08-14收盘1.712
    app.monitor.on_quote(SZ, {"last": 1.765, "bid1": 1.764, "prev_close": 1.712})
    monkeypatch.setattr("trade.monitor.trading_session",
                        lambda now=None: "pre_open")
    d = c.get("/api/trade/positions").json()
    p = {x["code"]: x for x in d["positions"]}[SZ]
    assert p["day_chg_amt"] == 0.0        # 盘前: 今日无变动 (不是 13950)


def test_positions_closed_realized_pnl(monkeypatch, client):
    """2026-08-11: volume=0 的已平仓票 (幽灵持仓) 应展示真实已实现盈亏,
    不是一堆 0。后端从 trades 算买入均价/卖出均价/已实现盈亏/出场时间,
    标 closed=True 供前端灰显+徽标。
    2026-08-17: 平仓票也显示当日涨幅(客观) + 当日盈亏(锚昨收, ≠已实现盈亏)。"""
    c, app = client
    SZ = "000001.SZ"
    day_start, _ = _pin_trading_day(monkeypatch, 2026, 8, 14)
    # 买 1000@10 (锚定日前一天), 卖 1000@11 (锚定日) —— 整周期闭环
    app.store.save_trade({"traded_id": "tb1", "order_id": "ob1", "code": SZ,
        "direction": DIRECTION_BUY, "price": 10.0, "qty": 1000,
        "amount": 10000.0, "ts": day_start - 86400})
    app.store.save_trade({"traded_id": "ts1", "order_id": "os1", "code": SZ,
        "direction": DIRECTION_SELL, "price": 11.0, "qty": 1000,
        "amount": 11000.0, "pnl_amount": 1000.0, "pnl_pct": 10.0,
        "ts": day_start + 3600})
    # book 里造幽灵: 买入再卖出 → volume 归零但 key 保留 (模拟卖光后未清的持仓)
    app.book.apply_trade("tb1", "ob1", SZ, DIRECTION_BUY, 10.0, 1000, strategy="测试")
    app.book.apply_trade("ts1", "os1", SZ, DIRECTION_SELL, 11.0, 1000)
    assert app.book.snapshot()["positions"][SZ].volume == 0
    # 昨收 10.5 → 当日盈亏=(卖出均价11-昨收10.5)×1000=500 (锚昨收, 非买入均价10)
    app.monitor.on_quote(SZ, {"last": 11.0, "bid1": 10.9, "prev_close": 10.5})

    d = c.get("/api/trade/positions").json()
    p = {x["code"]: x for x in d["positions"]}[SZ]
    assert p["closed"] is True
    assert p["volume"] == 0
    assert p["avg_cost"] == 10.0          # 买入均价 (不再是 0)
    assert p["sell_avg"] == 11.0          # 卖出均价
    assert p["buy_qty"] == 1000
    assert p["pnl"] == 1000.0             # 已实现盈亏 = (卖出11-买入10)×1000
    assert p["pnl_pct"] == 10.0           # 已实现%
    assert p["exit_ts"] is not None
    assert p["market_value"] is None      # 平仓 → 无市值
    assert p["day_chg_pct"] == round((11 / 10.5 - 1) * 100, 2)  # 客观涨幅
    assert p["day_chg_amt"] == 500.0      # 当日盈亏=(11-10.5)×1000, ≠已实现 1000


def test_read_endpoints_200(client):
    c, _ = client
    for url in ("/api/trade/orders", "/api/trade/reconciles",
                "/api/trade/audits?limit=10&offset=0"):
        assert c.get(url).status_code == 200


def test_buy_valid_code_accepted(client):
    c, _ = client
    r = c.post("/api/trade/buy", json={"code": SH, "qty": 100, "price": 10.0})
    assert r.status_code == 200 and r.json()["accepted"] is True


def test_buy_malformed_code_rejected_422(client):
    """审计L12修复: 畸形 code 在 HTTP 边界 422, 不进事件队列。"""
    c, app = client
    for bad in ("60051.SH", "600519.XX", "ABCDEF.SH", "600519.SH; DROP", ""):
        r = c.post("/api/trade/buy", json={"code": bad, "qty": 100})
        assert r.status_code == 422, bad
    assert app.store._conn.execute(
        "SELECT COUNT(*) FROM audit WHERE kind='manual_buy'").fetchone()[0] == 0


def test_sell_malformed_code_rejected_422(client):
    c, _ = client
    assert c.post("/api/trade/sell", json={"code": "bad"}).status_code == 422
    assert c.post("/api/trade/sell", json={"code": SH}).status_code == 200


def test_buy_qty_must_be_lot(client):
    c, _ = client
    assert c.post("/api/trade/buy", json={"code": SH, "qty": 150}).status_code == 422
    assert c.post("/api/trade/buy", json={"code": SH, "qty": 0}).status_code == 422


def test_command_endpoints_accepted_only(client):
    """命令端点全部只 put + accepted (薄层语义, 不同步执行)。"""
    c, _ = client
    for url in ("/api/trade/kill", "/api/trade/unkill", "/api/trade/ladder"):
        r = c.post(url)
        assert r.status_code == 200 and r.json()["accepted"] is True
    r = c.post("/api/trade/cancel", json={"order_id": "FAKE000001"})
    assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
# 设置面板: GET/PUT /api/trade/config (2026-07-26)
# ═══════════════════════════════════════════════════════════════

def test_get_config_shape(client):
    c, _ = client
    d = c.get("/api/trade/config").json()
    assert d["account_id"] == "API"
    assert d["daily_loss_limit"] == 0.05
    assert d["stop"]["priority"] == "trailing_first"
    assert d["stop"]["cost_stop"]["threshold"] == -0.12
    assert d["stop"]["ladder_tp"]["levels"][0] == {"profit": 0.06,
                                                   "sell_ratio": 0.3}
    assert d["position_sizing"]["max_positions"] == 10
    assert d["reconcile_times"] == ["09:35", "11:30", "14:55", "15:05"]
    assert d["sync_interval_sec"] == 180   # 2026-07-30 增量同步间隔


def test_put_config_hot_swap_and_yaml_writeback(client, tmp_path):
    """合法 PUT → accepted → 消费者热替换 → GET 反映新值 + yaml 写回同值
    + monitor/executor 引用已换 + 运行时状态 (_triggered) 保留 + audit。"""
    from trade.config import load_trade_config
    c, app = client
    body = c.get("/api/trade/config").json()
    body["stop"]["trailing_stop"]["drawdown"] = 0.03
    body["daily_loss_limit"] = 0.08
    app.monitor._triggered.add("SENTINEL")     # 运行时状态不应被重置
    r = c.put("/api/trade/config", json=body)
    assert r.status_code == 200
    assert set(r.json()["changed"]) == {"stop.trailing_stop.drawdown",
                                        "daily_loss_limit"}
    assert _wait(lambda: app.config.stop.trailing_stop.drawdown == 0.03)
    # GET 反映新值
    d = c.get("/api/trade/config").json()
    assert d["stop"]["trailing_stop"]["drawdown"] == 0.03
    assert d["daily_loss_limit"] == 0.08
    # 热替换: monitor/executor 引用已换, risk 快照字段已换
    assert app.monitor._cfg.stop.trailing_stop.drawdown == 0.03
    assert app.executor._cfg.daily_loss_limit == 0.08
    assert app.risk._loss_limit == 0.08
    # 运行时状态保留
    assert "SENTINEL" in app.monitor._triggered
    # yaml 写回可重新 load 得同值
    reloaded = load_trade_config(tmp_path / "trade.yaml")
    assert reloaded.stop.trailing_stop.drawdown == 0.03
    assert reloaded.daily_loss_limit == 0.08
    # audit 留痕
    rows = app.store._conn.execute(
        "SELECT kind FROM audit WHERE kind='config_update'").fetchall()
    assert rows


def test_put_config_partial_body_keeps_account(client, tmp_path):
    """深合并语义: 只发 stop 字段, account_id/qmt_path 等不被清
    (关键安全语义 —— 面板表单不含这些字段, 全量替换会清掉真账号)。"""
    c, app = client
    r = c.put("/api/trade/config",
              json={"stop": {"trailing_stop": {"drawdown": 0.02}}})
    assert r.status_code == 200
    assert _wait(lambda: app.config.stop.trailing_stop.drawdown == 0.02)
    assert app.config.account_id == "API"          # 没被默认值覆盖
    d = c.get("/api/trade/config").json()
    assert d["account_id"] == "API"


def test_put_config_invalid_422_and_untouched(client, tmp_path):
    """非法配置 → 422 + 字段错误; 生效配置与 yaml 都不动。"""
    c, app = client
    yaml_path = tmp_path / "trade.yaml"
    body = c.get("/api/trade/config").json()
    body["force_market_after"] = "99:99"
    r = c.put("/api/trade/config", json=body)
    assert r.status_code == 422 and "force_market_after" in r.json()["detail"]
    # 坏 levels 结构
    body = c.get("/api/trade/config").json()
    body["stop"]["ladder_tp"]["levels"] = [[0.05, 0.33]]
    assert c.put("/api/trade/config", json=body).status_code == 422
    # 负 activation
    body = c.get("/api/trade/config").json()
    body["stop"]["trailing_stop"]["activation"] = -0.01
    assert c.put("/api/trade/config", json=body).status_code == 422
    # sizing 上下限倒挂
    body = c.get("/api/trade/config").json()
    body["position_sizing"]["min_buy_amount"] = 30000
    assert c.put("/api/trade/config", json=body).status_code == 422
    # 生效配置未变
    assert app.config.stop.trailing_stop.drawdown == 0.01
    # yaml 未动 (从未有过合法 PUT → 文件不存在)
    assert not yaml_path.exists()


# ═══════════════════════════════════════════════════════════════
# 2026-07-30: 交易记录 TAB — /api/trade/deals + orders 历史日期
# ═══════════════════════════════════════════════════════════════

def test_deals_today_and_history_date(client):
    """成交记录: 缺省当日; date=YYYYMMDD 查历史; 行内含 name 字段。"""
    import datetime as _dt
    c, app = client
    app.store.save_trade({"traded_id": "T-1", "order_id": "O-1", "code": SH,
                          "direction": 23, "price": 10.0, "qty": 100,
                          "ts": time.time()})
    d = c.get("/api/trade/deals").json()
    assert len(d["deals"]) == 1
    row = d["deals"][0]
    assert row["traded_id"] == "T-1" and row["code"] == SH
    assert row["qty"] == 100 and "name" in row
    assert row["source"] == "system"   # 2026-07-30: 成交来源 (系统/手工)
    today = _dt.datetime.now().strftime("%Y%m%d")
    assert len(c.get(f"/api/trade/deals?date={today}").json()["deals"]) == 1
    yesterday = (_dt.datetime.now() - _dt.timedelta(days=1)).strftime("%Y%m%d")
    assert c.get(f"/api/trade/deals?date={yesterday}").json()["deals"] == []


def test_deals_invalid_date_422(client):
    c, _ = client
    assert c.get("/api/trade/deals?date=2026-07-30").status_code == 422
    assert c.get("/api/trade/deals?date=abc").status_code == 422


def test_deals_start_end_range(client):
    """2026-08-13: start/end=YYYYMMDD 闭区间范围查询 (分析页盈亏分布要全量历史);
    只传一个落回当日; 非法格式 422。"""
    import datetime as _dt
    c, app = client
    today = _dt.datetime.now()
    old = (today - _dt.timedelta(days=5)).strftime("%Y%m%d")
    old_ts = _dt.datetime.strptime(old, "%Y%m%d").timestamp() + 3600
    app.store.save_trade({"traded_id": "T-OLD", "order_id": "O-OLD",
                          "code": SH, "direction": 23, "price": 10.0,
                          "qty": 100, "ts": old_ts})
    app.store.save_trade({"traded_id": "T-NEW", "order_id": "O-NEW",
                          "code": SH, "direction": 24, "price": 11.0,
                          "qty": 100, "ts": time.time()})
    today_s = today.strftime("%Y%m%d")
    d = c.get(f"/api/trade/deals?start={old}&end={today_s}").json()
    assert {r["traded_id"] for r in d["deals"]} == {"T-OLD", "T-NEW"}
    # end 当日 inclusive: end=昨天 → 只有旧的那笔
    yesterday = (today - _dt.timedelta(days=1)).strftime("%Y%m%d")
    d2 = c.get(f"/api/trade/deals?start={old}&end={yesterday}").json()
    assert [r["traded_id"] for r in d2["deals"]] == ["T-OLD"]
    # 非法格式 422
    assert c.get("/api/trade/deals?start=abc&end=20260813").status_code == 422


def test_orders_history_date_param(client):
    """委托记录: date=YYYYMMDD 查历史; 非法日期 422。"""
    import datetime as _dt
    c, app = client
    assert c.get("/api/trade/orders?date=bad").status_code == 422
    yesterday = (_dt.datetime.now() - _dt.timedelta(days=1)).strftime("%Y%m%d")
    assert c.get(f"/api/trade/orders?date={yesterday}").json()["orders"] == []
    # 默认参数行为不变: 缺省仍是当日
    assert c.get("/api/trade/orders").status_code == 200


# ═══════════════════════════════════════════════════════════════
# 2026-08-01 分页
# ═══════════════════════════════════════════════════════════════

def test_deals_pagination_stable_across_same_ts(client):
    """同秒多笔成交翻页不重复不漏 —— ORDER BY ts DESC, traded_id DESC
    决胜键保证 tie 内顺序一致。"""
    c, app = client
    same_ts = time.time()
    for i in range(5):
        app.store.save_trade({
            "traded_id": f"T-pg-{i:04d}", "order_id": f"O-pg-{i:04d}",
            "code": "000001.SZ", "direction": 23,  # DIRECTION_BUY
            "price": 10.0, "qty": 100, "amount": 1000.0,
            "ts": same_ts, "source": "system", "reason": ""})
    p1 = c.get("/api/trade/deals?limit=2&offset=0").json()["deals"]
    assert len(p1) == 2
    p2 = c.get("/api/trade/deals?limit=2&offset=2").json()["deals"]
    assert len(p2) == 2
    p3 = c.get("/api/trade/deals?limit=2&offset=4").json()["deals"]
    assert len(p3) == 1
    ids = {d["traded_id"] for d in p1 + p2 + p3}
    assert len(ids) == 5, "跨页不应有重复或遗漏"


def test_deals_limit_out_of_range_rejected(client):
    """limit < 1 或 > 5000 → 422。"""
    c, _ = client
    assert c.get("/api/trade/deals?limit=-1").status_code == 422
    assert c.get("/api/trade/deals?limit=0").status_code == 422
    assert c.get("/api/trade/deals?limit=5001").status_code == 422


def test_orders_default_limit_within_bounds(client):
    """orders 缺省 limit=200 正常返回 + offset 翻页。"""
    c, app = client
    # 造 3 笔委托
    now = time.time()
    for i in range(3):
        app.store.save_order({
            "order_id": f"O-ord-{i:04d}", "remark": f"X{i}", "code": "000001.SZ",
            "direction": 23, "price": 10.0, "qty": 100, "status": 50})
    r = c.get("/api/trade/orders?limit=2&offset=0").json()
    assert len(r["orders"]) == 2
    r2 = c.get("/api/trade/orders?limit=2&offset=2").json()
    assert len(r2["orders"]) == 1


# ═══════════════════════════════════════════════════════════════
# 2026-08-07 盘后日报增强 (/deals 读 pnl 列 + /analysis/daily_report)
# ═══════════════════════════════════════════════════════════════

def test_deals_reads_pnl_column(client):
    """2026-08-07: /deals 卖出盈亏改读 trades.pnl_amount/pnl_pct 列 (book 成本法),
    不再 SQL 现算。"""
    c, app = client
    app.store.save_trade({"traded_id": "T-S", "order_id": "O-S", "code": SH,
                          "direction": 24, "price": 11.0, "qty": 100,
                          "amount": 1100.0, "ts": time.time(),
                          "pnl_amount": 123.45, "pnl_pct": 10.0})
    d = c.get("/api/trade/deals").json()
    row = [r for r in d["deals"] if r["traded_id"] == "T-S"][0]
    assert row["pnl_amount"] == 123.45 and row["pnl_pct"] == 10.0


def test_deals_sell_without_pnl_column_returns_none(client):
    """卖出但 pnl 列=0 (上线前历史行) → pnl_amount/pnl_pct = None
    (不回溯 SQL 现算, 保持单一 book 口径)。"""
    c, app = client
    app.store.save_trade({"traded_id": "T-N", "order_id": "O-N", "code": SH,
                          "direction": 24, "price": 11.0, "qty": 100,
                          "amount": 1100.0, "ts": time.time()})  # 无 pnl → 默认 0
    d = c.get("/api/trade/deals").json()
    row = [r for r in d["deals"] if r["traded_id"] == "T-N"][0]
    assert row["pnl_amount"] is None and row["pnl_pct"] is None


def test_daily_report_endpoint_roundtrip(client):
    """2026-08-07: /analysis/daily_report?date= 读 daily_report 表;
    缺省取最新; 无记录 → null; 非法日期 422。"""
    c, app = client
    today = time.strftime("%Y-%m-%d")
    payload = {"total_asset": 1e6, "day_pnl": 500.0, "buy_count": 1}
    app.store.daily_report.save(today, payload)
    assert c.get(f"/api/trade/analysis/daily_report?date={today}").json()["report"] == payload
    assert c.get("/api/trade/analysis/daily_report").json()["report"] == payload  # 缺省=最新
    assert c.get("/api/trade/analysis/daily_report?date=2099-12-31").json()["report"] is None
    assert c.get("/api/trade/analysis/daily_report?date=bad").status_code == 422
