"""brain/dsh_channel.py — DSH 深度思考通道 (复刻 IRX ADR-011 架构, VERA 化).

外部接口 5 个符号: run_dsh / stop_dsh / scan_leak / rewrite_agent_default_model /
apply_deep_settings. research_api 是唯一运行期调用方 (后两个为 AI 设置页接线);
子进程生命周期 / 会话日志取证 / 留档落库全藏在内部.

"摇醒"机制 (IRX 手册 §3): DSH headless 是一次性程序, 每问摇醒一个新进程,
答完即灭; 多轮记忆靠调用方把近期对话打包进任务文本 (headless 无会话续接).

VERA 化差异 (对照 IRX src/irx/assistant/dsh.py):
- asyncio 子进程 (与 brain/claude_cli.py 同款), 不用同步 Popen + sleep 轮询;
  杀进程树复用 claude_cli._kill_tree (Windows 双坑已解决: 孙进程持管道 + taskkill /T)。
- 直调 node.exe + bin.js, 不经 dsh.cmd 批处理 (IRX 2026-09-04 实测: 批处理
  参数有截断风险, 带 5 轮对话历史的任务文本会被咬断)。
- 子进程输出走 workspace 下日志文件, 不用 PIPE (IRX 2026-09-04 实测: Windows
  管道缓冲约 64KB, 无人排干时子进程写阻塞挂死 — 进度轮播 4 分钟无产出)。
- 任务文本带"直接回答"指令前缀, 人设本体在 dsh-runtime/workspace/CLAUDE.md
  (IRX 2026-09-04 实测: 出厂是程序员人设, 裸问题会答非所问 — 问财报它聊环境)。
- 留档落 SQLite data/brain_dsh_runs.db (VERA 无 Postgres); 留档失败 = 显性失败
  (宁可不给答案也不出无痕答案 — 检测型控制是底线不是装饰).
- 30 分钟安全上限防僵尸 (IRX 无超时纯手动停; VERA 有无人值守场景).
- 传 channel 时调用 brain.archive.archive_exchange 沉淀 vault 对话归档
  (2026-07-28 用户拍板铁律; 该调用原本藏在 ask_brain 内部, 本通道绕过它必须自补).
失败一律返 {"success": False, ...}, 不抛 (松耦合: DSH 挂了主大脑零感知).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path

from brain.archive import archive_exchange  # 对话沉淀 (松耦合, 自身不抛)
from brain.claude_cli import _kill_tree  # 复用: Windows 进程树双坑已解决
from utils.logger import get_logger
from utils.sysutil import project_root

logger = get_logger(__name__)

_ROOT = project_root()
DSH_RUNTIME = _ROOT / "dsh-runtime"
DSH_NODE = DSH_RUNTIME / "node" / "node.exe"
DSH_ENTRY = DSH_RUNTIME / "app" / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
DSH_HOME = DSH_RUNTIME / "home"
DSH_SETTINGS = DSH_HOME / "settings.yaml"
DSH_WORKSPACE = DSH_RUNTIME / "workspace"
DSH_CWD = _ROOT  # 2026-09-05 B 方案: 深度档工作目录=VERA 项目根, 能读写项目代码
DSH_DB = _ROOT / "data" / "brain_dsh_runs.db"

DSH_CHANNEL_ENABLED = True   # 应急总开关: False 一键停用, 主大脑零影响
DSH_PROGRESS_SECONDS = 2.0   # 等待动作提示轮询间隔
DSH_MAX_SECONDS = 1800       # 安全上限 (防僵尸; 正常深度思考 1-5 分钟)
DSH_HISTORY_ROUNDS = 5       # 多轮记忆打包轮数 (打进任务文本, headless 无会话续接)

# 泄漏扫描关键词 (单一来源): 命中 = 出网上下文含敏感域特征 → 告警记录
DSH_LEAK_KEYWORDS = ("资金账号", "股东账号", "交易密码", "trade.db")
_ID_CARD_RE = re.compile(r"\b\d{17}[\dXx]\b")

# 会话日志尾部事件 → 等待 UI 动作提示 (IRX 同款人话翻译)
_ACTION_HINTS = (
    ("user/message", "正在思考"),
    ("tool/call", "正在调用工具"),
    ("tool/result", "正在整理工具结果"),
    ("assistant/chunk", "正在撰写答复"),
    ("assistant/message", "正在撰写答复"),
    ("step/start", "正在推进"),
)

_runs: dict[str, asyncio.subprocess.Process] = {}
_stopped: set[str] = set()

_DDL = """
CREATE TABLE IF NOT EXISTS brain_dsh_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL,
    question         TEXT NOT NULL,
    answer           TEXT,
    exit_code        INTEGER,
    stopped          INTEGER NOT NULL DEFAULT 0,
    elapsed_ms       INTEGER,
    session_log_path TEXT,
    context_sha256   TEXT,
    context_chars    INTEGER,
    leak_hits        TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_brain_dsh_runs_ts ON brain_dsh_runs (created_at DESC);
"""


# ---------------------------------------------------------------- 公开接口

def scan_leak(content: str) -> list[str]:
    """纯函数: 扫描文本, 返回命中的敏感域特征列表 (关键词 + 证件号模式)。"""
    hits = [kw for kw in DSH_LEAK_KEYWORDS if kw.lower() in content.lower()]
    if _ID_CARD_RE.search(content):
        hits.append("证件号模式")
    return hits


def rewrite_agent_default_model(text: str, provider: str, model: str) -> str:
    """纯函数: 改写 settings.yaml 文本的 agent-default-model 两行。

    只替换 agent-default-model 段下的 provider:/model: 值, 其余 (注释/
    其他配置段) 原样保留。找不到该段时在文末追加完整段。
    """
    block = f"agent-default-model:\n  provider: {provider}\n  model: {model}\n"
    if "agent-default-model:" not in text:
        return text.rstrip("\n") + "\n" + block
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    in_block = False
    block_done = False
    while i < len(lines):
        line = lines[i]
        if line.strip() == "agent-default-model:":
            in_block = True
            block_done = True
            out.append(line)
            i += 1
            # 吞掉原 provider:/model: 两行 (若缩进对齐则归本段)
            consumed = 0
            while i < len(lines) and consumed < 2 and lines[i].startswith("  "):
                out.append(None)  # placeholder, 下面剔除
                i += 1
                consumed += 1
            out.extend([f"  provider: {provider}", f"  model: {model}"])
            continue
        if in_block and line.strip() and not line.startswith("  "):
            in_block = False  # 出段 (下一个非缩进行)
        out.append(line)
        i += 1
    cleaned = [l for l in out if l is not None]
    result = "\n".join(cleaned)
    if not block_done:
        result = text.rstrip("\n") + "\n" + block
    if not result.endswith("\n"):
        result += "\n"  # 保留尾换行 (原文件惯例, 防 diff 噪音)
    return result


def apply_deep_settings() -> bool:
    """深度思考档接入配置 → 改写 DSH settings 的 agent-default-model。

    AI 设置页签配了 deep 段 (provider/model) 时, 每次 run_dsh 前调用,
    让深度思考档用页面上选的适配器+模型; 未配置/文件缺失/读失败 →
    静默跳过 (按 DSH 现状运行, 零行为变化, 松耦合不抛)。
    返是否实际改写 (测试/日志用)。
    """
    try:
        from llm.ai_config import section
        cfg = section("deep") or {}
    except Exception as e:
        logger.debug(f"ai_config.deep 读取失败(按 DSH 现状运行): {e}")
        return False
    provider = (cfg.get("provider") or "").strip()
    model = (cfg.get("model") or "").strip()
    if not provider or not model:
        return False
    try:
        if not DSH_SETTINGS.is_file():
            logger.debug(f"DSH settings 不存在(未部署深度思考), 跳过改写: {DSH_SETTINGS}")
            return False
        text = DSH_SETTINGS.read_text(encoding="utf-8", errors="replace")
        new_text = rewrite_agent_default_model(text, provider, model)
        if new_text == text:
            return False  # 已是目标值, 不写盘 (省 mtime)
        DSH_SETTINGS.write_text(new_text, encoding="utf-8")
        logger.info(f"DSH 深度思考档已按 AI 设置切换: provider={provider} model={model}")
        return True
    except Exception as e:
        logger.warning(f"DSH settings 改写失败(按现状运行): {e}")
        return False


def stop_dsh(run_id: str) -> bool:
    """终止一次运行 (杀进程树)。返回是否找到了该运行。"""
    proc = _runs.get(run_id)
    if proc is None:
        return False
    _stopped.add(run_id)
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    return True


async def run_dsh(question: str, history: list[dict] | None = None,
                  run_id: str | None = None, on_line=None,
                  channel: str | None = None,
                  db_path: Path | None = None,
                  poll_seconds: float = DSH_PROGRESS_SECONDS,
                  max_seconds: int = DSH_MAX_SECONDS) -> dict:
    """摇醒一次 DSH 深度思考: 子进程执行 → 周期动作提示 → 收尾留档。

    返回与 brain.claude_cli.ask_brain 同款契约
    {answer, success, low_confidence, warnings, session_id}, 路由层/前端零适配。
    history 为 [{role, content}] (仅 user/assistant 轮被打包进任务文本)。
    on_line: async callable(line:str), SSE 进度回调; None=静默。
    channel: 传了就把问答沉淀进 vault 对话归档 (archive_exchange); None=不归档。
    """
    result = {"answer": "", "success": False, "low_confidence": False,
              "warnings": [], "session_id": None}
    if not DSH_CHANNEL_ENABLED:
        result["answer"] = "深度思考通道已停用 (DSH_CHANNEL_ENABLED=False)。取消勾选可用普通模式提问。"
        return result
    if not DSH_NODE.exists() or not DSH_ENTRY.exists() or not DSH_HOME.is_dir():
        result["answer"] = ("深度思考通道尚未部署: 缺 dsh-runtime/ 便携运行时。"
                            "部署见 docs/plan/2026-09-04_研究大脑DSH深度思考通道_计划书.md Task 2。")
        return result

    run_id = run_id or uuid.uuid4().hex[:12]
    task = _pack_task(question, history)
    env = {**os.environ, "DSH_HOME": str(DSH_HOME)}
    t0 = time.monotonic()

    # AI 设置页签 (deep 档): 用户配置了 provider/model → 改写 DSH settings,
    # 深度思考档用页面选的适配器+模型; 未配置/失败静默按现状运行。
    apply_deep_settings()

    # 子进程输出走日志文件而非 PIPE: Windows 管道缓冲约 64KB, 无人排干时
    # 子进程写阻塞挂死 (IRX 2026-09-04 端到端实测: 进度轮播 4 分钟无产出)。
    # 临时日志即读即删 — 取证全集在 home 会话日志, 不在这些临时文件。
    DSH_WORKSPACE.mkdir(parents=True, exist_ok=True)
    out_path = DSH_WORKSPACE / f"dsh-{run_id}.out.log"
    err_path = DSH_WORKSPACE / f"dsh-{run_id}.err.log"
    try:
        out_f = out_path.open("w", encoding="utf-8", errors="replace")
        err_f = err_path.open("w", encoding="utf-8", errors="replace")
    except OSError as e:
        result["answer"] = f"深度思考通道日志文件创建失败: {e}"
        return result

    try:
        try:
            proc = await _spawn(task, env, out_f, err_f)
        except Exception as e:
            result["answer"] = f"深度思考通道启动失败: {type(e).__name__}: {e}"
            return result

        # 先注册进程再发任何事件 (IRX 踩坑 #5: 保证任意时刻停止都能命中)
        _runs[run_id] = proc
        stopped = False
        timed_out = False
        try:
            if on_line:
                await on_line(f"[DSH] 已摇醒深度思考进程 (run_id={run_id})，答完自动退出…")
            while True:
                if run_id in _stopped:
                    stopped = True
                    break
                try:
                    # 不加 shield: wait_for 超时只取消"等待协程", 进程照跑;
                    # 加 shield 会让每个轮询周期堆积一个挂起 task
                    await asyncio.wait_for(proc.wait(), timeout=poll_seconds)
                    break  # 进程自然结束
                except TimeoutError:
                    pass
                if time.monotonic() - t0 > max_seconds:
                    await _kill_tree(proc)
                    timed_out = True
                    break
                if on_line:
                    await on_line(f"[DSH] {_tail_action()} · {int(time.monotonic() - t0)}s")
            await proc.communicate()  # 无管道可读, 此处仅等待退出 (停止/超时已被树杀)
            # 撞车修复 (2026-09-05 实测): stop_dsh 的 kill 会让 wait() 先返回,
            # 循环走"自然结束"分支 break, stopped 漏标记 → 用户点停止却看到
            # "未能完成 (exit 1)"。收尾前再认一次停止标记。
            stopped = stopped or (run_id in _stopped)
        finally:
            out_f.close()
            err_f.close()
            _runs.pop(run_id, None)
            _stopped.discard(run_id)

        answer, err_text = "", ""
        try:
            answer = out_path.read_text(encoding="utf-8", errors="replace").strip()
            err_text = err_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass
        for p in (out_path, err_path):
            try:
                p.unlink()
            except OSError:
                pass

        rc = proc.returncode
        hits = _archive(run_id, question, answer, rc, stopped or timed_out,
                        t0, db_path)  # 留档失败=抛 → 显性失败
        if hits:
            result["warnings"].append(f"泄漏扫描命中 {hits}（已告警留档）")
        if timed_out:
            result["answer"] = f"深度思考超时 (> {max_seconds}s)，已终止"
            return result
        if stopped:
            result["answer"] = "已停止: 本次深度思考被手动终止。"
            return result
        if rc == 0 and answer:
            result.update({"answer": answer, "success": True})
        else:
            lines = err_text.splitlines()
            hint = lines[0][:120] if lines else "无 stderr 输出"
            result["answer"] = (f"深度思考未能完成 (exit {rc}): {hint}\n"
                                "可稍后重试, 或取消勾选走普通模式。")
        return result
    except Exception as e:
        # 留档失败等异常 → 显性失败 (不出无痕答案), 仍松耦合不炸 server
        logger.warning(f"DSH 通道异常: {e}", exc_info=True)
        result["answer"] = f"深度思考异常 (含留档失败): {type(e).__name__}: {e}"
        return result
    finally:
        for f in (out_f, err_f):  # close 幂等: 正常路径已关, 异常路径兜底
            try:
                f.close()
            except Exception:
                pass
        if channel:
            # D12: vault 对话沉淀 (铁律 2026-07-28)。archive_exchange 松耦合不抛。
            # 停止/失败也归档 —— archive.py 惯例: 失败标 [失败], 试错也是思考
            archive_exchange(channel, question, result)


# ---------------------------------------------------------------- 内部实现

async def _spawn(task: str, env: dict, out_f, err_f):
    """起 DSH headless 子进程 (独立函数方便测试 monkeypatch)。

    直调 node.exe + bin.js, 不经 dsh.cmd 批处理 (IRX 实测: 批处理参数
    有截断风险)。输出写日志文件, 不用 PIPE (IRX 实测: 64KB 缓冲挂死)。
    cwd = VERA 项目根 (B 方案): 深度档能读写项目代码; 临时日志仍写
    workspace (不入项目根, 避免污染 reload 监视)。DSH 会话日志取证的
    出网上下文会含根 CLAUDE.md(已核实无泄漏词, 不误报)。
    """
    return await asyncio.create_subprocess_exec(
        str(DSH_NODE), str(DSH_ENTRY), "--profile", "headless", task,
        stdout=out_f, stderr=err_f,
        cwd=str(DSH_CWD), env=env)


# 任务指令前缀 (IRX 2026-09-04 实测: 出厂程序员人设遇裸问题会答非所问,
# 比如问财报它聊工作区环境)。cwd 是 VERA 项目根 (2026-09-05 B 方案),
# 根 CLAUDE.md 是工程铁律全集 (会诱导聊工程), 人设必须在前缀里压住:
# 默认直接答题; 用户点名要读/改项目文件时才动手, 且只动点名的。
_TASK_PREFIX = (
    "请直接回答下面的问题：结论先行、给出依据与出处；用大白话"
    "（术语首次出现配一句通俗解释）；不要反问澄清。\n"
    "工作说明：你当前在 VERA 量化项目根目录（可读写其代码，但默认不要"
    "翻阅项目文件）；除非用户明确要求你读取/修改某个具体项目文件，否则"
    "直接凭已有知识与联网回答；涉及需要实时数据的具体数字时如实说明"
    "『该数字需查证』。一律用中文回答。")


def _pack_task(question: str, history: list[dict] | None) -> str:
    """任务文本 = 直接回答指令前缀 + (可选)近期对话 + 问题。

    先过滤再截窗 —— 窗口算的是对话轮数, 杂讯角色不占名额。"""
    body = question
    if history:
        rounds = [m for m in history if m.get("role") in ("user", "assistant")]
        lines = []
        for m in rounds[-(DSH_HISTORY_ROUNDS * 2):]:
            label = "用户" if m.get("role") == "user" else "助手"
            lines.append(f"{label}: {m.get('content') or ''}")
        if lines:
            body = ("【对话记录】\n" + "\n".join(lines)
                    + f"\n\n【问题】\n{question}")
    return _TASK_PREFIX + "\n\n" + body


def _latest_session_log() -> Path | None:
    """定位 DSH home 下最新会话日志 (= 本次运行的出网上下文全集)。

    .jsonl 与 .jsonl.zstd 都认 (IRX 仓实测为 zstd 压缩, 其 glob 只认
    .jsonl —— 本实现修正该疑点)。
    """
    d = DSH_HOME / "sessions"
    if not d.is_dir():
        return None
    logs = [p for p in d.rglob("*")
            if p.is_file() and (p.name.endswith(".jsonl")
                                or p.name.endswith(".jsonl.zstd"))]
    return max(logs, key=lambda p: p.stat().st_mtime) if logs else None


def _log_text(raw: bytes, path: Path) -> str:
    """会话日志 bytes → 文本 (zstd 压缩则解压; 缺 zstandard 包且真遇到才报错)。"""
    if path.name.endswith(".zstd"):
        import zstandard  # 可选依赖: 仅压缩日志需要
        raw = zstandard.ZstdDecompressor().stream_reader(raw).read()
    return raw.decode("utf-8", errors="ignore")


def _tail_action() -> str:
    """读会话日志尾部, 把最近事件翻译成等待 UI 动作提示 (zstd 压缩时退化为"工作中")。"""
    log = _latest_session_log()
    if log is None:
        return "启动中"
    try:
        with log.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return "工作中"
    for marker, hint in _ACTION_HINTS:
        if marker in tail:
            return hint
    return "工作中"


def _archive(run_id: str, question: str, answer: str, exit_code: int | None,
             stopped: bool, t0: float, db_path: Path | None = None) -> list:
    """每问全量留档: 会话日志哈希 + 泄漏扫描 + 落 SQLite。

    失败直接抛 (显性失败)。返回泄漏命中列表 (供调用方写进 warnings 让前端可见)。"""
    db = db_path or DSH_DB
    log = _latest_session_log()
    ctx_hash, ctx_chars, hits = None, None, []
    if log is not None:
        raw = log.read_bytes()
        ctx_hash = hashlib.sha256(raw).hexdigest()
        ctx_chars = len(raw)
        hits = scan_leak(_log_text(raw, log))
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_DDL)
        conn.execute(
            "INSERT INTO brain_dsh_runs (run_id, question, answer, exit_code,"
            " stopped, elapsed_ms, session_log_path, context_sha256,"
            " context_chars, leak_hits) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, question[:500], answer[:20000], exit_code, int(stopped),
             int((time.monotonic() - t0) * 1000),
             str(log) if log else None, ctx_hash, ctx_chars,
             json.dumps(hits, ensure_ascii=False) if hits else None))
        conn.commit()
    finally:
        conn.close()
    if hits:
        logger.warning(f"DSH 泄漏告警 run={run_id}: 命中 {hits}")
    return hits
