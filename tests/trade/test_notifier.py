"""FeishuNotifier 单元 + TradeApp 端到端测试 (2026-07-31).

锁住:
- 卡片 JSON 合法 (互动卡片, 买入绿/卖出橙/盈亏红绿);
- _label_from_reason 把 monitor reason 串翻成中文策略名;
- worker 投递: 生产侧只入队零阻塞; POST 失败/超时 worker 不死;
- 无 URL / enabled=False → no-op, 一条不发;
- 队列满 → 丢弃不抛;
- TradeApp→FakeGateway 端到端: 阶梯成交→卖出卡(含档位/剩余),
  EOD→盘后日报; 无 URL 时不发且不影响交易。
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.config import LadderTpConfig, StopConfig, TradeConfig
from trade.events import EVENT_EOD, EVENT_SYNC_REPORTS, Event
from trade.executor import _detail_from_reason, _label_from_reason
from trade.notifier import DIRECTION_BUY, FeishuNotifier
from trade_main import TradeApp

CYB = "300750.SZ"      # 创业板, 涨停 20%, 三档全挂
SH = "600519.SH"       # 主板, 买入通知 E2E 用
_E2E_LADDER = ((0.05, 0.33), (0.10, 0.5), (0.15, 1.0))


# ═══════════════════════════════════════════════════════════════
# 假 webhook 捕获 (patch urllib.request.urlopen)
# ═══════════════════════════════════════════════════════════════

class _FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b'{"code":0}'


@pytest.fixture()
def captured(monkeypatch):
    posted = []

    def _fake_urlopen(req, timeout=None):
        posted.append(json.loads(req.data.decode("utf-8")))
        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    return posted


def _wait(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


# ═══════════════════════════════════════════════════════════════
# 卡片构建 (纯函数, 不依赖 worker)
# ═══════════════════════════════════════════════════════════════

def test_fill_card_buy_structure():
    n = FeishuNotifier(lambda: True, lambda: "http://x")
    card = n._build_fill_card({
        "code": "600519.SH", "direction": DIRECTION_BUY, "price": 12.34,
        "qty": 100, "amount": 1234.0, "ts": 1750865391.0, "label": "TDX买入"})
    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["template"] == "green"   # 买入绿
    body = card["card"]["elements"][0]["text"]["content"]
    assert "600519.SH" in body and "买入" in body and "TDX买入" in body
    assert "12.34" in body and "100" in body and "1,234.00" in body


def test_fill_card_sell_with_extras():
    n = FeishuNotifier(lambda: True, lambda: "http://x")
    card = n._build_fill_card({
        "code": CYB, "direction": 24, "price": 11.20, "qty": 300,
        "amount": 3360.0, "ts": 1750865391.0, "label": "阶梯止盈",
        "pnl_pct": 12.0, "tier": 0, "sell_ratio": 0.33,
        "remaining_vol": 700, "remaining_value": 7840.0})
    assert card["card"]["header"]["template"] == "orange"  # 卖出橙
    body = card["card"]["elements"][0]["text"]["content"]
    assert "卖出" in body and "阶梯止盈" in body
    assert "档位1" in body and "33%" in body      # tier0 → 档位1 卖33%
    assert "剩余 700" in body
    assert "+12.00%" in body                      # 盈亏


def test_daily_card_red_when_loss():
    n = FeishuNotifier(lambda: True, lambda: "http://x")
    card = n._build_daily_card({
        "total_asset": 990000.0, "cash": 100000.0,
        "day_pnl": -5000.0, "day_pnl_pct": -0.5,
        "position_count": 3, "ts": 1750865391.0})
    assert card["card"]["header"]["template"] == "red"
    body = card["card"]["elements"][0]["text"]["content"]
    assert "990,000.00" in body
    assert "-5,000.00" in body and "-0.50%" in body
    assert "持仓 3 只" in body


def test_label_from_reason_mapping():
    assert _label_from_reason("trailing: 现价 11.8 跌破峰值 12×(1-1%)") == "移动止盈"
    assert _label_from_reason("cost_stop: 现价 9 ≤ 成本×(1-12%)") == "硬止损"
    assert _label_from_reason("time_stop: 持有 20 天 ≥ 20 天") == "时间止损"
    assert _label_from_reason("ladder_tp: 最高涨破档 0") == "阶梯止盈"
    assert _label_from_reason("cond_time: 持有 5 天") == "条件时间止盈"
    assert _label_from_reason("first_day: 首日最高涨幅未达") == "首日不达标"
    assert _label_from_reason("manual_sell: 人工卖出") == "人工卖出"
    assert _label_from_reason("") == "系统卖出"
    assert _label_from_reason("unknown_rule: xxx") == "系统卖出"


def test_detail_from_reason_full_text():
    """2026-07-31: 成交原因全文 = 中文策略名 + monitor 正文 (带数字)。"""
    assert _detail_from_reason(
        "trailing: 最高 12.00 (峰值涨幅 +20.0%, 过激活线 5%), "
        "现价 11.30 回撤 5.8% 触发 (阈值 1%)") == (
        "移动止盈: 最高 12.00 (峰值涨幅 +20.0%, 过激活线 5%), "
        "现价 11.30 回撤 5.8% 触发 (阈值 1%)")
    assert _detail_from_reason("manual_sell: 人工卖出") == "人工卖出: 人工卖出"
    assert _detail_from_reason("") == "系统卖出"      # 无正文不拼冒号


# ═══════════════════════════════════════════════════════════════
# worker 行为
# ═══════════════════════════════════════════════════════════════

def test_worker_posts_fill(captured):
    n = FeishuNotifier(lambda: True, lambda: "http://hook")
    n.start()
    try:
        n.notify_fill({"code": "600519.SH", "direction": DIRECTION_BUY,
                       "price": 10.0, "qty": 100, "amount": 1000.0,
                       "ts": 1000.0, "label": "人工买入"})
        assert _wait(lambda: len(captured) == 1)
        msg = captured[0]
        assert msg["msg_type"] == "interactive"
        assert "买入" in msg["card"]["header"]["title"]["content"]
    finally:
        n.stop()


def test_worker_post_failure_keeps_worker_alive(monkeypatch):
    def _boom(req, timeout=None):
        raise OSError("network down")
    monkeypatch.setattr("urllib.request.urlopen", _boom)
    n = FeishuNotifier(lambda: True, lambda: "http://hook")
    n.start()
    try:
        for _ in range(3):
            n.notify_fill({"code": "600519.SH", "direction": DIRECTION_BUY,
                           "price": 10.0, "qty": 100, "amount": 1000.0,
                           "ts": 1000.0})
        # 三笔全失败, worker 仍把队列消费空 + 线程存活 (交易不受影响)
        assert _wait(lambda: n._queue.empty())
        assert n._thread is not None and n._thread.is_alive()
    finally:
        n.stop()


def test_no_url_is_noop(captured):
    n = FeishuNotifier(lambda: True, lambda: None)   # 无 URL
    n.start()
    try:
        n.notify_fill({"code": "600519.SH", "direction": DIRECTION_BUY,
                       "price": 10.0, "qty": 100, "amount": 1000.0, "ts": 1.0})
        n.notify_daily({"total_asset": 1e6, "cash": 1e5, "ts": 1.0})
        time.sleep(0.3)
        assert captured == []                          # 一条都不发
    finally:
        n.stop()


def test_disabled_is_noop(captured):
    n = FeishuNotifier(lambda: False, lambda: "http://hook")  # enabled=False
    n.start()
    try:
        n.notify_fill({"code": "600519.SH", "direction": DIRECTION_BUY,
                       "price": 10.0, "qty": 100, "amount": 1000.0, "ts": 1.0})
        time.sleep(0.2)
        assert captured == []
    finally:
        n.stop()


def test_queue_full_drops_silently():
    n = FeishuNotifier(lambda: True, lambda: "http://hook")
    # 不 start worker, 队列填满到上限
    for _ in range(n._queue.maxsize):
        n.notify_fill({"code": "600519.SH", "direction": DIRECTION_BUY,
                       "price": 10.0, "qty": 100, "amount": 1000.0, "ts": 1.0})
    assert n._queue.qsize() == n._queue.maxsize
    # 超限一笔: 丢弃不抛
    n.notify_fill({"code": "600519.SH", "direction": DIRECTION_BUY,
                   "price": 10.0, "qty": 100, "amount": 1000.0, "ts": 1.0})
    assert n._queue.qsize() == n._queue.maxsize   # 仍在上限, 没涨


# ═══════════════════════════════════════════════════════════════
# TradeApp 端到端 (FakeGateway)
# ═══════════════════════════════════════════════════════════════

@pytest.fixture()
def clock():
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


def _positions_seed():
    return {CYB: {"volume": 1000, "can_use": 1000, "avg_cost": 10.0}}


def _today(clock) -> str:
    return time.strftime("%Y%m%d", time.localtime(clock[0]))


def test_e2e_ladder_fill_notifies(cfg, clock, captured, monkeypatch):
    """阶梯预埋单成交 → 飞书卖出卡 (label=阶梯止盈, 档位1, 剩余700)。"""
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "http://hook")
    app = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                   fake_gateway_kwargs={"cash": 1_000_000.0,
                                        "positions": _positions_seed()})
    try:
        assert app.start(start_timers=False)
        gw = app.gateway
        gw.push_quote(CYB, {"last": 10.2, "bid1": 10.2, "high": 10.3,
                            "prev_close": 10.0})
        app.submit_command({"action": "place_ladder", "date_str": _today(clock)})
        assert _wait(lambda: len(gw.query_orders()) >= 3)
        tier0 = sorted([o for o in gw.query_orders() if o["code"] == CYB],
                       key=lambda o: o["price"])[0]
        gw.simulate_fill(tier0["order_id"])
        # 成交 → 飞书卖出卡
        assert _wait(lambda: any(
            "卖出" in m["card"]["header"]["title"]["content"] for m in captured))
        body = [m for m in captured
                if "卖出" in m["card"]["header"]["title"]["content"]][-1]
        text = body["card"]["elements"][0]["text"]["content"]
        assert "阶梯止盈" in text and CYB in text
        assert "档位1" in text and "剩余 700" in text   # tier0 卖 33%, 余 700
    finally:
        app.stop()


def test_e2e_eod_daily_notifies(cfg, clock, captured, monkeypatch):
    """15:05 EOD → 飞书盘后日报 (总资产/市值/现金/当日盈亏)。"""
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "http://hook")
    app = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                   fake_gateway_kwargs={"cash": 1_000_000.0,
                                        "positions": _positions_seed()})
    try:
        assert app.start(start_timers=False)
        app._engine.put(Event(type=EVENT_EOD, data={}))
        assert _wait(lambda: any(
            "日报" in m["card"]["header"]["title"]["content"] for m in captured))
        daily = [m for m in captured
                 if "日报" in m["card"]["header"]["title"]["content"]][-1]
        text = daily["card"]["elements"][0]["text"]["content"]
        assert "总资产" in text and "现金" in text and "当日盈亏" in text
        assert "持仓 1 只" in text
    finally:
        app.stop()


def test_e2e_no_url_no_posts(cfg, clock, captured, monkeypatch):
    """无 FEISHU_WEBHOOK_URL: 成交照常落账, 但飞书一条不发 (fail-soft)。"""
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    app = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                   fake_gateway_kwargs={"cash": 1_000_000.0,
                                        "positions": _positions_seed()})
    try:
        assert app.start(start_timers=False)
        gw = app.gateway
        gw.push_quote(CYB, {"last": 10.2, "bid1": 10.2, "high": 10.3,
                            "prev_close": 10.0})
        app.submit_command({"action": "place_ladder", "date_str": _today(clock)})
        assert _wait(lambda: len(gw.query_orders()) >= 3)
        tier0 = sorted([o for o in gw.query_orders() if o["code"] == CYB],
                       key=lambda o: o["price"])[0]
        gw.simulate_fill(tier0["order_id"])
        # 交易照常: book 递减
        assert _wait(lambda: app.book.snapshot()["positions"][CYB].volume == 700)
        time.sleep(0.3)
        assert captured == []                          # 无 URL, 一条不发
    finally:
        app.stop()


def test_e2e_backfill_adoption_notifies(cfg, clock, captured, monkeypatch):
    """成交回调丢失 → 增量同步补记 → 飞书卖出卡 (label=阶梯止盈, 档位1)。
    覆盖 QMT 回调常丢失、补记是主路径的真实场景 (0731 实证)。"""
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "http://hook")
    app = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                   fake_gateway_kwargs={"cash": 1_000_000.0,
                                        "positions": _positions_seed()})
    try:
        assert app.start(start_timers=False)
        gw = app.gateway
        gw.push_quote(CYB, {"last": 10.2, "bid1": 10.2, "high": 10.3,
                            "prev_close": 10.0})
        app.submit_command({"action": "place_ladder", "date_str": _today(clock)})
        assert _wait(lambda: len(gw.query_orders()) >= 3)
        tier0 = sorted([o for o in gw.query_orders() if o["code"] == CYB],
                       key=lambda o: o["price"])[0]
        # 模拟"成交回调丢失": 塞进 gw._trades (order_id=系统单, 不触发回调)
        gw._trades["FAKET-backfill"] = {
            "traded_id": "FAKET-backfill", "order_id": tier0["order_id"],
            "code": CYB, "direction": 24, "price": 10.5, "qty": 300,
            "amount": 3150.0, "ts": clock[0]}
        # 触发增量同步 → 补记 → 飞书 (系统单 pop 到阶梯止盈 ctx)
        app._engine.put(Event(type=EVENT_SYNC_REPORTS, data={"hhmm": "10:00"}))
        assert _wait(lambda: any(
            "卖出" in m["card"]["header"]["title"]["content"] for m in captured))
        body = [m for m in captured
                if "卖出" in m["card"]["header"]["title"]["content"]][-1]
        text = body["card"]["elements"][0]["text"]["content"]
        assert "阶梯止盈" in text and CYB in text and "档位1" in text
        assert "剩余 700" in text     # 补记后 book 持仓 1000-300=700
    finally:
        app.stop()


def test_e2e_full_close_sell_has_pnl(clock, captured, monkeypatch, tmp_path):
    """H1 (审计修复): 清仓卖出 volume→0 时 book 把 avg_cost 清零,
    通知仍显示盈亏% —— _on_trade 在 apply_trade 前快照成本。"""
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "http://hook")
    cfg = TradeConfig(
        account_id="E2E", fake_sdk=True,
        db_path=str(tmp_path / "db"), raw_log_path=str(tmp_path / "raw"),
        kill_flag_path=str(tmp_path / "KILL"),
        stop=StopConfig(ladder_tp=LadderTpConfig(levels=((0.05, 1.0),))),
    )
    app = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                   fake_gateway_kwargs={"cash": 1_000_000.0,
                                        "positions": {CYB: {"volume": 1000,
                                                            "can_use": 1000,
                                                            "avg_cost": 10.0}}})
    try:
        assert app.start(start_timers=False)
        gw = app.gateway
        gw.push_quote(CYB, {"last": 10.2, "bid1": 10.2, "high": 10.3,
                            "prev_close": 10.0})
        app.submit_command({"action": "place_ladder", "date_str": _today(clock)})
        assert _wait(lambda: len(gw.query_orders()) >= 1)
        tier0 = sorted(gw.query_orders(), key=lambda o: o["price"])[0]
        gw.simulate_fill(tier0["order_id"])            # 清仓档卖光 1000
        assert _wait(lambda: app.book.snapshot()["positions"][CYB].volume == 0)
        assert _wait(lambda: any(
            "卖出" in m["card"]["header"]["title"]["content"] for m in captured))
        body = [m for m in captured
                if "卖出" in m["card"]["header"]["title"]["content"]][-1]
        text = body["card"]["elements"][0]["text"]["content"]
        assert "+5.00%" in text, text                  # 10.5/10-1, 清仓盈亏% 在
        assert "剩余 0" in text
    finally:
        app.stop()


def test_e2e_buy_fill_notifies(cfg, clock, captured, monkeypatch):
    """M-测1: 买入实时通知端到端 (label=人工买入, 绿头)。"""
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "http://hook")
    app = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                   fake_gateway_kwargs={"cash": 1_000_000.0, "positions": {}})
    try:
        assert app.start(start_timers=False)
        gw = app.gateway
        gw.push_quote(SH, {"last": 10.0, "bid1": 10.0, "ask1": 10.0,
                           "high": 10.0, "prev_close": 10.0})
        app.submit_command({"action": "manual_buy", "code": SH,
                            "qty": 200, "price": 10.0})
        assert _wait(lambda: any(o["direction"] == 23 for o in gw.query_orders()))
        buy_oid = [o for o in gw.query_orders() if o["direction"] == 23][0]["order_id"]
        gw.simulate_fill(buy_oid)
        assert _wait(lambda: any(
            "买入" in m["card"]["header"]["title"]["content"] for m in captured))
        body = [m for m in captured
                if "买入" in m["card"]["header"]["title"]["content"]][-1]
        text = body["card"]["elements"][0]["text"]["content"]
        assert "人工买入" in text and SH in text
        assert body["card"]["header"]["template"] == "green"
    finally:
        app.stop()


def test_e2e_manual_adopt_notifies(cfg, clock, captured, monkeypatch):
    """M-测2: 手工单 (order_id 本地订单簿查不到) 补记也发飞书, 无 ctx 兜底。"""
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "http://hook")
    app = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                   fake_gateway_kwargs={"cash": 1_000_000.0,
                                        "positions": _positions_seed()})
    try:
        assert app.start(start_timers=False)
        gw = app.gateway
        gw._trades["FAKET-manual"] = {
            "traded_id": "FAKET-manual", "order_id": "EXT9999",
            "code": CYB, "direction": 24, "price": 10.5, "qty": 100,
            "amount": 1050.0, "ts": clock[0]}
        app._engine.put(Event(type=EVENT_SYNC_REPORTS, data={"hhmm": "10:00"}))
        assert _wait(lambda: any(
            "卖出" in m["card"]["header"]["title"]["content"] for m in captured))
        body = [m for m in captured
                if "卖出" in m["card"]["header"]["title"]["content"]][-1]
        text = body["card"]["elements"][0]["text"]["content"]
        assert CYB in text
        assert "档位" not in text          # 手工单无 ctx, 不带档位
    finally:
        app.stop()


def test_feishu_config_validation_and_roundtrip():
    """M-测3: FeishuConfig 校验 (未知字段/类型) + 往返 (锁配置契约)。"""
    from trade.config import trade_config_from_dict, trade_config_to_dict
    assert trade_config_from_dict({}).feishu.enabled is True        # 缺省
    assert trade_config_from_dict(
        {"feishu": {"enabled": False}}).feishu.enabled is False
    with pytest.raises(ValueError):
        trade_config_from_dict({"feishu": {"bogus": 1}})            # 未知字段
    with pytest.raises(TypeError):
        trade_config_from_dict({"feishu": {"enabled": "yes"}})      # 非 bool
    cfg = trade_config_from_dict({"feishu": {"enabled": False}})
    assert trade_config_to_dict(cfg)["feishu"]["enabled"] is False  # 往返


def test_e2e_eod_daily_has_numbers(cfg, clock, captured, monkeypatch):
    """M-测4: 盘后日报断言具体数值 (总资产/盈亏%), 不只字段存在。"""
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "http://hook")
    app = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                   fake_gateway_kwargs={"cash": 1_000_000.0,
                                        "positions": _positions_seed()})
    try:
        assert app.start(start_timers=False)
        app._engine.put(Event(type=EVENT_EOD, data={}))
        assert _wait(lambda: any(
            "日报" in m["card"]["header"]["title"]["content"] for m in captured))
        daily = [m for m in captured
                 if "日报" in m["card"]["header"]["title"]["content"]][-1]
        text = daily["card"]["elements"][0]["text"]["content"]
        assert "1,010,000.00" in text   # FakeGateway: 1000×10 + 1e6 cash
        assert "(+0.00%)" in text       # baseline=EOD total, 无交易 → 0
    finally:
        app.stop()


def test_enabled_toggle_is_hot(captured):
    """M-测5: enabled_getter 每次读, 运行中切换立即生效 (热关承诺)。"""
    holder = {"on": True}
    n = FeishuNotifier(lambda: holder["on"], lambda: "http://hook")
    n.start()
    try:
        n.notify_fill({"code": SH, "direction": DIRECTION_BUY, "price": 10.0,
                       "qty": 100, "amount": 1000.0, "ts": 1.0})
        assert _wait(lambda: len(captured) == 1)
        holder["on"] = False               # 运行中热关
        n.notify_fill({"code": SH, "direction": DIRECTION_BUY, "price": 10.0,
                       "qty": 100, "amount": 1000.0, "ts": 1.0})
        time.sleep(0.3)
        assert len(captured) == 1           # 第二条被 no-op 拦下
    finally:
        n.stop()
