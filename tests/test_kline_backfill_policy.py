# -*- coding: utf-8 -*-
"""K线回填失败治理纯函数测试 (2026-09-05 体检 P1).

覆盖 utils/kline_backfill_policy.py 三件套:
manifest_advanced (延伸是否有进展) / stall_due (停滞冷却) /
breaker_tripped (源站故障熔断), 全本地无网络。
"""
import datetime as dt

from utils import kline_backfill_policy as pol


class TestManifestAdvanced:
    def test_无记录到有记录为有进展(self):
        assert pol.manifest_advanced(None, ("20200101", "20260501", 1.0)) is True

    def test_无记录仍无记录为无进展(self):
        assert pol.manifest_advanced(None, None) is False

    def test_last后移为有进展(self):
        before = ("20200101", "20260901", 1.0)
        after = ("20200101", "20260905", 1.0)
        assert pol.manifest_advanced(before, after) is True

    def test_first前移为有进展(self):
        before = ("20210101", "20260905", 1.0)
        after = ("20200101", "20260905", 1.0)
        assert pol.manifest_advanced(before, after) is True

    def test_原地不动为无进展(self):
        row = ("20200101", "20260905", 1.0)
        assert pol.manifest_advanced(row, row) is False

    def test_有记录被删为无进展(self):
        assert pol.manifest_advanced(("20200101", "20260905", 1.0), None) is False


class TestStallDue:
    def test_无记录到期再试(self):
        assert pol.stall_due(None) is True

    def test_刚停滞在冷却期(self):
        rec = {"ts": dt.datetime.now().isoformat(), "err": "空返回(无进展)"}
        assert pol.stall_due(rec) is False

    def test_超冷却到期再试(self):
        old = (dt.datetime.now() - dt.timedelta(hours=73)).isoformat()
        assert pol.stall_due({"ts": old}) is True

    def test_坏时间按到期处理(self):
        assert pol.stall_due({"ts": "not-a-date"}) is True


class TestBreakerTripped:
    def test_样本不足不熔断(self):
        assert pol.breaker_tripped([True] * 79) is False

    def test_失败率达标熔断(self):
        assert pol.breaker_tripped([True] * 80) is True
        assert pol.breaker_tripped([True] * 60 + [False] * 20) is True  # 75%

    def test_失败率不达标不熔断(self):
        assert pol.breaker_tripped([True] * 47 + [False] * 33) is False  # 59%
