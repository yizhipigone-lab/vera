# -*- coding: utf-8 -*-
"""tests/trade/test_tick_age_field.py — 行情新鲜度字段 (2026-09-20 item 4b)。

为什么: 看门狗只凭 monitor_healthy 会瞎 —— 它的判定跑在被监控的消费者线程
里, 线程卡死时冻结在 True (假死不可检测)。暴露 last_tick_age_s 后,
外部(看门狗)才能看见"行情是不是真的在流"。

本文件锁: TradeApp.last_tick_age_s 的语义 (无 tick → None / 有 tick → 秒数)
+ /api/trade/status 必须带该字段 (手机契约快照同步锁形状)。
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

CYB = "300750.SZ"


def _wait(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture()
def app(tmp_path):
    # 真实时钟: last_tick_age_s 语义是"距现在的秒数", 注入假时钟会与
    # tick 事件自带的真实 ts 错位
    cfg = TradeConfig(
        account_id="T", fake_sdk=True,
        db_path=str(tmp_path / "t.db"),
        raw_log_path=str(tmp_path / "raw.jsonl"),
        kill_flag_path=str(tmp_path / "KILL"),
    )
    a = TradeApp(cfg, fake=True,
                 fake_gateway_kwargs={
                     "cash": 1_000_000.0,
                     "positions": {CYB: {"volume": 1000, "can_use": 1000,
                                         "avg_cost": 10.0}}})
    yield a
    a.stop()


def test_未收到tick时为None(app):
    assert app.last_tick_age_s is None


def test_收到tick后为秒数(app):
    assert app.start(start_timers=False)
    gw = app.gateway
    gw.push_quote(CYB, {"last": 10.2, "bid1": 10.2, "high": 10.3,
                        "prev_close": 10.0})
    assert _wait(lambda: app.monitor.quote_of(CYB) is not None)
    age = app.last_tick_age_s
    assert age is not None and 0 <= age < 60, f"age 应是近期秒数, 实际 {age}"


def test_status接口带该字段(app):
    client = TestClient(create_api_app(app))
    r = client.get("/api/trade/status")
    assert r.status_code == 200
    assert "last_tick_age_s" in r.json(), (
        f"status 缺 last_tick_age_s: {sorted(r.json())}")
