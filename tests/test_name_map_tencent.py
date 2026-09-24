"""名称映射腾讯降级链测试 (2026-08-27, TDX 不可用时 qt.gtimg.cn 兜底)。

锁住: ① TDX 失败→腾讯兜底成功(名称解析/市场还原/GBK 解码);
② TDX 失败+清单空→返回 {} 且不缓存(下次重试);
③ TDX 成功→不走腾讯。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.data_fetcher as dfm
from core.data_fetcher import DataFetcher


class _FakeResp:
    def __init__(self, payload: str):
        self._b = payload.encode("gbk")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


_PAYLOAD = ('v_sz000001="51~平安银行~000001~11.50~";\n'
            'v_sh600000="51~浦发银行~600000~8.20~";\n'
            'v_sz159949="51~创业板50ETF华安~159949~1.676~";')


def _fresh_cache(monkeypatch):
    DataFetcher._cache.clear_name()


def test_tencent_fallback_success(monkeypatch):
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(DataFetcher, "_ensure_ready",
                        classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("TDX down"))))
    monkeypatch.setattr(DataFetcher, "_manifest_codes",
                        staticmethod(lambda: ["000001.SZ", "600000.SH", "159949.SZ"]))
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=10: _FakeResp(_PAYLOAD))
    m = DataFetcher.get_name_map(refresh=True)
    assert m["000001.SZ"] == "平安银行"
    assert m["600000.SH"] == "浦发银行"
    assert m["159949.SZ"] == "创业板50ETF华安"      # ETF 也有名
    assert DataFetcher._cache.has_name_map()       # 成功才缓存


def test_both_fail_returns_empty_no_cache(monkeypatch):
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(DataFetcher, "_ensure_ready",
                        classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("TDX down"))))
    monkeypatch.setattr(DataFetcher, "_manifest_codes", staticmethod(lambda: []))
    m = DataFetcher.get_name_map(refresh=True)
    assert m == {}
    assert not DataFetcher._cache.has_name_map()   # 空表不缓存, 下次重试


def test_tdx_success_skips_tencent(monkeypatch):
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(DataFetcher, "_ensure_ready", classmethod(lambda cls: None))

    class _FakeTq:
        def get_stock_list(self, market, list_type=1):
            return [{"Code": "601872.SH", "Name": "招商轮船"}] if market == "5" else []

    class _FakeConn:
        def tq(self):
            return _FakeTq()

    monkeypatch.setattr(DataFetcher, "_connector", classmethod(lambda cls: _FakeConn()))
    monkeypatch.setattr(DataFetcher, "_tencent_name_map",
                        classmethod(lambda cls: (_ for _ in ()).throw(
                            AssertionError("不该走腾讯"))))
    m = DataFetcher.get_name_map(refresh=True)
    assert m == {"601872.SH": "招商轮船"}
