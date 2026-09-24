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
        "trailing: 最高 12.00 (峰值涨幅 +20.0%, 过激活线 5.0%), "
        "现价 11.30 回撤 5.8% 触发 (阈值 1.0%)") == (
        "移动止盈: 最高 12.00 (峰值涨幅 +20.0%, 过激活线 5.0%), "
        "现价 11.30 回撤 5.8% 触发 (阈值 1.0%)")
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
    # 2026-08-07: daily_report_level 校验 + 往返
    assert trade_config_from_dict({}).feishu.daily_report_level == "full"  # 缺省
    cfg_lvl = trade_config_from_dict(
        {"feishu": {"daily_report_level": "summary"}})
    assert cfg_lvl.feishu.daily_report_level == "summary"
    assert (trade_config_to_dict(cfg_lvl)["feishu"]["daily_report_level"]
            == "summary")
    with pytest.raises(ValueError):
        trade_config_from_dict(
            {"feishu": {"daily_report_level": "verbose"}})  # 非法档


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


# ═══════════════════════════════════════════════════════════════
# 2026-08-07 盘后日报全明细增强
# (卡片多 section + 仓位变动时序 CRITICAL + 落库 + 交易摘要)
# ═══════════════════════════════════════════════════════════════

def _daily_full_payload():
    return {
        "total_asset": 1_010_000.0, "cash": 1_000_000.0, "market_value": 10_000.0,
        "day_pnl": 500.0, "day_pnl_pct": 0.05, "position_count": 1,
        "floating_pnl": 200.0, "ts": 1750865391.0,
        "buy_count": 1, "sell_count": 1, "turnover": 20_000.0,
        "realized_pnl": 300.0, "win_rate": 1.0,
        "position_changes": {"new": [{"code": "300750.SZ", "delta": 1000}]},
        "sell_details": [{"code": "600519.SH", "pnl_amount": 300.0,
                          "pnl_pct": 5.0, "reason": "移动止盈", "ts": 1.0}],
        # 2026-08-07 交易明细双列 (买卖混排, notifier 优先此字段; 按 ts 升序)
        "trade_details": [
            {"code": "300750.SZ", "direction": 23, "price": 10.0, "qty": 1000,
             "amount": 10000.0, "pnl_amount": None, "pnl_pct": None,
             "reason": "", "ts": 1.0},
            {"code": "600519.SH", "direction": 24, "price": 10.5, "qty": 1000,
             "amount": 10500.0, "pnl_amount": 300.0, "pnl_pct": 5.0,
             "reason": "移动止盈", "ts": 2.0},
        ],
    }


def test_daily_card_full_has_trade_blocks(monkeypatch):
    """full 档: 资产/交易摘要/仓位变动 + 交易明细每条一块 (买卖混排, 简称, 盈亏着色)。
    2026-08-08: 双列 column_set → 单列每条一块 (信息更全: 股数/金额/原因/剩余/比例)。"""
    n = FeishuNotifier(lambda: True, lambda: "http://x")
    names = {"600519.SH": "贵州茅台", "300750.SZ": "宁德时代"}
    monkeypatch.setattr(n, "_name_of", lambda c: names.get(c, ""))
    card = n._build_daily_card(_daily_full_payload(), level="full")
    elems = card["card"]["elements"]
    body_all = "\n".join(e.get("text", {}).get("content", "")
                         for e in elems if e.get("tag") == "div")
    # 仓位变动段仍在
    assert "仓位变动" in body_all and "新进" in body_all and "300750.SZ" in body_all
    # 交易明细标题在, 不再用 column_set
    assert "交易明细" in body_all
    assert not any(e.get("tag") == "column_set" for e in elems)
    # 买入块: 简称 + 代码 + 买入 + 股数×单价=金额
    assert "宁德时代" in body_all and "300750.SZ" in body_all and "买入" in body_all
    assert "1000股 × 10.00 = **10,000.00**" in body_all
    # 卖出块: 简称 + 卖出 + 盈亏(着色+比例) + 原因
    assert "贵州茅台" in body_all and "600519.SH" in body_all and "卖出" in body_all
    assert "+300.00" in body_all and "+5.00%" in body_all and "green" in body_all
    assert "原因 移动止盈" in body_all


def test_daily_card_trade_block_has_remaining_and_ratio(monkeypatch):
    """2026-08-08: 交易明细每条一块含 卖出比例/剩余股数/剩余市值 (用户口径)。"""
    n = FeishuNotifier(lambda: True, lambda: "http://x")
    monkeypatch.setattr(n, "_name_of", lambda c: "示例股")
    card = n._build_daily_card({
        "total_asset": 1e6, "ts": 1750865391.0,
        "trade_details": [
            {"code": "000001.SZ", "direction": 24, "price": 10.0, "qty": 300,
             "amount": 3000.0, "pnl_amount": 248.0, "pnl_pct": 2.5,
             "reason": "移动止盈", "ts": 1.0,
             "sell_ratio": 0.3, "remaining_vol": 700, "remaining_value": 7000.0},
        ],
    })
    body = "\n".join(e.get("text", {}).get("content", "")
                     for e in card["card"]["elements"] if e.get("tag") == "div")
    assert "300股 × 10.00 = **3,000.00**" in body
    assert "+248.00" in body and "green" in body
    assert "原因 移动止盈" in body
    assert "卖出比例 30%" in body               # sell_ratio 0.3 → 30%
    assert "剩余 700股" in body and "市值 7,000.00" in body


def test_daily_card_partial_fallback_only_asset():
    """缺字段 fail-soft: 只有 total_asset → 只出资产 section, 不抛。"""
    n = FeishuNotifier(lambda: True, lambda: "http://x")
    card = n._build_daily_card({"total_asset": 100_000.0, "ts": 1750865391.0})
    divs = [e for e in card["card"]["elements"] if e.get("tag") == "div"]
    assert len(divs) == 1
    assert "100,000.00" in divs[0]["text"]["content"]


def test_daily_card_summary_level_omits_detail_sections():
    """summary 档: 只资产+交易摘要, 不出仓位变动/交易明细 (即使有数据)。"""
    n = FeishuNotifier(lambda: True, lambda: "http://x")
    card = n._build_daily_card(_daily_full_payload(), level="summary")
    elems = card["card"]["elements"]
    divs = [e for e in elems if e.get("tag") == "div"]
    assert len(divs) == 2
    bodies = "\n".join(d["text"]["content"] for d in divs)
    assert "仓位变动" not in bodies and "交易明细" not in bodies
    assert not any(e.get("tag") == "column_set" for e in elems)   # 双列也不出


def test_daily_card_sell_details_fold():
    """>8 笔卖出: 留前 8 + 折叠汇总行 (另 N 笔合计 X)。"""
    n = FeishuNotifier(lambda: True, lambda: "http://x")
    sells = [{"code": f"00000{i}.SZ", "pnl_amount": float(i),
              "pnl_pct": float(i), "reason": "", "ts": float(i)}
             for i in range(10)]
    card = n._build_daily_card({
        "total_asset": 1e6, "ts": 1750865391.0, "sell_details": sells,
        "sell_details_folded": {"count": 2, "sum_pnl_amount": 9.0}})
    body = [e for e in card["card"]["elements"] if e.get("tag") == "div"][-1]["text"]["content"]
    assert "另 2 笔合计 +9.00" in body


def test_daily_card_trade_details_fold(monkeypatch):
    """>12 笔交易明细 (新双列路径): 留前 12 + 折叠汇总 (另 N 笔 · 卖出合计 X)。"""
    n = FeishuNotifier(lambda: True, lambda: "http://x")
    monkeypatch.setattr(n, "_name_of", lambda c: "")
    details = [{"code": f"00000{i}.SZ", "direction": 24, "price": 10.0,
                "qty": 100, "amount": 1000.0, "pnl_amount": float(i),
                "pnl_pct": float(i), "reason": "", "ts": float(i)}
               for i in range(14)]
    card = n._build_daily_card({
        "total_asset": 1e6, "ts": 1750865391.0, "trade_details": details,
        "trade_details_folded": {"count": 2, "sum_sell_pnl": 27.0}})
    body = "\n".join(e.get("text", {}).get("content", "")
                     for e in card["card"]["elements"] if e.get("tag") == "div")
    assert "另 2 笔" in body and "卖出合计" in body and "+27.00" in body


def test_build_trade_summary_trade_details_cap():
    """>12 笔 trade_details: 前 12 + trade_details_folded (count + sum_sell_pnl)。
    2026-08-19 深模块治理: 改直调 trade.daily_report 纯函数 (原经 TradeApp)。"""
    from trade.daily_report import build_trade_summary
    td = [{"code": f"C{i}", "direction": 24, "amount": 100.0,
           "price": 10.0, "qty": 10, "pnl_amount": float(i),
           "pnl_pct": float(i), "reason": "", "ts": float(i)}
          for i in range(14)]
    s = build_trade_summary(td, [])
    assert len(s["trade_details"]) == 12
    assert s["trade_details_folded"]["count"] == 2
    assert s["trade_details_folded"]["sum_sell_pnl"] == 25.0  # ts12+13


def test_build_trade_summary_win_rate_and_sort():
    """sell_count=0 → win_rate=None (不除零); 有卖出按 ts 升序 + 胜率。
    2026-08-19 深模块治理: 改直调 trade.daily_report 纯函数 (原经 TradeApp)。"""
    from trade.daily_report import build_trade_summary
    # 无卖出: win_rate=None
    s0 = build_trade_summary([
        {"code": "A", "direction": 23, "amount": 1000.0, "pnl_amount": 0.0}], [])
    assert s0["sell_count"] == 0 and s0["win_rate"] is None
    assert s0["buy_count"] == 1 and s0["turnover"] == 1000.0
    # 两笔卖出 (一盈一亏): 胜率 0.5, realized=+20, ts 升序
    s1 = build_trade_summary([
        {"code": "B", "direction": 24, "amount": 100.0,
         "pnl_amount": 50.0, "pnl_pct": 5.0, "ts": 2.0},
        {"code": "C", "direction": 24, "amount": 100.0,
         "pnl_amount": -30.0, "pnl_pct": -3.0, "ts": 1.0}], [])
    assert s1["sell_count"] == 2 and s1["win_rate"] == 0.5
    assert s1["realized_pnl"] == 20.0
    assert s1["sell_details"][0]["code"] == "C"   # ts=1 在前
    # 2026-08-07 trade_details (买卖混排, 按 ts 升序, 买 pnl=None)
    assert len(s0["trade_details"]) == 1
    assert s0["trade_details"][0]["direction"] == 23
    assert s0["trade_details"][0]["pnl_amount"] is None
    assert len(s1["trade_details"]) == 2
    assert s1["trade_details"][0]["code"] == "C"   # 混排也按 ts 升序


def test_build_trade_summary_with_remaining():
    """2026-08-08: 重放全历史 Book 算每笔剩余/卖出比例
    (卖出比例=本次卖出÷累计买入, 用户口径)。
    2026-08-19 深模块治理: 改直调 trade.daily_report 纯函数 (原经 TradeApp)。"""
    from trade.daily_report import build_trade_summary
    # 全历史: 买 1000 股 → 卖 300 (剩 700, 卖出比例 30%)
    s = build_trade_summary([
        {"traded_id": "T2", "code": "X", "direction": 24, "price": 11.0,
         "qty": 300, "amount": 3300.0, "pnl_amount": 300.0, "pnl_pct": 10.0,
         "reason": "移动止盈", "ts": 2.0},
    ], [
        {"traded_id": "T1", "order_id": "O1", "code": "X", "direction": 23,
         "price": 10.0, "qty": 1000, "ts": 1.0},
        {"traded_id": "T2", "order_id": "O2", "code": "X", "direction": 24,
         "price": 11.0, "qty": 300, "ts": 2.0},
    ])
    d = s["trade_details"][0]
    assert d["remaining_vol"] == 700
    assert d["remaining_value"] == 7700.0      # 700 × 11.0 (成交价近似现价)
    assert d["sell_ratio"] == 0.3              # 300 / 1000


def test_compute_remaining_map_robustness():
    """2026-08-08: compute_remaining_map 对异常输入 fail-soft (锁 _notify_daily 兜底路径,
    回应安全审计 NOTE: 缺异常输入测试)。"""
    from trade.book import compute_remaining_map
    # 空 trades → 空 out
    assert compute_remaining_map([], {"T1"}) == {}
    # target_tids 含不存在 tid → 该 tid 不在 out, 命中的正常算
    out = compute_remaining_map([
        {"traded_id": "T1", "order_id": "O1", "code": "X", "direction": 23,
         "price": 10.0, "qty": 100, "ts": 1.0},
    ], {"T1", "T_MISSING"})
    assert "T1" in out and "T_MISSING" not in out
    assert out["T1"]["remaining_vol"] == 100
    # 卖出但无历史买入 (cum_buy=0) → sell_ratio=None 不除零, 剩余 0
    out2 = compute_remaining_map([
        {"traded_id": "T2", "order_id": "O2", "code": "Y", "direction": 24,
         "price": 5.0, "qty": 50, "ts": 1.0},
    ], {"T2"})
    assert out2["T2"]["sell_ratio"] is None       # cb=0 → None, 不除零
    assert out2["T2"]["remaining_vol"] == 0       # 卖空仓 → max(0, 0-50)=0


def test_e2e_eod_position_changes_uses_prev_snapshot(cfg, clock, captured, monkeypatch):
    """CRITICAL 回归 (2026-08-07): _on_eod 必须在覆盖 position_snapshot 前读昨仓,
    否则仓位变动恒空。手存昨仓={SH}, 今仓(FakeGateway)={CYB} → 新进 CYB + 清仓 SH。
    若 _on_eod 错成"先覆盖再读", prev=今仓, diff 全空, 此测试失败。"""
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "http://hook")
    app = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                   fake_gateway_kwargs={"cash": 1_000_000.0,
                                        "positions": _positions_seed()})
    try:
        assert app.start(start_timers=False)
        # 手动存"昨仓": 只有 SH, 没有 CYB (FakeGateway 今仓是 CYB)
        app.store.save_position_snapshot(
            {"600519.SH": {"volume": 500, "can_use": 500, "avg_cost": 10.0}})
        app._engine.put(Event(type=EVENT_EOD, data={}))
        assert _wait(lambda: any(
            "日报" in m["card"]["header"]["title"]["content"] for m in captured))
        daily = [m for m in captured
                 if "日报" in m["card"]["header"]["title"]["content"]][-1]
        body = "\n".join(e.get("text", {}).get("content", "")
                         for e in daily["card"]["elements"] if e.get("tag") == "div")
        assert "仓位变动" in body
        assert "新进" in body and CYB in body          # CYB 昨无今有
        assert "清仓" in body and "600519.SH" in body  # SH 昨有今无
    finally:
        app.stop()


def test_e2e_eod_daily_saves_report_for_web(cfg, clock, captured, monkeypatch):
    """盘后日报 payload 同时落 daily_report 表 (web /analysis/daily_report 同源)。"""
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "http://hook")
    app = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                   fake_gateway_kwargs={"cash": 1_000_000.0,
                                        "positions": _positions_seed()})
    try:
        assert app.start(start_timers=False)
        app._engine.put(Event(type=EVENT_EOD, data={}))
        date_str = time.strftime("%Y-%m-%d", time.localtime(clock[0]))
        assert _wait(lambda: app.store.daily_report.load(date_str) is not None)
        rep = app.store.daily_report.load(date_str)
        assert rep["total_asset"] == 1_010_000.0
        assert "buy_count" in rep and "sell_count" in rep  # 交易摘要已拼
    finally:
        app.stop()


def test_e2e_eod_day_pnl_uses_prev_daily_asset(cfg, clock, captured, monkeypatch):
    """2026-08-13 回归: 当日盈亏基准=昨日日终资产 (daily_asset 表),
    不是进程启动快照 _day_baseline。线上 bug 现场: 盘中重启后
    _day_baseline=当前资产 → 差值≈0, 日报当日盈亏恒 +0.00 (08-11/12/13 三天全错)。
    昨日日终 1,090,409.44 → 今日 1,084,551.39 必须报 -5,858.05 (-0.54%)。"""
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "http://hook")
    app = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                   fake_gateway_kwargs={"cash": 1_000_000.0, "positions": {}})
    try:
        assert app.start(start_timers=False)
        from datetime import datetime, timedelta
        yday = (datetime.fromtimestamp(clock[0]) - timedelta(days=1)
                ).strftime("%Y-%m-%d")
        app.store.daily_asset.save(yday, 1_090_409.44, 0.0, 0.0)
        # 复现 bug: 模拟盘中重启后基准被刷成当前资产
        app._day_baseline = 1_084_551.39
        monkeypatch.setattr(app.gateway, "query_asset", lambda: {
            "total_asset": 1_084_551.39, "cash": 512_574.69,
            "market_value": 571_876.70})
        app._engine.put(Event(type=EVENT_EOD, data={}))
        assert _wait(lambda: any(
            "日报" in m["card"]["header"]["title"]["content"] for m in captured))
        daily = [m for m in captured
                 if "日报" in m["card"]["header"]["title"]["content"]][-1]
        body = "\n".join(e.get("text", {}).get("content", "")
                         for e in daily["card"]["elements"] if e.get("tag") == "div")
        assert "-5,858.05" in body and "-0.54%" in body
        # 落库 payload 同源同值 (web 回看一致)
        date_str = time.strftime("%Y-%m-%d", time.localtime(clock[0]))
        rep = app.store.daily_report.load(date_str)
        assert rep["day_pnl"] == -5_858.05 and rep["day_pnl_pct"] == -0.54
    finally:
        app.stop()


def test_e2e_eod_day_pnl_fallback_to_startup_baseline(cfg, clock, captured,
                                                      monkeypatch):
    """首日运行 (daily_asset 无历史行) → 回落启动快照 _day_baseline;
    两者都无 (盘前查询失败) → None, 卡片显示 "—" 不崩。"""
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "http://hook")
    app = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                   fake_gateway_kwargs={"cash": 1_000_000.0, "positions": {}})
    try:
        assert app.start(start_timers=False)
        app._day_baseline = 1_000_000.0        # 无历史行 → 用启动快照
        monkeypatch.setattr(app.gateway, "query_asset", lambda: {
            "total_asset": 995_000.0, "cash": 995_000.0, "market_value": 0.0})
        app._engine.put(Event(type=EVENT_EOD, data={}))
        date_str = time.strftime("%Y-%m-%d", time.localtime(clock[0]))
        assert _wait(lambda: app.store.daily_report.load(date_str) is not None)
        rep = app.store.daily_report.load(date_str)
        assert rep["day_pnl"] == -5_000.0 and rep["day_pnl_pct"] == -0.5

        app._day_baseline = None               # 两者都无 → None
        app.store._conn.execute("DELETE FROM daily_report WHERE date=?",
                                (date_str,))
        app.store._conn.commit()
        app._notify_daily()
        rep2 = app.store.daily_report.load(date_str)
        assert rep2["day_pnl"] is None and rep2["day_pnl_pct"] is None
    finally:
        app.stop()
