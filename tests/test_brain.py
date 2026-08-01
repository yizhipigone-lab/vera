"""brain/ 对话大脑测试 — subprocess 全 mock，零网络零 CLI 依赖。

覆盖: 反证/引用校验、prompt 安全红线、session 管理、超时必杀、
CLI 缺失/启动失败/非零退出兜底、provider 软告警、golden set 判定。
"""
from __future__ import annotations

import asyncio
import json

import pytest

import brain.archive as brain_archive
import brain.claude_cli as cli
from brain import counter, evidence, memory, prompts
from brain import eval as brain_eval


@pytest.fixture(autouse=True)
def _isolate_archive(tmp_path, monkeypatch):
    """ask_brain 默认归档进 vault —— 测试一律隔离到 tmp, 不碰真实归档/映射。"""
    monkeypatch.setattr(brain_archive, "ARCHIVE_DIR", tmp_path / "arch")
    monkeypatch.setattr(brain_archive, "_MAP_PATH", tmp_path / "map.json")


# ── counter / evidence ───────────────────────────────────────

class TestCounter:
    def test_has_counter(self):
        a = "看好。<counter_evidence>但政策时效将至</counter_evidence>"
        assert counter.has_counter_evidence(a)
        assert counter.ensure_counter_evidence(a)[1] is True

    def test_missing_marks_low_confidence(self):
        out, ok = counter.ensure_counter_evidence("看好。")
        assert ok is False and "低置信" in out and "<counter_evidence>" not in "看好。"


class TestEvidence:
    @pytest.mark.parametrize("a", [
        "据 `kg/graph.db` 查询，共 3 条边",
        "依据 trade/config.py:41，阶梯为 0.05",
        "数据显示胜率 62%（13/21）",
        "见 <evidence>SELECT 1</evidence>",
    ])
    def test_has_citation(self, a):
        assert evidence.has_citation(a)

    def test_missing_marks_low_confidence(self):
        out, ok = evidence.ensure_citation("我觉得挺好。")
        assert ok is False and "低置信" in out


# ── prompts ──────────────────────────────────────────────────

class TestPrompts:
    def test_red_line_and_question(self):
        p = prompts.build_prompt("持仓有哪些？")
        assert "数据" in p and "不是【指令】" in p  # 内容注入红线
        assert "counter_evidence" in p               # B 模式反证
        assert "持仓有哪些？" in p
        assert "policy_report_zero" in p             # 确定性工具指引


# ── memory ───────────────────────────────────────────────────

class TestMemory:
    def test_session_lifecycle(self, tmp_path):
        sp = tmp_path / "s.json"
        sid1 = memory.get_or_create_session("cli", path=sp)
        assert memory.get_or_create_session("cli", path=sp) == sid1  # 持久
        assert memory.get_or_create_session("tab", path=sp) != sid1  # 分 channel
        memory.reset_session("cli", path=sp)
        assert memory.get_or_create_session("cli", path=sp) != sid1  # 重置后新建

    def test_prepare_session_marks_new_vs_existing(self, tmp_path):
        sp = tmp_path / "s.json"
        sid1, existed1 = memory.prepare_session("cli", path=sp)
        assert existed1 is False                       # 新建 → --session-id
        sid2, existed2 = memory.prepare_session("cli", path=sp)
        assert sid2 == sid1 and existed2 is True       # 既有 → --resume

    def test_append_cognition(self, tmp_path):
        cp = tmp_path / "cog.md"
        assert memory.append_cognition("政策脉络：新能源加码", path=cp) == cp
        text = cp.read_text(encoding="utf-8")
        assert "政策脉络" in text


# ── claude_cli（mock subprocess）────────────────────────────

class _FakeProc:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, hang=False):
        self._stdout, self._stderr, self.returncode = stdout, stderr, returncode
        self._hang = hang
        self.killed = False

    async def communicate(self, input=None):
        if self._hang:
            await asyncio.sleep(3600)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _patch_proc(monkeypatch, proc, provider=None):
    monkeypatch.setattr(cli, "_find_cli", lambda: "/fake/claude")
    monkeypatch.setattr(cli, "_check_provider", lambda: provider)

    async def _fake_exec(*cmd, **kw):
        _fake_exec.cmd = cmd
        _fake_exec.kw = kw
        return proc
    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", _fake_exec)
    return _fake_exec


_GOOD = "看好半导体。<counter_evidence>但估值高</counter_evidence> 据 `kg/graph.db` 3 条边。"


class TestAskBrain:
    def test_success_full_marks(self, monkeypatch):
        _patch_proc(monkeypatch, _FakeProc(stdout=_GOOD.encode()))
        r = asyncio.run(cli.ask_brain("哪些值得？", session_id="s1"))
        assert r["success"] and not r["low_confidence"] and r["session_id"] == "s1"
        assert "counter_evidence" in r["answer"]

    def test_missing_marks_low_confidence(self, monkeypatch):
        _patch_proc(monkeypatch, _FakeProc(stdout="随便答答。".encode()))
        r = asyncio.run(cli.ask_brain("q", session_id="s1"))
        assert r["success"] and r["low_confidence"]
        assert r["answer"].count("低置信") == 2  # 反证 + 引用两个标记

    def test_resume_flag_and_stdin(self, monkeypatch):
        exec_ = _patch_proc(monkeypatch, _FakeProc(stdout=_GOOD.encode()))
        asyncio.run(cli.ask_brain("问题X", session_id="sid-9"))
        cmd = exec_.cmd
        assert "--resume" in cmd and "sid-9" in cmd
        assert "--max-turns" in cmd                      # 成本硬闸
        assert "问题X" not in " ".join(cmd)              # prompt 走 stdin 不走参数
        assert exec_.kw["stdin"] is cli.asyncio.subprocess.PIPE

    def test_fresh_session_uses_session_id_flag(self, monkeypatch, tmp_path):
        """首次提问（session 未建档）必须用 --session-id, 用 --resume 必报错。"""
        monkeypatch.setattr(cli.memory, "SESSIONS_PATH", tmp_path / "s.json")
        exec_ = _patch_proc(monkeypatch, _FakeProc(stdout=_GOOD.encode()))
        r = asyncio.run(cli.ask_brain("q", channel="fresh"))
        cmd = exec_.cmd
        assert "--session-id" in cmd and "--resume" not in cmd
        assert r["success"] and r["session_id"] in cmd

    def test_timeout_kills_proc(self, monkeypatch):
        proc = _FakeProc(hang=True)
        _patch_proc(monkeypatch, proc)
        r = asyncio.run(cli.ask_brain("q", session_id="s1", timeout=0.05))
        assert not r["success"] and "超时" in r["answer"] and proc.killed

    def test_timeout_bounded_when_wait_hangs(self, monkeypatch):
        """Windows 坑（实测 458s）: cmd 杀了 node 孙进程持有管道, wait() 挂死。
        超时路径必须有界: 等 5s 兜底 + taskkill 杀树, 不许等到天荒地老。"""
        proc = _FakeProc(hang=True)

        async def _hang_wait():
            await asyncio.sleep(3600)
        proc.wait = _hang_wait
        _patch_proc(monkeypatch, proc)
        import time
        t0 = time.monotonic()
        r = asyncio.run(cli.ask_brain("q", session_id="s1", timeout=0.05))
        elapsed = time.monotonic() - t0
        assert not r["success"] and "超时" in r["answer"]
        assert elapsed < 20, f"超时路径必须有界, 实际 {elapsed:.1f}s"

    def test_cli_missing(self, monkeypatch):
        monkeypatch.setattr(cli, "_find_cli", lambda: None)
        r = asyncio.run(cli.ask_brain("q", session_id="s1"))
        assert not r["success"] and "未安装" in r["answer"]

    def test_exec_file_not_found(self, monkeypatch):
        monkeypatch.setattr(cli, "_find_cli", lambda: "/fake/claude")
        monkeypatch.setattr(cli, "_check_provider", lambda: None)

        async def _raise(*a, **k):
            raise FileNotFoundError
        monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", _raise)
        r = asyncio.run(cli.ask_brain("q", session_id="s1"))
        assert not r["success"] and "启动失败" in r["answer"]

    def test_api_error_on_stdout_triggers_retry(self, monkeypatch, tmp_path):
        """错误打在 stdout（不在 stderr）也要自愈: 服务器被杀 → 会话悬挂
        tool_use → "API Error: 400 tool use concurrency"（实测死因）。"""
        monkeypatch.setattr(cli.memory, "SESSIONS_PATH", tmp_path / "s.json")
        procs = [_FakeProc(stdout=b"API Error: 400 due to tool use concurrency issues.",
                           stderr=b"", returncode=1),
                 _FakeProc(stdout=_GOOD.encode())]
        monkeypatch.setattr(cli, "_find_cli", lambda: "/fake/claude")
        monkeypatch.setattr(cli, "_check_provider", lambda: None)
        calls = []

        async def _fake_exec(*cmd, **kw):
            calls.append(cmd)
            return procs[len(calls) - 1]
        monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", _fake_exec)

        r = asyncio.run(cli.ask_brain("q", channel="corrupt"))
        assert r["success"] and any("自动重建" in w for w in r["warnings"])
        assert len(calls) == 2 and "--session-id" in calls[1]

    def test_max_turns_retries_with_continue_same_session(self, monkeypatch, tmp_path):
        """max turns 打满: 不重置 session (它没坏), 同 session 发"继续"续跑。"""
        monkeypatch.setattr(cli.memory, "SESSIONS_PATH", tmp_path / "s.json")
        monkeypatch.setattr(cli, "_find_cli", lambda: "/fake/claude")
        monkeypatch.setattr(cli, "_check_provider", lambda: None)
        procs = [_FakeProc(stdout=b"Error: Reached max turns (10)", returncode=1),
                 _FakeProc(stdout=_GOOD.encode())]
        calls, inputs = [], []

        async def _fake_exec(*cmd, **kw):
            calls.append(cmd)
            return procs[len(calls) - 1]
        monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", _fake_exec)
        orig_comm = _FakeProc.communicate

        async def _comm(self, input=None):
            inputs.append(input)
            return await orig_comm(self, input)
        monkeypatch.setattr(_FakeProc, "communicate", _comm)

        r = asyncio.run(cli.ask_brain("q", channel="deep"))
        assert r["success"] and any("续跑" in w for w in r["warnings"])
        assert "--resume" in calls[1] and "--session-id" not in calls[1]  # 同 session 续
        assert inputs[1] == "继续".encode()

    def test_max_turns_twice_reports_with_hint(self, monkeypatch, tmp_path):
        """续跑还打满: 如实报 + 提示手动"继续", 不无限烧。"""
        monkeypatch.setattr(cli.memory, "SESSIONS_PATH", tmp_path / "s.json")
        monkeypatch.setattr(cli, "_find_cli", lambda: "/fake/claude")
        monkeypatch.setattr(cli, "_check_provider", lambda: None)

        async def _fake_exec(*cmd, **kw):
            return _FakeProc(stdout=b"Error: Reached max turns (10)", returncode=1)
        monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", _fake_exec)

        r = asyncio.run(cli.ask_brain("q", channel="deep2"))
        assert not r["success"] and "max turns" in r["answer"]
        assert "继续" in r["answer"]

    def test_retry_fail_shows_stdout_text(self, monkeypatch, tmp_path):
        """配额/鉴权类错误: 不重试 (重试无意义), 如实报 stdout 内容。"""
        monkeypatch.setattr(cli.memory, "SESSIONS_PATH", tmp_path / "s.json")
        monkeypatch.setattr(cli, "_find_cli", lambda: "/fake/claude")
        monkeypatch.setattr(cli, "_check_provider", lambda: None)
        calls = []

        async def _fake_exec(*cmd, **kw):
            calls.append(cmd)
            return _FakeProc(stdout=b"API Error: 429 quota exhausted",
                             stderr=b"", returncode=1)
        monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", _fake_exec)

        r = asyncio.run(cli.ask_brain("q", channel="dead2"))
        assert not r["success"] and "429 quota" in r["answer"]
        assert len(calls) == 1  # 不重试

    def test_transient_disconnect_auto_resume(self, monkeypatch, tmp_path):
        """2026-07-30: provider 连接中断 (mid-response) → 同 session 续跑
        一轮恢复。错误打在 stdout (实测如此), 分类必须覆盖 stdout。"""
        monkeypatch.setattr(cli.memory, "SESSIONS_PATH", tmp_path / "s.json")
        monkeypatch.setattr(cli, "_find_cli", lambda: "/fake/claude")
        monkeypatch.setattr(cli, "_check_provider", lambda: None)
        procs = [
            _FakeProc(stdout=b"API Error: Connection closed mid-response",
                      stderr=b"", returncode=1),
            _FakeProc(stdout=_GOOD.encode()),
        ]
        calls = []

        async def _fake_exec(*cmd, **kw):
            calls.append(cmd)
            return procs[len(calls) - 1]
        monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", _fake_exec)

        r = asyncio.run(cli.ask_brain("q", channel="tran1"))
        assert r["success"] and _GOOD in r["answer"]
        assert len(calls) == 2
        assert "--resume" in calls[1]               # 同 session 续跑, 不重建
        assert any("连接中断" in w for w in r["warnings"])

    def test_transient_disconnect_retry_fail_reports(self, monkeypatch, tmp_path):
        """瞬时断流只续一次: 续跑仍失败 → 如实报, 不连环重试。"""
        monkeypatch.setattr(cli.memory, "SESSIONS_PATH", tmp_path / "s.json")
        monkeypatch.setattr(cli, "_find_cli", lambda: "/fake/claude")
        monkeypatch.setattr(cli, "_check_provider", lambda: None)

        async def _fake_exec(*cmd, **kw):
            return _FakeProc(stdout=b"API Error: Connection closed mid-response",
                             stderr=b"", returncode=1)
        monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", _fake_exec)
        calls = []

        async def _counting_exec(*cmd, **kw):
            calls.append(cmd)
            return await _fake_exec(*cmd, **kw)
        monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", _counting_exec)

        r = asyncio.run(cli.ask_brain("q", channel="tran2"))
        assert not r["success"] and "续跑仍失败" in r["answer"]
        assert len(calls) == 2                      # 首发 + 仅一次续跑

    def test_nonzero_exit_session_retry_success(self, monkeypatch, tmp_path):
        """session 失效 → 自动换新 session 重试, 第二次成功。"""
        monkeypatch.setattr(cli.memory, "SESSIONS_PATH", tmp_path / "s.json")
        procs = [_FakeProc(stderr=b"No conversation found with session ID", returncode=1),
                 _FakeProc(stdout=_GOOD.encode())]
        monkeypatch.setattr(cli, "_find_cli", lambda: "/fake/claude")
        monkeypatch.setattr(cli, "_check_provider", lambda: None)
        calls = []

        async def _fake_exec(*cmd, **kw):
            calls.append(cmd)
            return procs[len(calls) - 1]
        monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", _fake_exec)

        r = asyncio.run(cli.ask_brain("q", channel="retry"))
        assert r["success"] and any("自动重建" in w for w in r["warnings"])
        assert "--session-id" in calls[1]  # 重试用新 session 建档

    def test_nonzero_exit_session_retry_also_fails(self, monkeypatch, tmp_path):
        """重试仍失败 → 如实报退出码, 不装死。"""
        monkeypatch.setattr(cli.memory, "SESSIONS_PATH", tmp_path / "s.json")
        monkeypatch.setattr(cli, "_find_cli", lambda: "/fake/claude")
        monkeypatch.setattr(cli, "_check_provider", lambda: None)

        async def _fake_exec(*cmd, **kw):
            return _FakeProc(stderr=b"No conversation found with session ID",
                             returncode=1)
        monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", _fake_exec)

        r = asyncio.run(cli.ask_brain("q", channel="dead"))
        assert not r["success"] and "退出码 1" in r["answer"]

    def test_provider_warning_soft(self, monkeypatch):
        _patch_proc(monkeypatch, _FakeProc(stdout=_GOOD.encode()),
                    provider="provider 不是 DeepSeek（https://x）")
        r = asyncio.run(cli.ask_brain("q", session_id="s1"))
        assert r["success"] and any("DeepSeek" in w for w in r["warnings"])


class TestProviderCheck:
    def test_deepseek_ok(self, tmp_path):
        p = tmp_path / "settings.json"
        p.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic"}}),
                     encoding="utf-8")
        assert cli._check_provider(p) is None

    def test_other_provider_warns(self, tmp_path):
        p = tmp_path / "settings.json"
        p.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"}}),
                     encoding="utf-8")
        w = cli._check_provider(p)
        assert w and "z.ai" in w

    def test_missing_file_silent(self, tmp_path):
        assert cli._check_provider(tmp_path / "nope.json") is None


# ── 流式 SSE (on_line 回调) ───────────────────────────────

class _FakeStdin:
    def __init__(self): self.written = None; self.closed = False
    def write(self, data): self.written = data
    async def drain(self): pass
    def close(self): self.closed = True


class _FakeStdoutReader:
    def __init__(self, lines: list[bytes]): self._lines = lines; self._idx = 0
    async def readline(self):
        if self._idx < len(self._lines):
            line = self._lines[self._idx]; self._idx += 1; return line
        return b""


class _FakeStderrReader:
    def __init__(self, data: bytes = b""): self._data = data; self._read = False
    async def read(self, n: int):
        if not self._read and self._data: self._read = True; return self._data
        return b""


class _FakeStreamProc:
    def __init__(self, stdout_lines: list[bytes], returncode: int = 0,
                 stderr_data: bytes = b""):
        self.returncode = returncode; self.killed = False
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdoutReader(stdout_lines)
        self.stderr = _FakeStderrReader(stderr_data)

    def kill(self): self.killed = True
    async def wait(self): return self.returncode


class TestAskBrainStreaming:
    def test_on_line_receives_stdout(self, monkeypatch):
        """SSE 流式: on_line 逐行收到 claude stdout。"""
        lines = [b"hello\n", b"world\n"]
        monkeypatch.setattr(cli, "_find_cli", lambda: "/fake/claude")
        monkeypatch.setattr(cli, "_check_provider", lambda: None)

        async def _fake_exec(*a, **kw):
            return _FakeStreamProc(lines)
        monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", _fake_exec)

        collected = []
        async def on_line(line: str): collected.append(line)

        r = asyncio.run(cli.ask_brain("q", session_id="s1", on_line=on_line))
        assert r["success"]
        assert collected == ["hello", "world"]

    def test_on_line_max_turns_retry(self, monkeypatch, tmp_path):
        """SSE 流式: max_turns stderr → 续跑 + on_line 收系统消息。"""
        monkeypatch.setattr(cli.memory, "SESSIONS_PATH", tmp_path / "s.json")
        monkeypatch.setattr(cli, "_find_cli", lambda: "/fake/claude")
        monkeypatch.setattr(cli, "_check_provider", lambda: None)

        procs = [
            _FakeStreamProc([b"a\n"], returncode=1,
                           stderr_data=b"Error: Reached max turns (20)"),
            _FakeStreamProc([b"b\n"]),
        ]
        calls = []

        async def _fake_exec(*cmd, **kw):
            calls.append(cmd)
            return procs[len(calls) - 1]
        monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", _fake_exec)

        collected = []
        async def on_line(line: str): collected.append(line)

        r = asyncio.run(cli.ask_brain("q", session_id="s1", on_line=on_line, max_turns=20))
        assert r["success"]
        assert any("续跑" in l for l in collected if "[系统]" in l)
        assert r["warnings"]

    def test_on_line_session_rebuild_retry(self, monkeypatch, tmp_path):
        """SSE 流式: session 失效 stderr → 重建 + on_line 收系统消息。"""
        monkeypatch.setattr(cli.memory, "SESSIONS_PATH", tmp_path / "s.json")
        monkeypatch.setattr(cli, "_find_cli", lambda: "/fake/claude")
        monkeypatch.setattr(cli, "_check_provider", lambda: None)

        procs = [
            _FakeStreamProc([b"a\n"], returncode=1,
                           stderr_data=b"No conversation found with session"),
            _FakeStreamProc([b"b\n"]),
        ]
        calls = []

        async def _fake_exec(*cmd, **kw):
            calls.append(cmd)
            return procs[len(calls) - 1]
        monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", _fake_exec)

        collected = []
        async def on_line(line: str): collected.append(line)

        r = asyncio.run(cli.ask_brain("q", session_id="s1", on_line=on_line))
        assert r["success"]
        assert any("session" in l for l in collected if "[系统]" in l)
        assert r["warnings"]

    def test_on_line_nonzero_other_error(self, monkeypatch):
        """SSE 流式: 非 max_turns/session 错误 → success=False。"""
        monkeypatch.setattr(cli, "_find_cli", lambda: "/fake/claude")
        monkeypatch.setattr(cli, "_check_provider", lambda: None)

        async def _fake_exec(*a, **kw):
            return _FakeStreamProc([b"partial\n"], returncode=1,
                                  stderr_data=b"Some other error")
        monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", _fake_exec)

        collected = []
        async def on_line(line: str): collected.append(line)

        r = asyncio.run(cli.ask_brain("q", session_id="s1", on_line=on_line))
        assert not r["success"]
        assert "大脑非零退出" in str(r["warnings"])
        assert collected == ["partial"]

    def test_on_line_transient_disconnect_retry(self, monkeypatch, tmp_path):
        """SSE 流式: API 断流错误打在 stdout (实测) → 尾部捕获分类为
        瞬时错误 → 同 session 自动续跑恢复。"""
        monkeypatch.setattr(cli.memory, "SESSIONS_PATH", tmp_path / "s.json")
        monkeypatch.setattr(cli, "_find_cli", lambda: "/fake/claude")
        monkeypatch.setattr(cli, "_check_provider", lambda: None)

        procs = [
            _FakeStreamProc(
                [b"partial answer\n",
                 b"API Error: Connection closed mid-response.\n"],
                returncode=1, stderr_data=b""),       # 注意: stderr 为空
            _FakeStreamProc([b"resumed\n"]),
        ]
        calls = []

        async def _fake_exec(*cmd, **kw):
            calls.append(cmd)
            return procs[len(calls) - 1]
        monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", _fake_exec)

        collected = []
        async def on_line(line: str): collected.append(line)

        r = asyncio.run(cli.ask_brain("q", session_id="s1", on_line=on_line))
        assert r["success"]
        assert any("续跑" in l for l in collected if "[系统]" in l)
        assert any("连接中断" in w for w in r["warnings"])
        assert len(calls) == 2


# ── eval ─────────────────────────────────────────────────────

class TestEval:
    def test_check_answer(self):
        case = {"must_contain": ["AVOID", "\\d"]}
        assert brain_eval.check_answer(case, "AVOID 档 3 笔")
        assert not brain_eval.check_answer(case, "没有相关信息")

    def test_golden_set_shape(self):
        cases = brain_eval.load_golden()
        assert len(cases) == 10
        assert all(c.get("q") and c.get("must_contain") for c in cases)

    def test_run_eval_no_cli(self, monkeypatch):
        monkeypatch.setattr(brain_eval, "_find_cli", lambda: None)
        report = brain_eval.run_eval(limit=1)
        assert "error" in report and report["gate_pass"] is False
