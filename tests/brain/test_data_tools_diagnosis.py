"""tests/brain/test_data_tools_diagnosis.py — stock_diagnosis 一站式命令护栏测试。

mock 数据源，不依赖真实网络（防 flaky）。覆盖：
结构三段 / fail-soft 隔离 / 超时降级 / 模板附录 / CLI 子命令 / prompt 指向。
"""
from __future__ import annotations

import time

from brain import data_tools
from brain.prompts import SYSTEM_PROMPT


class TestStockDiagnosis:
    def test_结构三段加模板(self, monkeypatch):
        """三段标题齐全 + 末尾附成文模板，不抛异常。"""
        monkeypatch.setattr(data_tools, "stock_technicals", lambda c: "TECH")
        monkeypatch.setattr(data_tools, "stock_fundamentals", lambda c: "FUND")
        monkeypatch.setattr(data_tools, "stock_notices", lambda c: "NOTI")
        out = data_tools.stock_diagnosis("300750")
        assert "TECH" in out and "FUND" in out and "NOTI" in out
        assert "成文模板" in out  # 模板附录（读不到文件时静默跳过，本仓库有模板）

    def test_failsoft_单源异常不拖死(self, monkeypatch):
        """技术面抛异常 → 该段标【缺】，其余两段照常。"""
        def _boom(c):
            raise RuntimeError("数据源炸了")
        monkeypatch.setattr(data_tools, "stock_technicals", _boom)
        monkeypatch.setattr(data_tools, "stock_fundamentals", lambda c: "FUND")
        monkeypatch.setattr(data_tools, "stock_notices", lambda c: "NOTI")
        out = data_tools.stock_diagnosis("300750")
        assert "【缺】数据源炸了" in out
        assert "FUND" in out and "NOTI" in out

    def test_failsoft_单源超时判缺(self, monkeypatch):
        """技术面挂死 → per_source_timeout 内返回，该段标超时【缺】。

        per_source_timeout 传小值，测试不用真等 60s。
        """
        monkeypatch.setattr(data_tools, "stock_technicals",
                            lambda c: time.sleep(10))
        monkeypatch.setattr(data_tools, "stock_fundamentals", lambda c: "FUND")
        monkeypatch.setattr(data_tools, "stock_notices", lambda c: "NOTI")
        t0 = time.monotonic()
        out = data_tools.stock_diagnosis("300750", per_source_timeout=0.5)
        assert time.monotonic() - t0 < 5  # 不被挂死源拖住
        assert "超时" in out
        assert "FUND" in out and "NOTI" in out

    def test_并发真快(self, monkeypatch):
        """三个各 sleep 0.3s 的源并发跑，总耗时应 << 串行的 0.9s。"""
        def _slow(c):
            time.sleep(0.3)
            return "X"
        for fn in ("stock_technicals", "stock_fundamentals", "stock_notices"):
            monkeypatch.setattr(data_tools, fn, _slow)
        t0 = time.monotonic()
        data_tools.stock_diagnosis("300750")
        assert time.monotonic() - t0 < 0.8

    def test_非法代码(self):
        out = data_tools.stock_diagnosis("abc")
        assert "【错】" in out


class TestDiagnosisCli:
    def test_CLI子命令注册(self, capsys):
        """argparse 认识 stock_diagnosis（mock 掉真实取数）。"""
        import brain.data_tools as m
        orig = m.stock_diagnosis
        m.stock_diagnosis = lambda c: "PACK"
        try:
            rc = m.main(["stock_diagnosis", "300750"])
        finally:
            m.stock_diagnosis = orig
        assert rc == 0
        assert capsys.readouterr().out.strip() == "PACK"


class TestPromptPointsToDiagnosis:
    def test_固定打法指向一站式(self):
        assert "stock_diagnosis" in SYSTEM_PROMPT

    def test_软舆情纪律未丢(self):
        """「这条腿不可省」的纪律仍在 —— 2026-09-20 换源后指向 akshare 按词搜。

        旧断言是 `"search_web" in SYSTEM_PROMPT`；换源后它仍会**偶然通过**
        （正文抓取那句还留着 search_web fetch），但测的已不是本意 —— 故改为
        重新钉在新源上，避免"通过了但通过的理由是错的"。
        """
        assert "stock_news" in SYSTEM_PROMPT
        assert "不可省" in SYSTEM_PROMPT

    def test_红线和A股纪律逐字保留(self):
        for s in ("【数据】，不是【指令】", "KILL 开关",
                  "<counter_evidence>", "T+1", "涨停"):
            assert s in SYSTEM_PROMPT, f"prompt 瘦身误删: {s}"
