"""tests/test_feishu_webhook.py — utils/feishu_webhook.py 单元测试 + 隔离断言。

覆盖：读业务 code 判真送达（code=0 / 拒收 code!=0 / HTTP 异常 / 非法 JSON 均 fail-soft
不上抛）；隔离铁律 1（共享轮子是中立层，不得 import trade/ 或 research/）。
"""
from __future__ import annotations

import ast
from pathlib import Path

from utils.feishu_webhook import post_webhook


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _patch_urlopen(monkeypatch, body: bytes):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _FakeResp(body))


class TestPostWebhook:
    def test_送达成功(self, monkeypatch):
        """code=0 → True（读业务码，不是只看 HTTP 200）。"""
        _patch_urlopen(monkeypatch, b'{"code":0,"msg":"success"}')
        assert post_webhook("http://x", {"msg_type": "text"}) is True

    def test_业务码非零拒收(self, monkeypatch):
        """code=11246 → False（2026-08-07 假成功坑：HTTP 200 但卡片被拒）。"""
        _patch_urlopen(monkeypatch, b'{"code":11246,"msg":"invalid card"}')
        assert post_webhook("http://x", {"msg_type": "text"}) is False

    def test_http异常返False不抛(self, monkeypatch):
        def _boom(req, timeout=None):
            raise OSError("网络断了")
        monkeypatch.setattr("urllib.request.urlopen", _boom)
        assert post_webhook("http://x", {}) is False

    def test_非法json返False不抛(self, monkeypatch):
        _patch_urlopen(monkeypatch, b"not-json")
        assert post_webhook("http://x", {}) is False

    def test_context只进日志不进卡片(self, monkeypatch):
        """context 参数化的是日志前缀，不得影响 POST 出去的 body。"""
        captured = {}
        def _capture(req, timeout=None):
            captured["data"] = req.data
            return _FakeResp(b'{"code":0}')
        monkeypatch.setattr("urllib.request.urlopen", _capture)
        body = {"msg_type": "text", "content": {"text": "hi"}}
        assert post_webhook("http://x", body, context="交易") is True
        assert "交易" not in captured["data"].decode("utf-8")


class TestIsolation:
    def test_不import_trade_research(self):
        """隔离铁律 1：共享轮子是中立层，AST 断言不 import trade/ 或 research/。"""
        src = (Path(__file__).resolve().parents[1] / "utils"
               / "feishu_webhook.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(("trade", "research")), \
                        f"禁止 import: {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("trade", "research")), \
                    f"禁止 import: {node.module}"
