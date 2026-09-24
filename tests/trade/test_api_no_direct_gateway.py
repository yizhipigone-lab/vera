# -*- coding: utf-8 -*-
"""tests/trade/test_api_no_direct_gateway.py — HTTP 线程零接触 QMT
(2026-09-19 架构修订批次 4.1)。

背景(架构审查 P0-2): trade/api.py 与 trade/analysis_api.py 的端点在 HTTP 线程
直调 `gateway.query_asset()` (xtquant 同步接口), 与消费者线程并发打柜台 ——
官方死锁坑的擦边。现在统一走 TradeApp.read_via_consumer (投递 EVENT_READ_QUERY
+ 等 Future), xtquant 调用回到唯一消费者线程。

本测试直接证明"查询发生在消费者线程", 而不是只看接口还返回 200。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient

from trade.api import create_api_app
from trade.config import TradeConfig
from trade_main import TradeApp


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.setattr("trade.monitor.trading_session",
                        lambda now=None: "continuous")
    cfg = TradeConfig(
        account_id="T", fake_sdk=True,
        db_path=str(tmp_path / "t.db"),
        raw_log_path=str(tmp_path / "r.jsonl"),
        kill_flag_path=str(tmp_path / "KILL"))
    app = TradeApp(cfg, fake=True, config_path=str(tmp_path / "trade.yaml"))
    app.start(start_timers=False)
    yield app, TestClient(create_api_app(app), raise_server_exceptions=False)
    app.stop()


def test_asset_endpoint_queries_on_consumer_thread(app_client):
    app, client = app_client
    seen: dict = {}
    orig = app.gateway.query_asset

    def _spy():
        seen["thread"] = threading.current_thread().name
        return orig()
    app.gateway.query_asset = _spy

    r = client.get("/api/trade/asset")
    assert r.status_code == 200, r.text
    assert seen.get("thread") == "trade-event-consumer", (
        f"资产查询必须发生在消费者线程, 实际: {seen.get('thread')} "
        "(HTTP 线程直调 xtquant 是 P0-2 要修的问题)")


def test_read_asset_times_out_with_clear_error(app_client):
    """消费者被占住 (查询卡住) → TimeoutError, 由 API 层转 503 人话。"""
    app, client = app_client

    def _slow():
        time.sleep(1.5)
        return {"cash": 0.0}
    app.gateway.query_asset = _slow
    with pytest.raises(TimeoutError):
        app.read_asset(timeout=0.2)

    # 端点侧: 同一慢查询 → 503 且文案是"忙", 不是栈信息
    r = client.get("/api/trade/asset")
    assert r.status_code == 503
    assert "超时" in r.json()["detail"]


def test_read_via_consumer_propagates_query_error(app_client):
    """查询抛异常 → 原样回到调用方 (不许吞掉变成 None)。"""
    app, _client = app_client

    def _boom():
        raise RuntimeError("柜台查询炸了")
    app.gateway.query_asset = _boom
    with pytest.raises(RuntimeError, match="柜台查询炸了"):
        app.read_asset(timeout=2.0)


# ---------------------------------------------------------------------------
# 2026-09-20 审计 P2-1: timeout 曾经只是 Future 的等待时间 —— 入队走
# EVENT_READ_QUERY 的"关键事件"5s 超时, 于是 HTTP 线程最坏卡 5+2 = 7s,
# 且超时后队列里那次查询照样执行 (结果已无人要)。下面两条分别锁这两点。
# ---------------------------------------------------------------------------

def test_read_via_consumer_put_budget_is_bounded(app_client, monkeypatch):
    """入队必须吃同一个 timeout 预算 (而不是走关键事件 5s), 失败即立刻返回。"""
    app, _client = app_client
    seen: dict = {}

    def _full_put(event, timeout=None):
        seen["timeout"] = timeout
        return False              # 队列满 / 丢弃
    monkeypatch.setattr(app._engine, "put", _full_put)

    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match="入队"):
        app.read_via_consumer(lambda: 1, timeout=0.25)
    elapsed = time.monotonic() - t0

    assert seen["timeout"] is not None, \
        "put 必须带显式预算 —— 否则 EVENT_READ_QUERY 走关键事件 5s 超时"
    assert 0 <= seen["timeout"] <= 0.25 + 1e-6
    assert elapsed < 0.2, f"入队失败必须立刻失败, 实际等了 {elapsed:.2f}s"


def test_read_via_consumer_cancels_and_skips_fn_after_timeout(app_client,
                                                              monkeypatch):
    """超时后 cancel future, 消费者线程据此**不再执行**那次查询。"""
    app, _client = app_client
    captured: dict = {}

    def _capture_put(event, timeout=None):
        captured["event"] = event
        captured["timeout"] = timeout
        return True
    monkeypatch.setattr(app._engine, "put", _capture_put)

    def _never():
        raise AssertionError("等待方已放弃, 这次查询不该被执行")
    with pytest.raises(TimeoutError):
        app.read_via_consumer(_never, timeout=0.05)

    fut = captured["event"].data["future"]
    assert fut.cancelled(), "超时后必须 cancel —— 否则消费者线程不知道没人等了"

    called: list = []
    app._on_read_query({"fn": lambda: called.append(1), "future": fut})
    assert called == [], "future 已取消, _on_read_query 必须跳过 fn() (不打柜台)"

