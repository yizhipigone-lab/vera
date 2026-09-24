"""名称表惰性加载重试测试 (2026-08-27 页面简称全丢事件防回归)。

锁住: get_name_map 失败/返空时**不缓存空表**, 下次调用重试;
拿到非空表后才缓存 (进程级), 之后不再重拉。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import trade.analysis as an
from core.data_fetcher import DataFetcher
from trade.notifier import FeishuNotifier


def _flaky(calls):
    def f(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("TDX down")
        if calls["n"] == 2:
            return {}                      # 第二次: TDX 起了但列表空
        return {"000001.SZ": "平安银行"}
    return f


def test_analysis_name_of_retries(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(DataFetcher, "get_name_map", _flaky(calls))
    monkeypatch.setattr(an, "_name_map", None)
    assert an.name_of("000001.SZ") == ""          # 异常: 空串不缓存
    assert an.name_of("000001.SZ") == ""          # 空表: 空串不缓存
    assert an.name_of("000001.SZ") == "平安银行"   # 第三次重试成功
    assert an.name_of("000001.SZ") == "平安银行"
    assert calls["n"] == 3                        # 成功后不再重拉


def test_notifier_name_of_retries(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(DataFetcher, "get_name_map", _flaky(calls))
    n = FeishuNotifier.__new__(FeishuNotifier)    # 绕过 __init__ (无需 webhook)
    n._name_map = None
    assert n._name_of("000001.SZ") == ""
    assert n._name_of("000001.SZ") == ""
    assert n._name_of("000001.SZ") == "平安银行"
    assert n._name_of("000001.SZ") == "平安银行"
    assert calls["n"] == 3
