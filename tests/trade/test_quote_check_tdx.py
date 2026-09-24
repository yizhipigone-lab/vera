# -*- coding: utf-8 -*-
"""tests/trade/test_quote_check_tdx.py — T7 通达信验枪适配器接缝测试。

接缝策略: monkeypatch core.tdx_tq.snapshot 注入替身, 断言适配器的
形状变换 (snapshot 返回 -> bool), 不依赖通达信客户端在场。
"""
from __future__ import annotations

from core import tdx_tq
from trade.quote_check_tdx import DEFAULT_PROBE_CODE, make_tdx_quote_check


def _patch_snapshot(monkeypatch, value):
    monkeypatch.setattr(tdx_tq, "snapshot", lambda code: value)


def test_alive_with_now_price(monkeypatch):
    _patch_snapshot(monkeypatch, {"now": 3.21, "prev_close": 3.20})
    assert make_tdx_quote_check()() is True


def test_alive_with_prev_close_only(monkeypatch):
    # 盘前/盘后 now 为空, 昨收有效也算"活着"
    _patch_snapshot(monkeypatch, {"now": None, "prev_close": 3.20})
    assert make_tdx_quote_check()() is True


def test_snapshot_none_fails_closed(monkeypatch):
    # tdx_tq fail-soft 返回 None (通达信没开/异常) → 验枪不通过
    _patch_snapshot(monkeypatch, None)
    assert make_tdx_quote_check()() is False


def test_snapshot_empty_fails_closed(monkeypatch):
    _patch_snapshot(monkeypatch, {})
    assert make_tdx_quote_check()() is False


def test_zero_and_missing_prices_fail_closed(monkeypatch):
    _patch_snapshot(monkeypatch, {"now": 0.0, "prev_close": None})
    assert make_tdx_quote_check()() is False


def test_probe_code_passed_through(monkeypatch):
    seen = {}

    def _fake(code):
        seen["code"] = code
        return {"now": 1.0}

    monkeypatch.setattr(tdx_tq, "snapshot", _fake)
    assert make_tdx_quote_check("513100.SH")() is True
    assert seen["code"] == "513100.SH"


def test_blank_code_falls_back_to_default(monkeypatch):
    seen = {}

    def _fake(code):
        seen["code"] = code
        return {"now": 1.0}

    monkeypatch.setattr(tdx_tq, "snapshot", _fake)
    assert make_tdx_quote_check("  ")() is True
    assert seen["code"] == DEFAULT_PROBE_CODE


def test_snapshot_exception_fails_closed(monkeypatch):
    def _boom(code):
        raise RuntimeError("TQ 爆炸")
    monkeypatch.setattr(tdx_tq, "snapshot", _boom)
    # tdx_tq 自身 fail-soft 不抛; 适配器再兜一层, 异常 → False 绝不外抛
    assert make_tdx_quote_check()() is False
