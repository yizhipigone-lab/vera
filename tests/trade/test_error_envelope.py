# -*- coding: utf-8 -*-
"""tests/trade/test_error_envelope.py — 错误响应契约 (2026-09-19 架构修订批次 2.2)。

全站两种许可形状, 不许发明第三种 (server.py 全局 handler 注释即契约文本):
  传输/意外错误 → 非 200 + {"detail": "人话"}
  业务软失败    → 200 + {"success": false, "error": ...}
本文件锁死第一条: 未捕获异常必须落 JSON {"detail": ...} (两台服务同口径),
不许回到 Starlette 纯文本 500 (前端 r.json() 会炸成 SyntaxError)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient

from trade.api import create_api_app
from trade.config import TradeConfig
from trade_main import TradeApp


@pytest.fixture()
def trade_client(tmp_path, monkeypatch):
    monkeypatch.setattr("trade.monitor.trading_session",
                        lambda now=None: "continuous")
    cfg = TradeConfig(
        account_id="API", fake_sdk=True,
        db_path=str(tmp_path / "t.db"),
        raw_log_path=str(tmp_path / "r.jsonl"),
        kill_flag_path=str(tmp_path / "KILL"))
    app = TradeApp(cfg, fake=True, config_path=str(tmp_path / "trade.yaml"))
    app.start(start_timers=False)
    # raise_server_exceptions=False: Starlette 的 ServerErrorMiddleware 是
    # "发完 500 响应还会再 raise 一次"(给服务器日志用), 默认 True 会让
    # TestClient 把已 handled 的异常再抛出来, 测不到响应形状
    yield TestClient(create_api_app(app), raise_server_exceptions=False)
    app.stop()


def test_trade_api_unhandled_error_is_json_detail(trade_client):
    """8081: 未捕获异常 → 500 + {"detail": ...}, 不是纯文本。"""
    app = trade_client.app
    @app.get("/_test_boom")
    def _boom():
        raise RuntimeError("炸了")
    r = trade_client.get("/_test_boom")
    assert r.status_code == 500
    body = r.json()                      # 是纯文本时这里直接抛 —— 即回归点
    assert "炸了" in body["detail"]


def test_trade_api_httpexception_shape_unchanged(trade_client):
    """HTTPException 维持 FastAPI 原生 {"detail": ...} 形状 (前端按它解析)。"""
    r = trade_client.get("/api/trade/deals?date=abc")   # 非法日期 → 422
    assert r.status_code == 422
    assert "detail" in r.json()


def test_server_unhandled_error_is_json_detail():
    """8080: 同一个契约 (server.py 全局 handler)。"""
    from server import app
    @app.get("/_test_boom_envelope")
    def _boom():
        raise RuntimeError("炸了")
    r = TestClient(app, raise_server_exceptions=False).get("/_test_boom_envelope")
    assert r.status_code == 500
    assert "炸了" in r.json()["detail"]
