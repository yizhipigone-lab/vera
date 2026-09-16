"""trade/api.py 通道端点测试 (2026-09-07 T4, 方案设计书 §5.5)。

锁: status 扩字段、channel GET、probe/arm/disarm 只对 ths 通道开放
(其余 409)、arm 打字确认错串 422、switch 盘中 403 / 休市 accepted。
装配与 test_api.py 相同 (TestClient + FakeGateway, channel=qmt)。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from trade.api import create_api_app  # noqa: E402
from trade.config import TradeConfig  # noqa: E402
from trade_main import TradeApp  # noqa: E402

SH = "600519.SH"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("trade.monitor.trading_session",
                        lambda now=None: "continuous")
    cfg = TradeConfig(
        account_id="CH", fake_sdk=True,
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


def test_status_has_channel_fields(client):
    c, _ = client
    d = c.get("/api/trade/status").json()
    assert d["channel"] == "qmt"
    assert d["armed_intent"] is None        # 非 ths 通道无 mgr
    assert d["armed_effective"] is None
    assert d["channel_down"] is False
    assert "last_probe" in d


def test_channel_get(client):
    c, _ = client
    d = c.get("/api/trade/channel").json()
    assert d["channel"] == "qmt"
    assert set(d["available"]) == {"qmt", "ths", "fake"}
    assert d["mgr"] is None


def test_probe_on_non_ths_409(client):
    c, _ = client
    r = c.post("/api/trade/channel/probe")
    assert r.status_code == 409


def test_arm_bad_confirm_422(client):
    c, _ = client
    r = c.post("/api/trade/channel/arm", json={"confirm": "手滑"})
    assert r.status_code == 422


def test_arm_on_non_ths_409(client):
    c, _ = client
    r = c.post("/api/trade/channel/arm", json={"confirm": "确认实盘"})
    assert r.status_code == 409


def test_disarm_on_non_ths_409(client):
    c, _ = client
    r = c.post("/api/trade/channel/disarm")
    assert r.status_code == 409


def test_switch_rejected_in_continuous(client):
    c, _ = client
    r = c.post("/api/trade/channel/switch", json={"channel": "ths"})
    assert r.status_code == 403


def test_switch_ok_when_closed(client, monkeypatch):
    c, app = client
    monkeypatch.setattr("trade.monitor.trading_session",
                        lambda now=None: "closed")
    r = c.post("/api/trade/channel/switch", json={"channel": "ths"})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["restart_required"] is True
    assert "channel" in body["changed"]


def test_switch_bad_channel_422(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr("trade.monitor.trading_session",
                        lambda now=None: "closed")
    r = c.post("/api/trade/channel/switch", json={"channel": "evil"})
    assert r.status_code == 422
