# -*- coding: utf-8 -*-
"""brain/quick_chat.py 行为测试 — 全离线 (monkeypatch LLM client + archive)。

覆盖: 成功契约 / 失败兜底 / 空答兜底 / temp0.7 防空 content / channel 归档 /
不传 channel 不归档 / fastpath writer=quick 走 quick_compose。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import brain.quick_chat as qc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class _FakeClient:
    """假 LLM client: 记录 temperature, 返固定文本。"""

    def __init__(self, text="秒回答案", fail=False):
        self._text, self._fail = text, fail
        self.calls = []

    def chat(self, messages, temperature=0.0, **kw):
        self.calls.append({"temperature": temperature, "messages": messages})
        if self._fail:
            raise RuntimeError("网络炸了")
        return self._text


def _patch_client(monkeypatch, client):
    # _chat_once 内延迟 import llm.providers.get_client, patch 其模块路径即可
    import llm.providers as lp
    monkeypatch.setattr(lp, "get_client", lambda: client)


def _patch_archive(monkeypatch):
    """patch brain.archive.archive_exchange (真实 _archive 的落地处)。"""
    import brain.archive as arc
    cap = {}
    monkeypatch.setattr(arc, "archive_exchange",
                        lambda ch, q, r: cap.update({"ch": ch, "q": q,
                                                     "ans": r.get("answer")}))
    return cap


def test_quick_answer_success_contract(monkeypatch):
    c = _FakeClient(text="答: 移动平均线是…")
    _patch_client(monkeypatch, c)
    r = run(qc.quick_answer("什么是均线?"))
    assert r["success"] is True and "移动平均线" in r["answer"]
    assert c.calls and c.calls[0]["temperature"] == 0.7  # 防空 content 的坑


def test_quick_answer_no_history_uses_quick_system(monkeypatch):
    c = _FakeClient()
    _patch_client(monkeypatch, c)
    run(qc.quick_answer("q", history=None))
    msgs = c.calls[0]["messages"]
    assert msgs[0]["role"] == "system" and "快速问答" in msgs[0]["content"]
    assert msgs[-1] == {"role": "user", "content": "q"}


def test_quick_answer_packs_history_only_user_assistant(monkeypatch):
    c = _FakeClient()
    _patch_client(monkeypatch, c)
    hist = [{"role": "user", "content": "上问"},
            {"role": "tool", "content": "不该打包"},
            {"role": "assistant", "content": "上答"}]
    run(qc.quick_answer("q", history=hist))
    roles = [m["role"] for m in c.calls[0]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert "不该打包" not in str(c.calls[0]["messages"])


def test_quick_answer_exception_soft_fail(monkeypatch):
    _patch_client(monkeypatch, _FakeClient(fail=True))
    r = run(qc.quick_answer("q"))
    assert r["success"] is False and "秒回通道" in r["answer"]


def test_quick_answer_empty_text_soft_fail(monkeypatch):
    _patch_client(monkeypatch, _FakeClient(text=None))
    r = run(qc.quick_answer("q"))
    assert r["success"] is False and "无响应" in r["answer"]
    assert r["low_confidence"] is True


def test_quick_answer_archives_when_channel(monkeypatch):
    cap = _patch_archive(monkeypatch)
    _patch_client(monkeypatch, _FakeClient(text="沉淀答案"))
    run(qc.quick_answer("问题X", channel="research_tab_c1"))
    assert cap == {"ch": "research_tab_c1", "q": "问题X", "ans": "沉淀答案"}


def test_quick_answer_no_channel_no_archive(monkeypatch):
    cap = _patch_archive(monkeypatch)
    _patch_client(monkeypatch, _FakeClient())
    run(qc.quick_answer("问题Y"))
    assert cap == {}          # 脚本/测试场景不污染 vault
