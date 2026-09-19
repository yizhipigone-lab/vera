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
