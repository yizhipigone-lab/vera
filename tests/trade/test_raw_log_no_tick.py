# -*- coding: utf-8 -*-
"""tests/trade/test_raw_log_no_tick.py — raw 审计日志不记行情 tick (2026-09-20 item 1)。

铁律 5 的底账是"原始回报先落盘"——它的价值在审计/复盘, 而 tick 把它淹到了
99.99% (2026-09-20 实测 raw_reports_202608.jsonl: 3,740,113 行里 tick
3,739,631 行, 真实回报仅 482 行)。tick 仍必须进引擎 (monitor 心跳/止损判定
全靠它), 只是不落 JSONL。本文件锁两件事: tick 不落盘 + tick 仍到引擎。
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

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
def clock():
    from datetime import datetime, timedelta
    d = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return [d.timestamp()]


@pytest.fixture()
def cfg(tmp_path):
    return TradeConfig(
        account_id="T", fake_sdk=True,
        db_path=str(tmp_path / "t.db"),
        raw_log_path=str(tmp_path / "raw.jsonl"),
        kill_flag_path=str(tmp_path / "KILL"),
    )


@pytest.fixture()
def app(cfg, clock):
    a = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                 fake_gateway_kwargs={
                     "cash": 1_000_000.0,
                     "positions": {CYB: {"volume": 1000, "can_use": 1000,
                                         "avg_cost": 10.0}}})
    yield a
    a.stop()


def test_tick不落raw但到达引擎(app, cfg, clock):
    """tick 不写 raw JSONL (底账只留真实回报), 但仍驱动 monitor (引擎路径不动)。"""
    assert app.start(start_timers=False)
    gw = app.gateway

    gw.push_quote(CYB, {"last": 10.2, "bid1": 10.2, "high": 10.3,
                        "prev_close": 10.0})
    # tick 到达引擎的证明: monitor 拿到该代码的快照
    assert _wait(lambda: app.monitor.quote_of(CYB) is not None)

    # 产生一条真实回报作对照 (预埋 → 成交)
    today = time.strftime("%Y%m%d", time.localtime(clock[0]))
    app.submit_command({"action": "place_ladder", "date_str": today})
    assert _wait(lambda: len(gw.query_orders()) >= 1)
    gw.simulate_fill(gw.query_orders()[0]["order_id"])

    assert app.store.flush_raw()
    raw_lines = Path(cfg.raw_log_path).read_text(encoding="utf-8").splitlines()
    kinds = [json.loads(x)["kind"] for x in raw_lines]
    assert "tick" not in kinds, f"raw 底账不应记 tick: {kinds}"
    assert "trade_fill" in kinds, f"真实回报仍须落盘: {kinds}"
