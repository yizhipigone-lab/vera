# -*- coding: utf-8 -*-
"""tests/trade/test_rotation_feed.py — 轮动取数降级链 (2026-09-19 批次 4.5)。

自 rotation.py 端出后的独立测试: 三级降级顺序、判空阈值、来源名 (含 QMT 陈旧
后缀)、全挂 fail-closed。用假 gateway + monkeypatch 两个备源。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.rotation_feed import IndexFeed

CODE = "399673.SZ"


class _Gw:
    def __init__(self, closes=None, stale=None, boom=False):
        self._closes = closes
        self.history_stale = stale or {}
        self._boom = boom

    def query_daily_closes(self, code, count=0):
        if self._boom:
            raise RuntimeError("QMT 挂了")
        return self._closes


def test_qmt_primary_returns_source_name():
    feed = IndexFeed(_Gw(closes=[1.0] * 30))
    closes, src = feed.closes(CODE, count=30, min_bars=21)
    assert len(closes) == 30 and src == "QMT"


def test_qmt_stale_marks_source_name():
    """2026-09-16 审计修复①: QMT 日线陈旧时来源名带后缀 (页面看得见)。"""
    feed = IndexFeed(_Gw(closes=[1.0] * 30, stale={CODE: "2026-09-14"}))
    _closes, src = feed.closes(CODE, count=30, min_bars=21)
    assert src == "QMT(陈旧)"


def test_falls_back_to_tdx_when_qmt_short(monkeypatch):
    monkeypatch.setattr(IndexFeed, "_tdx_closes",
                        staticmethod(lambda code, count: [2.0] * count))
    feed = IndexFeed(_Gw(closes=[1.0] * 5))     # 不足 min_bars
    closes, src = feed.closes(CODE, count=30, min_bars=21)
    assert src == "TDX" and closes[0] == 2.0


def test_falls_back_to_tencent_when_qmt_and_tdx_fail(monkeypatch):
    monkeypatch.setattr(IndexFeed, "_tdx_closes",
                        staticmethod(lambda code, count: (_ for _ in ()).throw(
                            RuntimeError("TDX 挂了"))))
    monkeypatch.setattr(IndexFeed, "_tencent_closes",
                        staticmethod(lambda code, count: [3.0] * count))
    feed = IndexFeed(_Gw(boom=True))
    closes, src = feed.closes(CODE, count=30, min_bars=21)
    assert src == "腾讯" and closes[0] == 3.0


def test_all_sources_fail_is_fail_closed(monkeypatch):
    """全挂 → ([], "none"), 绝不返回"半截数据"让上层误判。"""
    monkeypatch.setattr(IndexFeed, "_tdx_closes",
                        staticmethod(lambda code, count: []))
    monkeypatch.setattr(IndexFeed, "_tencent_closes",
                        staticmethod(lambda code, count: []))
    feed = IndexFeed(_Gw(boom=True))
    closes, src = feed.closes(CODE, count=30, min_bars=21)
    assert closes == [] and src == "none"


def test_min_bars_threshold_enforced(monkeypatch):
    """恰好多一根也不行: 根数 < min_bars 必须降级 (阈值判据不许放宽)。"""
    monkeypatch.setattr(IndexFeed, "_tdx_closes",
                        staticmethod(lambda code, count: None))
    monkeypatch.setattr(IndexFeed, "_tencent_closes",
                        staticmethod(lambda code, count: None))
    feed = IndexFeed(_Gw(closes=[1.0] * 20))
    closes, src = feed.closes(CODE, count=20, min_bars=21)
    assert closes == [] and src == "none"
