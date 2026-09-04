# -*- coding: utf-8 -*-
"""brain/dsh_channel.py 行为测试 — 全离线 (fake 子进程 + tmp sqlite)。

任何路径不碰真 DSH / 真 DB / 真 taskkill 有效目标。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import brain.dsh_channel as dsh  # noqa: E402


class _FakeProc:
    """假子进程: wait 阻塞直到 kill; communicate 返固定输出。"""

    def __init__(self, out=None, err=b"", rc=0, hang=False):
        self._out = out if out is not None else "答案".encode()
        self._err, self._rc = err, rc
        self._hang = hang
        self.returncode = None
        self.pid = 99999
        self.killed = False

    async def wait(self):
        while self.returncode is None:
            if not self._hang:
                self.returncode = self._rc
            await asyncio.sleep(0.005)
        return self.returncode

    async def communicate(self):
        if self.returncode is None:
            self.returncode = self._rc
        return self._out, self._err

    def kill(self):
        self.killed = True
        self.returncode = self._rc if not self._hang else -9


def _patch_spawn(monkeypatch, proc):
    """假 spawn: 把 fake 子进程的固定输出写进日志文件 (对齐真实现的文件输出)。"""
    async def fake_spawn(task, env, out_f, err_f):
        out_f.write((proc._out or b"").decode("utf-8", "replace"))
        err_f.write((proc._err or b"").decode("utf-8", "replace"))
        out_f.flush()
        err_f.flush()
        return proc
    monkeypatch.setattr(dsh, "_spawn", fake_spawn)


def _patch_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(dsh, "DSH_NODE", tmp_path / "node" / "node.exe")
    (tmp_path / "node").mkdir()
    (tmp_path / "node" / "node.exe").write_text("")
    entry = tmp_path / "app" / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("")
    monkeypatch.setattr(dsh, "DSH_ENTRY", entry)
    monkeypatch.setattr(dsh, "DSH_HOME", tmp_path / "home")
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(dsh, "DSH_WORKSPACE", tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()


def run(coro):
    return asyncio.run(coro)


# ---- scan_leak 纯函数 ----

def test_scan_leak_keyword_and_idcard():
    assert "资金账号" in dsh.scan_leak("我的资金账号是 123")
    assert "证件号模式" in dsh.scan_leak("11010119900307777X")
    assert dsh.scan_leak("今天大盘怎么样") == []


# ---- _pack_task 历史打包 ----

def test_pack_task_no_history():
    """任务文本必带"直接回答"指令前缀 (治出厂程序员人设跑偏, IRX 实测)。"""
    t = dsh._pack_task("问题", None)
    assert t.startswith(dsh._TASK_PREFIX) and t.endswith("问题")
    assert dsh._pack_task("问题", []) == t


def test_pack_task_packs_recent_rounds_only():
    hist = [m for i in range(8) for m in
            ({"role": "user", "content": f"问{i}"},
             {"role": "assistant", "content": f"答{i}"})]
    hist.append({"role": "tool", "content": "忽略我"})
    task = dsh._pack_task("新问题", hist)
    assert "【对话记录】" in task and "【问题】" in task
    assert "问0" not in task            # 超窗丢弃 (DSH_HISTORY_ROUNDS=5)
    assert "问3" in task and "答7" in task
    assert "忽略我" not in task          # 非 user/assistant 轮不打包


# ---- run_dsh 主路径 ----

def test_run_dsh_success_archives(monkeypatch, tmp_path):
    _patch_runtime(monkeypatch, tmp_path)
    _patch_spawn(monkeypatch, _FakeProc(out="答: 42".encode()))
    db = tmp_path / "runs.db"
    r = run(dsh.run_dsh("1+1?", db_path=db, poll_seconds=0.01))
    assert r["success"] is True and r["answer"] == "答: 42"
    import sqlite3
    row = sqlite3.connect(str(db)).execute(
        "SELECT run_id, question, answer, exit_code, stopped FROM brain_dsh_runs"
    ).fetchone()
    assert row[1] == "1+1?" and row[2] == "答: 42"
    assert row[3] == 0 and row[4] == 0


def test_run_dsh_nonzero_exit(monkeypatch, tmp_path):
    _patch_runtime(monkeypatch, tmp_path)
    _patch_spawn(monkeypatch, _FakeProc(out=b"", err="boom".encode(), rc=2))
    r = run(dsh.run_dsh("q", db_path=tmp_path / "r.db", poll_seconds=0.01))
    assert r["success"] is False and "exit 2" in r["answer"] and "boom" in r["answer"]


def test_run_dsh_archive_failure_is_hard_failure(monkeypatch, tmp_path):
    """留档失败 = 显性失败 (宁可不给答案也不出无痕答案)。"""
    _patch_runtime(monkeypatch, tmp_path)
    _patch_spawn(monkeypatch, _FakeProc())
    bad_db = tmp_path / "是个目录"   # connect 到目录必炸
    bad_db.mkdir()
    r = run(dsh.run_dsh("q", db_path=bad_db, poll_seconds=0.01))
    assert r["success"] is False
    assert ("留档" in r["answer"]) or ("异常" in r["answer"])


def test_run_dsh_stop_kills_tree(monkeypatch, tmp_path):
    _patch_runtime(monkeypatch, tmp_path)
    proc = _FakeProc(hang=True)
    _patch_spawn(monkeypatch, proc)
    db = tmp_path / "r.db"

    async def scenario():
        task = asyncio.create_task(
            dsh.run_dsh("q", run_id="run123", db_path=db, poll_seconds=0.01))
        await asyncio.sleep(0.05)
        assert dsh.stop_dsh("run123") is True
        return await task

    r = run(scenario())
    assert proc.killed is True
    assert r["success"] is False and "已停止" in r["answer"]
    import sqlite3
    stopped = sqlite3.connect(str(db)).execute(
        "SELECT stopped FROM brain_dsh_runs WHERE run_id='run123'").fetchone()
    assert stopped[0] == 1


def test_stop_dsh_unknown_run_id():
    assert dsh.stop_dsh("不存在的") is False


def test_run_dsh_disabled_channel(monkeypatch, tmp_path):
    monkeypatch.setattr(dsh, "DSH_CHANNEL_ENABLED", False)
    r = run(dsh.run_dsh("q", db_path=tmp_path / "r.db"))
    assert r["success"] is False and "已停用" in r["answer"]


def test_run_dsh_runtime_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(dsh, "DSH_NODE", tmp_path / "没有node.exe")
    monkeypatch.setattr(dsh, "DSH_ENTRY", tmp_path / "也没有bin.js")
    monkeypatch.setattr(dsh, "DSH_HOME", tmp_path / "也没有home")
    r = run(dsh.run_dsh("q", db_path=tmp_path / "r.db"))
    assert r["success"] is False and "尚未部署" in r["answer"]


def test_run_dsh_leak_scan_records_hits(monkeypatch, tmp_path):
    """会话日志含敏感词 → leak_hits 落库。"""
    _patch_runtime(monkeypatch, tmp_path)
    sess = dsh.DSH_HOME / "sessions" / "s1"
    sess.mkdir(parents=True)
    (sess / "session.jsonl").write_text("...资金账号 12345...", encoding="utf-8")
    _patch_spawn(monkeypatch, _FakeProc())
    db = tmp_path / "r.db"
    run(dsh.run_dsh("q", db_path=db, poll_seconds=0.01))
    import sqlite3
    hits = sqlite3.connect(str(db)).execute(
        "SELECT leak_hits FROM brain_dsh_runs").fetchone()[0]
    assert "资金账号" in json.loads(hits)


def test_run_dsh_registers_before_events(monkeypatch, tmp_path):
    """IRX 踩坑 #5: on_line 回调触发时进程必须已注册 (停止随时可命中)。"""
    _patch_runtime(monkeypatch, tmp_path)
    _patch_spawn(monkeypatch, _FakeProc(hang=True))

    seen = {}

    async def scenario():
        async def on_line(line):
            seen["registered"] = "run99" in dsh._runs
        t = asyncio.create_task(dsh.run_dsh(
            "q", run_id="run99", on_line=on_line,
            db_path=tmp_path / "r.db", poll_seconds=0.01))
        await asyncio.sleep(0.05)
        dsh.stop_dsh("run99")
        await t

    run(scenario())
    assert seen["registered"] is True


def test_run_dsh_archives_to_vault_when_channel(monkeypatch, tmp_path):
    """D12: 传了 channel 就必须调 archive_exchange (对话沉淀铁律, 2026-07-28)。

    archive_exchange 自身松耦合不抛, 这里 monkeypatch 只验证"被调到、参数对"。
    """
    _patch_runtime(monkeypatch, tmp_path)
    _patch_spawn(monkeypatch, _FakeProc(out=b"a"))
    cap = {}
    monkeypatch.setattr(dsh, "archive_exchange",
                        lambda ch, q, r: cap.update({"ch": ch, "q": q,
                                                     "ans": r["answer"]}))
    run(dsh.run_dsh("问题X", channel="research_tab_c1",
                    db_path=tmp_path / "r.db", poll_seconds=0.01))
    assert cap == {"ch": "research_tab_c1", "q": "问题X", "ans": "a"}

    cap.clear()
    run(dsh.run_dsh("问题Y", db_path=tmp_path / "r.db", poll_seconds=0.01))
    assert cap == {}          # 不传 channel 不归档 (脚本/测试场景不污染 vault)
