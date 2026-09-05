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

    2026-09-02: positions 计算体下沉 trade/view_calc.py (热加载层),
    _last_trading_day_range 单一实现移到 trade.analysis —— patch 两处
    (analysis 是实现处, api 是历史兼容), 所有既有测试语义不变。
    """
    start = _dt.datetime(y, m, d).timestamp()
    end = (_dt.datetime(y, m, d) + _dt.timedelta(days=1)).timestamp()
    for target in ("trade.analysis._last_trading_day_range",
                   "trade.api._last_trading_day_range"):
        try:
            monkeypatch.setattr(target, lambda now=None: (start, end),
                                raising=False)
        except (AttributeError, ImportError):
            pass
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


def test_positions_closed_day_pnl_multi_day_sells(monkeypatch, client):
    """2026-09-02 (518880 事件) 回归: 平仓票跨日分批卖出时, 当日盈亏只能算
    最近交易日实际卖出的那部分, 不能用整周期累计卖出均价×累计卖出量。

    真实事件: 518880 于 08-21 卖 5600@9.385, 09-02 (最近交易日) 又卖
    遗产仓 200@8.892, 昨收 9.118。旧代码拿累计卖均 9.368 × 累计量 5800
    得 (9.368-9.118)*5800 = +1450 (页面显示"当日盈亏 +1450"); 黄金当日
    实际跌 -2.37%, 正确答案是今日卖出腿 (8.892-9.118)*200 = -45.2。"""
    c, app = client
    ETF = "518880.SH"
    day_start, _ = _pin_trading_day(monkeypatch, 2026, 8, 14)
    # 买入 5600@9.05 (锚定日前一周) → book 持仓
    app.store.save_trade({"traded_id": "tg_b", "order_id": "og_b", "code": ETF,
        "direction": DIRECTION_BUY, "price": 9.05, "qty": 5600,
        "amount": 50680.0, "ts": day_start - 6 * 86400})
    app.book.apply_trade("tg_b", "og_b", ETF, DIRECTION_BUY, 9.05, 5600,
                         strategy="测试")
    # 锚定日前一天: 卖出 5600@9.385 (历史卖出, 与"当日"无关)
    app.store.save_trade({"traded_id": "tg_s1", "order_id": "og_s1", "code": ETF,
        "direction": DIRECTION_SELL, "price": 9.385, "qty": 5600,
        "amount": 52556.0, "pnl_amount": 1869.1, "pnl_pct": 3.69,
        "ts": day_start - 86400})
    app.book.apply_trade("tg_s1", "og_s1", ETF, DIRECTION_SELL, 9.385, 5600)
    assert app.book.snapshot()["positions"][ETF].volume == 0
    # 锚定日当天 14:54: 卖出遗产仓 200@8.892 (只进 trades, book volume 保持 0)
    app.store.save_trade({"traded_id": "tg_s2", "order_id": "og_s2", "code": ETF,
        "direction": DIRECTION_SELL, "price": 8.892, "qty": 200,
        "amount": 1778.4, "pnl_amount": -58.6, "pnl_pct": -3.19,
        "ts": day_start + 14 * 3600 + 54 * 60})
    # 现价 8.902 / 昨收 9.118 (2026-09-02 真实行情感值)
    app.monitor.on_quote(ETF, {"last": 8.902, "bid1": 8.90, "prev_close": 9.118})

    d = c.get("/api/trade/positions").json()
    p = {x["code"]: x for x in d["positions"]}[ETF]
    assert p["closed"] is True
    assert p["volume"] == 0
    # 已实现盈亏是整周期口径, 不受本修复影响: 1869.1 + (-58.6) = 1810.5
    assert p["pnl"] == 1810.5
    # 当日盈亏只算今日卖出的 200 股: 1778.4 - 9.118*200 = -45.2
    # (旧代码: 累计卖均 9.368*5800 → +1450.0, 红色断言)
    assert p["day_chg_amt"] == -45.2


def test_positions_closed_day_pnl_no_sell_today(monkeypatch, client):
    """2026-09-02 修复补充: 平仓票最近交易日无卖出 (全部卖在更早) →
    当日盈亏 = 0, 不受历史卖出均价影响。"""
    c, app = client
    SZ = "000002.SZ"
    day_start, _ = _pin_trading_day(monkeypatch, 2026, 8, 14)
    app.store.save_trade({"traded_id": "tb_n", "order_id": "ob_n", "code": SZ,
        "direction": DIRECTION_BUY, "price": 10.0, "qty": 1000,
        "amount": 10000.0, "ts": day_start - 3 * 86400})
    app.store.save_trade({"traded_id": "ts_n", "order_id": "os_n", "code": SZ,
        "direction": DIRECTION_SELL, "price": 11.0, "qty": 1000,
        "amount": 11000.0, "pnl_amount": 1000.0, "pnl_pct": 10.0,
        "ts": day_start - 2 * 86400})      # 卖在锚定日前两天
    app.book.apply_trade("tb_n", "ob_n", SZ, DIRECTION_BUY, 10.0, 1000,
                         strategy="测试")
    app.book.apply_trade("ts_n", "os_n", SZ, DIRECTION_SELL, 11.0, 1000)
    app.monitor.on_quote(SZ, {"last": 11.0, "bid1": 10.9, "prev_close": 10.5})
    d = c.get("/api/trade/positions").json()
    p = {x["code"]: x for x in d["positions"]}[SZ]
    assert p["closed"] is True
    assert p["day_chg_amt"] == 0.0         # 非当日卖出 → 0


# ═══════════════════════════════════════════════════════════════
# 2026-09-02: 展示层热加载 — /api/trade/positions 计算体下沉
# trade/view_calc.py, POST /api/trade/reload_view 非交易时段重载
# ═══════════════════════════════════════════════════════════════

def test_positions_uses_latest_view_calc(monkeypatch, client):
    """热生效机制: 路由函数内 import 每次请求从 sys.modules 取最新
    view_calc —— reload 之后的新代码即刻生效。本测试用假模块替换
    sys.modules['trade.view_calc'] 模拟 reload 的结果, 断言页面输出
    立刻跟着变 (不重启进程)。"""
    import sys
    import types
    c, _ = client
    fake = types.ModuleType("trade.view_calc")

    def build_positions_view(trade_app):
        return {"positions": [{"code": "FAKE.SH", "marker": 1}],
                "closed": [], "ts": 1.0}

    fake.build_positions_view = build_positions_view
    monkeypatch.setitem(sys.modules, "trade.view_calc", fake)
    d = c.get("/api/trade/positions").json()
    assert d["positions"][0]["code"] == "FAKE.SH"
    assert d["positions"][0]["marker"] == 1


def test_reload_view_rejected_in_continuous(client):
    """连续竞价时段调 reload 端点 → 403 拒绝 + audit 留痕, 模块不动。
    (client fixture 已把 trading_session 钉为 continuous)"""
    c, app = client
    r = c.post("/api/trade/reload_view")
    assert r.status_code == 403
    assert app.store._conn.execute(
        "SELECT COUNT(*) FROM audit WHERE kind='view_reload_rejected'"
    ).fetchone()[0] >= 1


def test_reload_view_ok_when_closed(monkeypatch, client):
    """收盘后时段 (closed) 调 reload 端点 → 200 + 重载真实 view_calc
    + 烟测通过 + audit 留痕 view_reload。"""
    c, app = client
    monkeypatch.setattr("trade.monitor.trading_session",
                        lambda now=None: "closed")
    r = c.post("/api/trade/reload_view")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["module"] == "trade.view_calc"
    assert app.store._conn.execute(
        "SELECT COUNT(*) FROM audit WHERE kind='view_reload'"
    ).fetchone()[0] >= 1


def test_reload_view_syntax_error_422(monkeypatch, client, tmp_path):
    """view_calc 源文件语法坏了 → 422 拒绝重载 (进程内保持旧版可用),
    audit 留痕 view_reload_syntax_error。"""
    c, app = client
    monkeypatch.setattr("trade.monitor.trading_session",
                        lambda now=None: "closed")
    import trade.view_calc as vc
    bad = tmp_path / "bad_view_calc.py"
    bad.write_text("def broken(:\n", encoding="utf-8")
    monkeypatch.setattr(vc, "__file__", str(bad))
    r = c.post("/api/trade/reload_view")
    assert r.status_code == 422
    assert app.store._conn.execute(
        "SELECT COUNT(*) FROM audit WHERE kind='view_reload_syntax_error'"
    ).fetchone()[0] >= 1
    # 模块未被重载: 原函数仍可用
    assert callable(vc.build_positions_view)


def test_read_endpoints_200(client):
    c, _ = client
    for url in ("/api/trade/orders", "/api/trade/reconciles",
                "/api/trade/audits?limit=10&offset=0"):
        assert c.get(url).status_code == 200


def test_buy_valid_code_accepted(client):
    c, _ = client
    r = c.post("/api/trade/buy", json={"code": SH, "qty": 100, "price": 10.0})
    assert r.status_code == 200 and r.json()["accepted"] is True


def test_buy_etf_without_price_quote_fallback(client):
    """2026-08-25 人工买入 ETF 支持: 未持仓 ETF 不在行情订阅名单,
    monitor 缓存必无价; 限价留空时先走 query_quotes 一次性取价
    (回填缓存 + 顺带订阅), 不再直接 buy_fail_closed 拒单。"""
    c, app = client
    ETF = "518880.SH"
    # 网关侧有价但 monitor 缓存无 (直接塞快照不 fire 事件, 模拟未订阅代码)
    app.gateway._quotes[ETF] = {"last": 5.6, "bid1": 5.59, "ask1": 5.61,
                                "high": 5.65, "prev_close": 5.58}
    r = c.post("/api/trade/buy", json={"code": ETF, "qty": 400})
    assert r.status_code == 200
    # 等消费者线程执行: 按取到的 5.6 下单 (400×5.6=2240 ≥ 2000 金额下限)
    assert _wait(lambda: any(
        o["code"] == ETF and o["direction"] == DIRECTION_BUY
        for o in app.gateway.query_orders()))
    order = [o for o in app.gateway.query_orders() if o["code"] == ETF][0]
    assert order["price"] == 5.6 and order["qty"] == 400
    # 一次性取价已回填 monitor 缓存 (后续监控/卖出链有价可用)
    assert app.monitor.quote_of(ETF)["last"] == 5.6


def test_buy_without_price_no_quote_still_fail_closed(client):
    """一次性取价也取不到 → 维持 fail-closed 拒单 (行为不变)。"""
    c, app = client
    GHOST = "512000.SH"
    r = c.post("/api/trade/buy", json={"code": GHOST, "qty": 400})
    assert r.status_code == 200
    assert _wait(lambda: app.store._conn.execute(
        "SELECT COUNT(*) FROM audit WHERE kind='buy_fail_closed'")
        .fetchone()[0] > 0)
    assert not [o for o in app.gateway.query_orders()
                if o["code"] == GHOST]


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
