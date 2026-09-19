# -*- coding: utf-8 -*-
"""tests/trade/test_backfill_second_writer_guard.py — 回填 CLI 的第二写者守卫
(2026-09-19 架构修订批次 4.3)。

背景(架构审查 P0-5): tools/backfill_daily_decision.py 直写 trade.db 的
daily_decision 表, 与运行中的 trade_main (唯一写者) 并发, 原来只靠 WAL busy
重试兜底。现在交易进程活着就拒绝 (除非 --force)。
"""
from __future__ import annotations

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools import backfill_daily_decision as bd


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({"connected": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 静音
        pass


@pytest.fixture()
def live_api():
    """起一个假交易 API (返回 200 JSON), 返回 base URL。"""
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()   # shutdown 只停循环, 不关监听 socket (不关会被后续探活误判)


@pytest.fixture()
def dead_base():
    """确定性的"没人监听"地址: 只 bind 不 listen → connect 必被拒。

    (不用 bind 后 close 的写法: 端口会被系统立刻复用, 与刚关闭的测试服务器
    撞车, 曾实测导致探活误报 True。)
    """
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        s.close()


def test_alive_detects_running_api(live_api):
    assert bd._trade_api_alive(live_api, timeout=2.0) is True


def test_alive_false_when_nothing_listening(dead_base):
    assert bd._trade_api_alive(dead_base, timeout=1.0) is False


def test_cli_refuses_when_trade_process_alive(monkeypatch, tmp_path, live_api):
    """守卫在最前面: 即使 db 路径无效也先拒 (不碰库)。"""
    rc = bd.main(["--db", str(tmp_path / "nope.db"), "--api-base", live_api])
    assert rc == 3


def test_cli_force_bypasses_guard(monkeypatch, tmp_path, live_api):
    calls = {}

    def _fake_run(db, start, end, **kw):
        calls["db"] = db
        return {"days": 0, "rows": 0, "keys": 0, "written": 0,
                "removed": 0, "per_day": {}}
    monkeypatch.setattr(bd, "run", _fake_run)
    db = tmp_path / "trade.db"
    db.write_bytes(b"")
    rc = bd.main(["--db", str(db), "--api-base", live_api, "--force"])
    assert rc == 0 and calls["db"] == str(db)


def test_cli_dry_run_not_blocked(monkeypatch, tmp_path, live_api):
    """dry-run 只读不写, 即使交易进程活着也放行。"""
    monkeypatch.setattr(bd, "run", lambda db, s, e, **kw: {
        "days": 0, "rows": 0, "keys": 0, "written": 0, "removed": 0,
        "per_day": {}})
    db = tmp_path / "trade.db"
    db.write_bytes(b"")
    rc = bd.main(["--db", str(db), "--api-base", live_api, "--dry-run"])
    assert rc == 0
