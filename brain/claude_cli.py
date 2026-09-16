"""brain/claude_cli.py — 对话大脑核心：asyncio subprocess 调 claude CLI。

计划书 v2 §5.1 落地 + 4 处修正：
1. 单一记忆：对话历史只走 --resume（不拼 prompt，见 memory.py docstring）。
2. provider 校验改【软告警】（对计划书 M3 硬拦的刻意偏差）：硬拦要求 DeepSeek
   会误伤其他兼容端点（如 z.ai）；改为在结果里带 provider_warning，用户知情。
3. 超时必杀进程：wait_for 超时后 proc.kill() + wait()，不留僵尸。
4. 成本硬闸：--max-turns 限制 agent loop 轮数（防 loop 失控烧 token）。

C 裸奔（用户 2026-07-28 拍板）：--allowedTools 含 Bash，理论能写 trade/。
2026-08-01 权限闸移除（用户拍板）：base_cmd 加 --dangerously-skip-permissions，
大脑 LLM 的任意 Bash 命令（含写/删）零审批直通——这是 Phase 2 联网搜索的前提
（-p 非交互模式下 search_web 的 Bash 调用会被权限闸自动拒绝），代价是安全模型
只剩两道软闸：QMT 真相源对账（铁律1）+ prompt 安全红线（prompts.py）+ 内容注入标记。
失败一律返 {"success": False, ...}，不抛（松耦合：大脑挂了 VERA 零感知）。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections import deque
from pathlib import Path

from brain import counter, evidence, memory, prompts
from brain.archive import archive_exchange
from utils.logger import get_logger
from utils.sysutil import close_subprocess_pipes, project_root

logger = get_logger(__name__)

VERA_ROOT = project_root()

ALLOWED_TOOLS = "Read,Grep,Glob,Bash,Write,Edit"  # C 裸奔：Bash 放行（计划书 §5.1 诚实声明）;
# 2026-08-12 用户拍板放开写文件（简报产物落 docs/brief/）：+Write,Edit

# 2026-08-14 冷启动提速（实测驱动）：claude CLI 每次 -p 起进程会做遥测/更新检查
# 等非必要网络，实测拖慢冷启动 ~30%（每次 ~3-5s）。此 env 关掉非必要流量——
# 不影响 provider 调用 / Bash 工具 / --resume 会话，只砍 CLI 自己的遥测与更新检查。
CLI_ENV = {**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}


def _standard_cfg() -> dict | None:
    """config/ai.json 的 standard 段 (AI 设置页签配置; 松耦合: 异常/缺省返 None)。"""
    try:
        from llm.ai_config import section
        return section("standard")
    except Exception as e:
        logger.debug(f"ai_config.standard 读取失败(回落 ~/.claude 现状): {e}")
        return None


def _cli_env() -> dict:
    """子进程 env: 默认 CLI_ENV; 有 standard 配置时注入 ANTHROPIC_* 三件套。

    2026-09-06 AI 设置页: 用户在页签配了标准档 (Anthropic 兼容端点+Key+模型)
    后, claude CLI 直接连配置端点, 不碰 ~/.claude/settings.json 原文件;
    未配置时 env 与现状完全一致 (零行为变化)。
    """
    env = dict(CLI_ENV)
    cfg = _standard_cfg()
    if cfg:
        if cfg.get("base_url"):
            env["ANTHROPIC_BASE_URL"] = cfg["base_url"].rstrip("/")
        if cfg.get("api_key"):
            env["ANTHROPIC_AUTH_TOKEN"] = cfg["api_key"]
        if cfg.get("model"):
            env["ANTHROPIC_MODEL"] = cfg["model"]
    return env

# provider 软告警每 channel 只报一次（2026-08-12：原来每条回答都弹，太吵）
_PROVIDER_WARNED: set[str] = set()
DEFAULT_TIMEOUT = 300  # v2: 120→300 (简报任务需要)
DEFAULT_MAX_TURNS = 20  # v2: 8→20

# 2026-07-30: provider 网络抖动类瞬时错误特征 (实测: z.ai
# "API Error: Connection closed mid-response" 跑到一半断流)。
# 区别于配额/鉴权硬错误 —— 命中则同 session 自动续跑一轮 ("继续"),
# 与 max-turns 恢复同款语义; 只续一次, 防连环抖动烧 token。
_TRANSIENT_PATTERNS = (
    "connection closed", "mid-response", "connection reset", "econnreset",
    "etimedout", "socket hang up", "fetch failed", "network error",
    "overloaded", "502", "503",
    # 注意: 不含 "429"/"rate limit" —— "429 quota exhausted" 是配额耗尽
    # (重试无意义, 与"配额/鉴权不重试"语义一致), 每分钟级限流 CLI 内部
    # 已自行退避, 不需要这一层续跑
)


def _is_transient_net(err_text: str) -> bool:
    """err_text (已小写) 是否为 provider 瞬时网络错误 (可续跑恢复)。"""
    return any(p in err_text for p in _TRANSIENT_PATTERNS)


# session 类错误模式 (坏 session 常见死因: 服务器中途被杀 → 会话留下悬挂
# tool_use → 续聊必炸 "API Error: 400 due to tool use concurrency issues",
# 且错误打在 stdout 不在 stderr —— 实测)。新 session 不可能坏, 重试必愈。
_SESSION_PATTERNS = ("session", "conversation", "tool use", "concurrency")


def _classify_recovery(err_text: str) -> str | None:
    """非零退出错误 → 恢复策略分类 (流式/同步两分支**唯一实现**, 2026-09-15 收口)。

    返 "max_turns"(同 session 续跑) / "session"(重建 session 重试) /
    "transient_net"(同 session 续跑) / None(不可恢复, 如实报错)。
    此前两个分支各手写一份三规则, 且 session 模式集已漂移
    (同步侧有 "tool use"/"concurrency", 流式侧漏) —— 同款语义不写第二份。
    """
    if "max turns" in err_text:
        return "max_turns"
    if any(p in err_text for p in _SESSION_PATTERNS):
        return "session"
    if _is_transient_net(err_text):
        return "transient_net"
    return None


def _provider_warning() -> str | None:
    """标准档 provider 软告警: AI 设置页配了 standard 档 → 不发告警
    (端点/Key 由页面显式指定, 是用户主动选的接入); 否则按 ~/.claude
    settings 判断 (沿用旧逻辑)。返告警文案; 无告警返 None。"""
    if _standard_cfg():
        return None
    return _check_provider()


def _find_cli() -> str | None:
    """找 claude 可执行文件（Windows 下 shutil.which 认 PATHEXT 的 .cmd/.exe）。"""
    return shutil.which("claude")


def _check_provider(settings_path: Path | None = None) -> str | None:
    """读 ~/.claude/settings.json 的 ANTHROPIC_BASE_URL，非 DeepSeek 给软告警。

    返告警文案；无告警返 None（含"读不到配置"——不发警报，只记 debug）。
    """
    path = settings_path or (Path.home() / ".claude" / "settings.json")
    try:
        env = json.loads(path.read_text(encoding="utf-8")).get("env", {})
        base_url = env.get("ANTHROPIC_BASE_URL", "")
        if base_url and "deepseek" not in base_url.lower():
            return f"provider 不是 DeepSeek（{base_url}），注意成本与能力差异"
    except Exception as e:
        logger.debug(f"provider 校验跳过（读配置失败）: {e}")
    return None


async def _kill_tree(proc) -> None:
    """超时必杀整棵进程树：kill → taskkill /T → 有界 wait（不挂死）。

    Windows 双坑（端到端实测）: ① cmd.exe 被杀后 node 孙进程持有管道,
    wait() 会挂到孙进程自然死 (458s); ② 必须 taskkill /T 杀整棵树。
    """
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:  # 杀整棵进程树 (Windows; 其他平台 taskkill 不存在, 静默忽略)
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/F", "/T", "/PID", str(proc.pid),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(killer.wait(), timeout=5)
    except Exception:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)  # 有界等待, 不挂死
    except TimeoutError:
        pass


def _err_detail(stderr: bytes | None, stdout: bytes | None) -> str:
    """非零退出错误摘要: stderr+stdout 拼接解码截断 (实测错误常打 stdout)。"""
    return ((stderr or b"") + b"\n" + (stdout or b"")).decode(
        "utf-8", "replace").strip()[:400]


async def ask_brain(question: str, session_id: str | None = None,
                    timeout: int = DEFAULT_TIMEOUT,
                    max_turns: int = DEFAULT_MAX_TURNS,
                    channel: str = "default",
                    archive: bool = True,
                    on_line=None, system: str | None = None) -> dict:
    """问大脑一个问题 + 归档进 vault「对话沉淀」（archive=False 豁免, 如 eval）。

    归档松耦合: 写盘失败不影响回答 (见 brain/archive.py)。
    on_line: async callable(line:str)->None, SSE 流式回调（逐行推 stdout）; None=同步原路径。
    system: 自定义 system prompt；None=用默认完整 SYSTEM_PROMPT（快路径传瘦身版）。
    """
    result = await _ask_brain_impl(question, session_id, timeout, max_turns, channel,
                                   on_line=on_line, system=system)
    if archive:
        archive_exchange(channel, question, result)
    return result


async def _ask_brain_impl(question: str, session_id: str | None = None,
                          timeout: int = DEFAULT_TIMEOUT,
                          max_turns: int = DEFAULT_MAX_TURNS,
                          channel: str = "default",
                          on_line=None, system: str | None = None) -> dict:
    """问大脑一个问题。返 {answer, success, low_confidence, warnings, session_id}。

    session_id 为 None 时按 channel 取/建持久 session（多轮 --resume）。
    """
    result = {"answer": "", "success": False, "low_confidence": False,
              "warnings": [], "session_id": None}
    cli = _find_cli()
    if not cli:
        result["answer"] = "claude CLI 未安装（npm i -g @anthropic-ai/claude-code），大脑不可用"
        return result
    # 2026-09-06: AI 设置页配了 standard 档 → 端点/Key 由页面显式指定,
    # 不再对 ~/.claude 的 provider 发软告警 (那是用户主动选的接入)
    warn = _provider_warning()
    if warn and channel not in _PROVIDER_WARNED:
        result["warnings"].append(warn)
        _PROVIDER_WARNED.add(channel)

    if session_id:
        sid, existed = session_id, True      # 显式指定 = 续既有会话
    else:
        sid, existed = memory.prepare_session(channel)
    output_fmt = "stream-json" if on_line else "text"  # ★fix: 流式 stream-json (text 不流式, 等完整结果)
    base_cmd = [cli, "-p", "--allowedTools", ALLOWED_TOOLS,
                "--output-format", output_fmt, "--max-turns", str(max_turns),
                "--dangerously-skip-permissions"]
    # 2026-07-30 空回答 BUG 根因: claude 2.x 下 -p + stream-json 必须配
    # --verbose, 否则 CLI 立即 rc=1 ("requires --verbose"), stdout 零事件,
    # 前端只能渲染"空回答"。text 格式不需要也不加。
    if output_fmt == "stream-json":
        base_cmd.append("--verbose")
    # 实测 claude 2.x: 新 session 用 --session-id 建档, 既有才用 --resume;
    # 对不存在的 sid 用 --resume 直接报错（首次提问必失败的坑）
    cmd = base_cmd + (["--resume", sid] if existed else ["--session-id", sid])
    prompt_bytes = (prompts.build_prompt(question) if system is None
                    else prompts.build_with_system(system, question)).encode("utf-8")

    async def _run_once(command: list[str], prompt: bytes = prompt_bytes):
        """跑一次 CLI。返 (returncode, stdout, stderr)；异常返 (None, b"", 错误文案)。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(VERA_ROOT),
                env=_cli_env(),
            )
        except FileNotFoundError:
            return None, b"", "claude CLI 启动失败（找不到可执行文件）"
        except Exception as e:
            # 同流式分支 (2026-09-04): 带异常类型名, 空消息异常不再打出空白
            return None, b"", f"大脑调用失败: {type(e).__name__}: {e}"
        try:
            out, serr = await asyncio.wait_for(
                proc.communicate(prompt), timeout=timeout)
        except TimeoutError:  # py3.11+ wait_for 抛内建 TimeoutError（无 asyncio.TimeoutExpired）
            await _kill_tree(proc)  # Windows 双坑 (孙进程持管道/taskkill /T) 见 _kill_tree
            return None, b"", f"大脑超时（>{timeout}s），已终止"
        finally:
            close_subprocess_pipes(proc)  # 消 Windows Proactor "closed pipe" 噪音
        return proc.returncode, out, serr

    # 2026-08-12: 流式回答全文收集器（归档用）。原流式分支 answer 返回空串,
    # 研究 TAB 对话沉淀因此断流 (research_api 一度被迫 archive=False)。
    # 优先取 stream-json 收尾 result 事件的全文 (权威), 增量 text 块拼接兜底。
    stream_collect: dict = {"parts": [], "final": None}

    # ★v2 SSE 流式路径：逐行读 stdout → on_line 回调（H-1/H-2/H-3 计划书 §3.1）
    async def _run_once_stream(command: list[str], prompt: bytes = prompt_bytes):
        """流式跑 CLI: 逐行读 stdout → on_line, 累积 stderr + stdout 尾部,
        超时杀树, cancel 响应。返 (returncode|None, stderr_bytes, stdout_tail)。
        stdout 已逐行消费不全返, 但留尾部 30 行 —— API 错误常打在 stdout
        ("API Error: Connection closed mid-response" 实测如此), 错误分类
        只看 stderr 会把瞬时网络抖动误判成未知硬错误。"""
        tail: deque = deque(maxlen=30)
        try:
            # limit=8MB: 默认 64KB 行上限会让超长 stream-json 行抛
            # "Separator is found, but chunk is longer than limit" 崩整条流
            # (2026-09-05 实测: claude CLI 大文本块单行超 64KB → SSE wedge)。
            sproc = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=8 * 1024 * 1024,
                cwd=str(VERA_ROOT), env=_cli_env())
        except FileNotFoundError:
            await on_line("[系统] claude CLI 启动失败（找不到可执行文件）")
            return None, b"", ""
        except Exception as e:
            # 2026-09-04: 带异常类型名 —— 空消息异常 (实测 NotImplementedError,
            # uvicorn reload 把循环换成 Selector 不支持子进程) 不带类型名时
            # 前端只见"大脑启动失败: "冒号后空白, 完全无法定位
            await on_line(f"[系统] 大脑启动失败: {type(e).__name__}: {e}")
            return None, b"", ""
        sproc.stdin.write(prompt)
        await sproc.stdin.drain()
        sproc.stdin.close()
        deadline = asyncio.get_running_loop().time() + timeout
        stderr_buf = []

        async def _drain_stderr():
            while True:
                chunk = await sproc.stderr.read(4096)
                if not chunk: break
                stderr_buf.append(chunk)

        async def _stream_stdout():
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError()
                line = await asyncio.wait_for(sproc.stdout.readline(), timeout=remaining)
                if not line: break
                raw = line.decode("utf-8", "replace").rstrip()
                if not raw: continue
                tail.append(raw)
                # ★fix: stream-json 每行 JSON event, 提取 text/tool_use 推进度
                try:
                    evt = json.loads(raw)
                    if evt.get("type") == "assistant":
                        for block in (evt.get("message", {}).get("content") or []):
                            if block.get("type") == "text" and block.get("text"):
                                stream_collect["parts"].append(block["text"])
                                await on_line(block["text"])
                            elif block.get("type") == "tool_use":
                                await on_line("[工具] " + block.get("name", "?"))
                    elif evt.get("type") == "result":
                        # 收尾事件带全文 (权威快照, 优先于增量块拼接)
                        stream_collect["final"] = (
                            evt.get("result") or stream_collect["final"])
                except (json.JSONDecodeError, KeyError, TypeError):
                    await on_line(raw)  # 非 JSON 行直接推

        try:
            await asyncio.gather(_stream_stdout(), _drain_stderr())
            await sproc.wait()
        except TimeoutError:
            await _kill_tree(sproc)
            await on_line(f"[系统] 大脑超时（>{timeout}s），已终止")
            return None, b"".join(stderr_buf), "\n".join(tail)
        except asyncio.CancelledError:
            try: sproc.kill()
            except Exception: pass
            raise
        finally:
            close_subprocess_pipes(sproc)  # 消 Windows Proactor "closed pipe" 噪音
        return sproc.returncode, b"".join(stderr_buf), "\n".join(tail)

    # ★v2 流式分支: on_line 提供 → 走流式 + 简化 result
    if on_line is not None:
        rc_s, serr_s, tail_s = await _run_once_stream(cmd)
        if rc_s != 0 and rc_s is not None:
            # 错误分类看 stdout 尾部 + stderr (API 错误常打在 stdout, 实测)
            err_text = (tail_s + "\n"
                        + serr_s.decode("utf-8", "replace")).lower()
            kind = _classify_recovery(err_text)  # 恢复分类唯一实现
            if kind == "max_turns":
                await on_line(f"[系统] 已达 {max_turns} 轮上限，自动续跑…")
                retry_cmd = base_cmd + ["--resume", sid]
                rc_s2, _, _ = await _run_once_stream(retry_cmd, "继续".encode())
                if rc_s2 == 0:
                    rc_s = 0
                    result["warnings"].append(
                        f"已达 {max_turns} 轮上限, 已自动续跑 (成本双倍, 知悉)")
                else:
                    result["warnings"].append(
                        f"已达 {max_turns} 轮上限, 续跑仍失败 (rc={rc_s2})")
            elif kind == "session":
                await on_line("[系统] session 失效，自动重建…")
                memory.reset_session(channel)
                sid, _ = memory.prepare_session(channel)
                retry_cmd = base_cmd + ["--session-id", sid]
                rc_s2, _, _ = await _run_once_stream(retry_cmd)
                if rc_s2 == 0:
                    rc_s = 0
                    result["warnings"].append("session 失效已自动重建（该会话上文记忆已重置）")
                else:
                    result["warnings"].append(
                        f"session 重建后仍失败 (rc={rc_s2})")
            elif kind == "transient_net":
                # 2026-07-30: provider 连接中断 (如 z.ai mid-response 断流)
                # 同 session 续跑恢复, 只续一次 —— 与 max-turns 恢复同款语义
                await on_line("[系统] provider 连接中断（网络抖动），自动续跑…")
                retry_cmd = base_cmd + ["--resume", sid]
                rc_s2, serr_s2, tail_s2 = await _run_once_stream(
                    retry_cmd, "继续".encode())
                if rc_s2 == 0:
                    rc_s = 0
                    result["warnings"].append(
                        "provider 连接中断, 已自动续跑恢复 (知悉)")
                else:
                    detail2 = (tail_s2 + "\n" + serr_s2.decode(
                        "utf-8", "replace")).strip()[:300]
                    result["warnings"].append(
                        f"provider 连接中断, 续跑仍失败 (rc={rc_s2})"
                        + (f": {detail2}" if detail2 else ""))
            else:
                # 2026-07-30: 附带错误摘要 (stdout 尾部 + stderr) —— 原实现
                # 吞掉错误只报 rc, "requires --verbose" 这类完全不可见
                detail = (tail_s + "\n" + serr_s.decode(
                    "utf-8", "replace")).strip()[:300]
                result["warnings"].append(
                    f"大脑非零退出 (rc={rc_s})" + (f": {detail}" if detail else ""))
        # 2026-08-12: 流式回答也回填 answer + 跑质量校验 (反证/引用) ——
        # 这是研究 TAB 对话沉淀恢复归档的前提 (归档写的是 result["answer"])。
        answer = (stream_collect["final"] or "\n".join(
            p for p in stream_collect["parts"] if p)).strip()
        low = False
        if rc_s == 0 and answer:
            answer, ok_counter = counter.ensure_counter_evidence(answer, question)
            answer, ok_evidence = evidence.ensure_citation(answer)
            low = not (ok_counter and ok_evidence)
        return {"answer": answer, "success": rc_s == 0,
                "low_confidence": low, "warnings": result["warnings"],
                "session_id": sid}

    rc, stdout, stderr = await _run_once(cmd)
    if rc is None:
        result["answer"] = stderr
        return result

    if rc != 0:
        err = _err_detail(stderr, stdout)
        low = err.lower()
        kind = _classify_recovery(low)  # 恢复分类唯一实现 (与流式分支同源)
        if kind == "max_turns":
            # 轮数打满 ≠ session 坏 (实测: 重置会话是冤枉它)。同 session 续跑
            # 一轮 ("继续"), 用户可见警告; 再打满就如实报 + 提示手动"继续"。
            retry_cmd = base_cmd + ["--resume", sid]
            rc, stdout, stderr = await _run_once(retry_cmd, "继续".encode())
            if rc is None:
                result["answer"] = stderr
                return result
            if rc == 0:
                result["warnings"].append(
                    f"已达 {max_turns} 轮上限, 已自动续跑一轮 (成本双倍, 知悉)")
            else:
                err2 = _err_detail(stderr, stdout)
                result["answer"] = (f"大脑退出码 {rc}: {err2 or '无输出'}\n"
                                    f"（回复「继续」可让它接着干）")
                return result
        elif kind == "session":
            # session 类错误才换新 session 重试 (_SESSION_PATTERNS 单点定义)。
            memory.reset_session(channel)
            sid, _ = memory.prepare_session(channel)
            retry_cmd = base_cmd + ["--session-id", sid]
            rc, stdout, stderr = await _run_once(retry_cmd)
            if rc is None:
                result["answer"] = stderr
                return result
            if rc == 0:
                result["warnings"].append("session 失效已自动重建（该会话上文记忆已重置）")
            else:
                err2 = _err_detail(stderr, stdout)
                result["answer"] = f"大脑退出码 {rc}: {err2 or '无输出'}"
                return result
        elif kind == "transient_net":
            # 2026-07-30: provider 连接中断 (z.ai mid-response 断流实测)。
            # 同 session 续跑一轮 ("继续"), 只续一次; 再断如实报。
            retry_cmd = base_cmd + ["--resume", sid]
            rc, stdout, stderr = await _run_once(retry_cmd, "继续".encode())
            if rc is None:
                result["answer"] = stderr
                return result
            if rc == 0:
                result["warnings"].append(
                    "provider 连接中断, 已自动续跑恢复 (知悉)")
            else:
                err2 = _err_detail(stderr, stdout)
                result["answer"] = (f"provider 连接中断, 续跑仍失败 "
                                    f"(退出码 {rc}): {err2 or '无输出'}")
                return result
        else:
            # 配额/鉴权/其他: 重试无意义, 如实报 (含 stdout, 实测错误常打 stdout)
            result["answer"] = f"大脑退出码 {rc}: {err or '无输出'}"
            return result

    raw = stdout.decode("utf-8", "replace").strip()
    if not raw:
        result["answer"] = "大脑返回空回答"
        return result

    # 回答质量三件套之硬校验两件（反证 + 引用），缺失标低置信不拦截
    # 2026-08-12: 反证校验传入 question 做意图分类 (非判断类不强制反证)
    raw, ok_counter = counter.ensure_counter_evidence(raw, question)
    raw, ok_evidence = evidence.ensure_citation(raw)
    result.update({
        "answer": raw,
        "success": True,
        "low_confidence": not (ok_counter and ok_evidence),
        "session_id": sid,
    })
    return result


def ask_brain_sync(question: str, session_id: str | None = None,
                   timeout: int = DEFAULT_TIMEOUT,
                   max_turns: int = DEFAULT_MAX_TURNS,
                   channel: str = "default",
                   archive: bool = True) -> dict:
    """同步包装（CLI / notes_gen / 定时任务用）。"""
    return asyncio.run(ask_brain(question, session_id, timeout, max_turns,
                                 channel, archive))
