# -*- coding: utf-8 -*-
"""tests/trade/test_mobile_contract.py — 手机版契约快照 (2026-09-19 架构修订批次 2.3)。

背景: mobile.html 与 PC 前端**独立维护、不自动跟随**(2026-09-04 `_month` 幽灵行
事故 —— 后端加字段 PC 自动有、手机静默漂移)。本测试是双前端漂移的唯一机器防护:
把 mobile.html 实际消费的 9 个交易端点的**响应形状**(字段名+类型, 递归)锁进
快照文件 tests/web/mobile_contract_snapshot.json; 后端改字段 → 测试红 → 提醒
同步 mobile.html。

快照更新流程 (有意为之的"麻烦"): 改接口的人设环境变量重跑
  set VERA_UPDATE_MOBILE_SNAPSHOT=1 && python -m pytest tests/trade/test_mobile_contract.py
然后**亲自看一眼 diff**, 确认手机版要不要跟着改 —— 人工过目是最后一道闸。
(曾用 pytest 自定义选项 --update-snapshot, 但 pytest_addoption 在非 conftest
的测试模块里不被收集, 实测 unrecognized → 改用环境变量, 少一层 pytest 管道。)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient

from trade.api import create_api_app
from trade.config import TradeConfig
from trade_main import TradeApp

SNAPSHOT = (Path(__file__).resolve().parents[1]
            / "web" / "mobile_contract_snapshot.json")

#: mobile.html 实际消费的交易端点 (2026-09-19 grep 实证: 5 读 + kill/unkill
#: + 分析 2 读)。移动端新增消费端点时, 在这里加一行并更新快照。
MOBILE_ENDPOINTS = [
    ("GET", "/api/trade/status"),
    ("GET", "/api/trade/asset"),
    ("GET", "/api/trade/positions"),
    ("GET", "/api/trade/deals?limit=15"),
    ("GET", "/api/trade/rotation/last"),
    ("GET", "/api/trade/analysis/summary"),
    ("GET", "/api/trade/analysis/daily_pnl"),
    ("POST", "/api/trade/kill"),
    ("POST", "/api/trade/unkill"),
]


def _shape(v):
    """递归提取 JSON 形状: dict → {k: shape}, list → [首元素形状] (+len 备注),
    标量 → 类型名。None 记 "null" (字段可空时形状检查靠人工看 diff)。"""
    if isinstance(v, dict):
        return {k: _shape(x) for k, x in sorted(v.items(), key=lambda kv: kv[0])}
    if isinstance(v, list):
        return [_shape(v[0])] if v else []
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "num"
    return "str"


def _collect(client: TestClient) -> dict:
    out = {}
    for method, path in MOBILE_ENDPOINTS:
        r = (client.get(path) if method == "GET"
             else client.post(path, json={}))
        assert r.status_code == 200, f"{method} {path} → {r.status_code}: {r.text[:200]}"
        out[f"{method} {path}"] = _shape(r.json())
    return out


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("trade.monitor.trading_session",
                        lambda now=None: "continuous")
    cfg = TradeConfig(
        account_id="MOBILE", fake_sdk=True,
        db_path=str(tmp_path / "t.db"),
        raw_log_path=str(tmp_path / "r.jsonl"),
        kill_flag_path=str(tmp_path / "KILL"))
    app = TradeApp(cfg, fake=True, config_path=str(tmp_path / "trade.yaml"))
    app.start(start_timers=False)
    yield TestClient(create_api_app(app), raise_server_exceptions=False)
    app.stop()


def test_mobile_contract_snapshot(client):
    current = _collect(client)
    if os.environ.get("VERA_UPDATE_MOBILE_SNAPSHOT") == "1":
        SNAPSHOT.write_text(json.dumps(current, ensure_ascii=False, indent=2,
                                       sort_keys=True), encoding="utf-8")
        pytest.skip("快照已更新, 请人工核对 diff 后提交")
    if not SNAPSHOT.exists():
        raise AssertionError(
            f"快照不存在: {SNAPSHOT} —— 先设 VERA_UPDATE_MOBILE_SNAPSHOT=1 "
            "重跑生成并人工核对")
    want = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    if current != want:
        # 打印逐端点差异, 让人一眼看到是哪个端点的哪个字段变了
        for key in sorted(set(current) | set(want)):
            if current.get(key) != want.get(key):
                print(f"\n[契约漂移] {key}")
                print(f"  快照: {json.dumps(want.get(key), ensure_ascii=False)[:400]}")
                print(f"  现在: {json.dumps(current.get(key), ensure_ascii=False)[:400]}")
        raise AssertionError(
            "手机版契约漂移 —— 后端响应形状变了。若是有意改动: ①同步 mobile.html "
            "②设 VERA_UPDATE_MOBILE_SNAPSHOT=1 重跑更新快照 ③提交前人工核对 diff")
