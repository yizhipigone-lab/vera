# -*- coding: utf-8 -*-
"""tests/test_tdx_tq.py — core/tdx_tq.py 薄封装测试 (2026-08-28)。

分层:
- 纯归一化函数 (norm_*): 用真实探针抓回的样本断言, 零 TDX 依赖
- TQ 薄封装 (etf_of_index/stock_ext/snapshot/available): monkeypatch
  _tq 假连接, 只测 fail-soft 与归一化串联, 绝不在测试里连真通达信
- 模块导入不触发 tqcenter (懒加载铁律: 测试/server 起动不需要 TDX)

样本来源: research/tdx_skillhub/tq_probe_诊断脚本.py 2026-08-28 实抓。
"""
from __future__ import annotations

import pytest


# ── 真实样本 (探针实抓, 字段为字符串形态) ──────────────────────

ETF_RAW_399673 = [
    {"Code": "159949.SZ", "Name": "创业板50ETF华安", "NowPrice": "1.676",
     "PreClose": "1.689", "IOPV": "1.6746", "Zgb": "129693.79", "Sz": "8.13"},
    {"Code": "159372.SZ", "Name": "创业板50ETF万家", "NowPrice": "1.812",
     "PreClose": "1.834", "IOPV": "1.8102", "Zgb": "6245.15", "Sz": "1.13"},
]

EXT_RAW_600519 = {
    "MainBusiness": "茅台酒", "HqDate": "20260828", "ZAF": "0.39",
    "ZAFYesterday": "-0.81", "ZAFPre2D": "-0.09", "ZAFPre5D": "1.20",
    "ZAFPre10D": "-2.31", "ZAFPre20D": "4.05", "ZAFPre30D": "6.11",
    "ZAFPre60D": "9.87", "Zsz": "16218.56", "Ltsz": "16218.56",
    "ZTPrice": "1421.53", "DTPrice": "1163.07", "fHSL": "0.13",
}

SNAP_RAW_159949 = {
    "LastClose": "1.635", "Open": "1.628", "Max": "1.659", "Min": "1.608",
    "Now": "1.611", "Volume": "9338483", "Amount": "152490.17",
    "Inside": "4777398", "Outside": "4561086", "Jjjz": "1.609",
}


# ── 模块导入纪律 ────────────────────────────────────────────────

class TestLazyImport:
    def test_import_does_not_load_tqcenter(self):
        """导入 core.tdx_tq 不得**新引入** tqcenter (懒加载铁律)。
        注: 全量跑时 core/connector.py 可能已被其他测试触发而先行载入
        tqcenter (进程级 sys.modules 共享), 那不是本模块的责任 —— 只断言
        本模块的 import 不是新引入者。"""
        import sys
        before = "tqcenter" in sys.modules
        import core.tdx_tq  # noqa: F401
        after = "tqcenter" in sys.modules
        assert not (not before and after), (
            "core.tdx_tq 模块级导入把 tqcenter 拉进来了 —— 破坏懒加载铁律")


# ── 纯归一化函数 ────────────────────────────────────────────────

class TestNormEtfList:
    def test_real_sample(self):
        from core.tdx_tq import norm_etf_list
        out = norm_etf_list(ETF_RAW_399673)
        assert len(out) == 2
        e = out[0]
        assert e["code"] == "159949.SZ"
        assert e["name"] == "创业板50ETF华安"
        assert e["price"] == pytest.approx(1.676)
        assert e["prev_close"] == pytest.approx(1.689)
        assert e["iopv"] == pytest.approx(1.6746)
        assert e["scale_yi"] == pytest.approx(8.13)

    def test_none_and_empty(self):
        from core.tdx_tq import norm_etf_list
        assert norm_etf_list(None) == []
        assert norm_etf_list([]) == []

    def test_bad_values_become_none(self):
        from core.tdx_tq import norm_etf_list
        out = norm_etf_list([{"Code": "x", "NowPrice": "abc"}])
        assert out[0]["price"] is None
        assert out[0]["code"] == "x"


class TestNormStockExt:
    def test_real_sample(self):
        from core.tdx_tq import norm_stock_ext
        e = norm_stock_ext(EXT_RAW_600519)
        assert e["main_business"] == "茅台酒"
        assert e["hq_date"] == "20260828"
        assert e["zaf_pct"] == pytest.approx(0.39)
        assert e["zaf_yesterday_pct"] == pytest.approx(-0.81)
        assert e["zaf_pre60d_pct"] == pytest.approx(9.87)
        assert e["zsz_yi"] == pytest.approx(16218.56)
        assert e["zt_price"] == pytest.approx(1421.53)
        assert e["hsl_pct"] == pytest.approx(0.13)

    def test_none_returns_empty_dict(self):
        from core.tdx_tq import norm_stock_ext
        assert norm_stock_ext(None) == {}

    def test_missing_fields_none(self):
        from core.tdx_tq import norm_stock_ext
        e = norm_stock_ext({"ZAF": "1.5"})
        assert e["zaf_pct"] == pytest.approx(1.5)
        assert e["zsz_yi"] is None


class TestNormSnapshot:
    def test_real_sample(self):
        from core.tdx_tq import norm_snapshot
        s = norm_snapshot(SNAP_RAW_159949)
        assert s["now"] == pytest.approx(1.611)
        assert s["high"] == pytest.approx(1.659)
        assert s["low"] == pytest.approx(1.608)
        assert s["prev_close"] == pytest.approx(1.635)
        assert s["jjjz"] == pytest.approx(1.609)
        assert s["volume"] == pytest.approx(9338483)

    def test_none_returns_empty_dict(self):
        from core.tdx_tq import norm_snapshot
        assert norm_snapshot(None) == {}


# ── TQ 薄封装 fail-soft (mock, 不连真通达信) ────────────────────

class _FakeTq:
    """假 tq 对象: 按需抛错或回样本。"""

    def __init__(self, *, etf=None, ext=None, snap=None, raise_on=None):
        self._etf, self._ext, self._snap = etf, ext, snap
        self._raise_on = raise_on or set()

    def get_trackzs_etf_info(self, zs_code=""):
        if "etf" in self._raise_on:
            raise ConnectionError("server return none")
        return self._etf

    def get_more_info(self, stock_code="", field_list=[]):
        if "ext" in self._raise_on:
            raise ConnectionError("boom")
        return self._ext

    def get_market_snapshot(self, stock_code="", field_list=[]):
        if "snap" in self._raise_on:
            raise ConnectionError("boom")
        return self._snap


class TestWrappersFailSoft:
    def test_etf_of_index_normalizes(self, monkeypatch):
        import core.tdx_tq as m
        monkeypatch.setattr(m, "_tq",
                            lambda: _FakeTq(etf=ETF_RAW_399673))
        out = m.etf_of_index("399673.SZ")
        assert len(out) == 2
        assert out[0]["code"] == "159949.SZ"

    def test_etf_of_index_exception_returns_empty(self, monkeypatch):
        import core.tdx_tq as m
        monkeypatch.setattr(m, "_tq",
                            lambda: _FakeTq(raise_on={"etf"}))
        assert m.etf_of_index("000300.CSI") == []

    def test_etf_of_index_dead_connection_returns_empty(self, monkeypatch):
        import core.tdx_tq as m

        def boom():
            raise RuntimeError("TQ 未初始化")

        monkeypatch.setattr(m, "_tq", boom)
        assert m.etf_of_index("399673.SZ") == []

    def test_stock_ext_ok(self, monkeypatch):
        import core.tdx_tq as m
        monkeypatch.setattr(m, "_tq",
                            lambda: _FakeTq(ext=EXT_RAW_600519))
        e = m.stock_ext("600519.SH")
        assert e["zaf_pct"] == pytest.approx(0.39)

    def test_stock_ext_fail_returns_none(self, monkeypatch):
        import core.tdx_tq as m
        monkeypatch.setattr(m, "_tq",
                            lambda: _FakeTq(raise_on={"ext"}))
        assert m.stock_ext("600519.SH") is None

    def test_snapshot_fail_returns_none(self, monkeypatch):
        import core.tdx_tq as m
        monkeypatch.setattr(m, "_tq",
                            lambda: _FakeTq(raise_on={"snap"}))
        assert m.snapshot("159949.SZ") is None

    def test_available_false_when_no_plugins_dir(self, monkeypatch, tmp_path):
        import core.tdx_tq as m
        monkeypatch.setattr(m, "tdx_plugins_user",
                            lambda: str(tmp_path / "nope"))
        assert m.available() is False


class TestPublicSurface:
    def test_public_methods_within_limit(self):
        """铁律 8: 公开接口 ≤8 个。"""
        import core.tdx_tq as m
        fns = [n for n in dir(m) if not n.startswith("_")
               and callable(getattr(m, n))
               and getattr(getattr(m, n), "__module__", "") == m.__name__]
        assert 0 < len(fns) <= 8, f"公开函数 {len(fns)} 个: {fns}"
