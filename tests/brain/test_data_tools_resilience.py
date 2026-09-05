# -*- coding: utf-8 -*-
"""_fetch_resilient 单测 (治理补丁 2026-09-05: 9-02/03 cninfo 域 11001 事件).

覆盖: DNS 预检一次成功 / 前两次 gaierror 后自愈 / 全败抛 RuntimeError
(含 host) 并落一行 _collect_errors.log。
"""
import socket

import pytest

from brain import data_tools as dt


@pytest.fixture(autouse=True)
def _tmp_log(tmp_path, monkeypatch):
    # _fetch_resilient 落盘路径 = project_root()/data/brain_model_cache;
    # patch 模块级 project_root, 把日志引到 tmp_path, 不碰真实 data/。
    monkeypatch.setattr(dt, "project_root", lambda: tmp_path)
    return tmp_path


def test_dns_ok_once_call_ok(_tmp_log, monkeypatch):
    calls = {"dns": 0, "fn": 0}

    def fake_dns(h):
        calls["dns"] += 1
        return "1.2.3.4"

    def fake_fn():
        calls["fn"] += 1
        return {"ok": True}

    monkeypatch.setattr(socket, "gethostbyname", fake_dns)
    out = dt._fetch_resilient(fake_fn, ("www.cninfo.com.cn",), 5, "测试源")
    assert out == {"ok": True}
    assert calls["dns"] == 1 and calls["fn"] == 1


def test_dns_transient_then_recovers(_tmp_log, monkeypatch):
    calls = {"dns": 0}

    def flaky(h):
        calls["dns"] += 1
        if calls["dns"] < 3:
            raise socket.gaierror(11001, "getaddrinfo failed")
        return "1.2.3.4"

    monkeypatch.setattr(socket, "gethostbyname", flaky)
    out = dt._fetch_resilient(lambda: 42, ("irm.cninfo.com.cn",), 5, "测试源")
    assert out == 42
    assert calls["dns"] == 3          # 前两次失败重试, 第三次成功


def test_all_fail_raises_with_host_and_logs(_tmp_log, monkeypatch):
    monkeypatch.setattr(
        socket, "gethostbyname",
        lambda h: (_ for _ in ()).throw(socket.gaierror(11001, "getaddrinfo failed")))
    with pytest.raises(RuntimeError) as ei:
        dt._fetch_resilient(lambda: None, ("www.cninfo.com.cn",), 5,
                            "公告源（巨潮 www.cninfo.com.cn）")
    msg = str(ei.value)
    assert "www.cninfo.com.cn" in msg and "getaddrinfo" in msg
    log = (_tmp_log / "data" / "brain_model_cache"
           / "_collect_errors.log").read_text(encoding="utf-8")
    assert "公告源" in log and "www.cninfo.com.cn" in log


def test_call_failure_retried_then_ok(_tmp_log, monkeypatch):
    calls = {"fn": 0}
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "1.2.3.4")

    def flaky():
        calls["fn"] += 1
        if calls["fn"] < 2:
            raise TimeoutError("超时")
        return "data"

    assert dt._fetch_resilient(flaky, ("zhibo.sina.com.cn",), 5, "快讯源") == "data"
    assert calls["fn"] == 2
