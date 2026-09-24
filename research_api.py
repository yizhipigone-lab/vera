# -*- coding: utf-8 -*-
"""研究 TAB 端点 /api/research/* (2026-08-01 批次5 C4c 从 server.py 抽出, 纯移动不改行为)。

含: 对话大脑 (P3, 2026-07-28)。
松耦合: brain/ 模块挂了只影响本路由的接口, 其余页面零感知。
"""
import re
import uuid

from fastapi import APIRouter

from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter()


_BRAIN_CONV_RE = re.compile(r"^[a-zA-Z0-9_-]{1,20}$")


def _brain_channel(conv: str) -> str:
    """会话号 → brain channel。非法会话号一律归到 default (防注入/防乱建)。"""
    if conv and _BRAIN_CONV_RE.match(conv):
        return f"research_tab_{conv}"
    return "research_tab_default"


def _channel_of(body: dict) -> str:
    """body.conv → brain channel (缺省/非法会话号归 default)。"""
    return _brain_channel(body.get("conv") or "")


def _extract_question(body: dict):
    """question 提取+校验: 合法返回 (question, None); 为空返回 (None, 错误响应 dict)。"""
    question = (body.get("question") or "").strip()
    if not question:
        return None, {"answer": "请输入问题", "success": False}
    return question, None


def _mode_of(body: dict) -> str:
    """body → 对话档位 (三态): fast=秒回 / standard=标准大脑 / deep=深度思考。

    向后兼容: 旧前端只发 deep:true (无 mode) → 归 deep; 缺省 → standard。
    """
    mode = (body.get("mode") or "").strip().lower()
    if mode in ("fast", "standard", "deep"):
        return mode
    if body.get("deep"):
        return "deep"
    return "standard"


def _brain_fail(e: Exception) -> dict:
    """大脑调用失败的统一错误响应 (松耦合兜底, 不炸 server)。"""
    return {"answer": f"大脑调用失败: {e}", "success": False}


async def _dispatch(question: str, body: dict, on_line=None,
                    standard_timeout: int = 480, standard_turns: int = 40) -> dict:
    """三态分流核心 (chat 同步 + stream 共用, 避免两份分流漂移)。

    fast     秒回: 快路径(个股/大盘, 带数据) → 未命中落 DeepSeek 直答
    standard 标准大脑: 快路径 → claude agent loop (现状, 可读写代码)
    deep     深度思考: DSH (DeepSeek harness, cwd=VERA 项目根)

    standard_timeout/turns: 同步端点(300/20)与 stream(480/40)保留原差异。
    """
    mode = _mode_of(body)
    channel = _channel_of(body)
    from brain import fastpath
    if mode == "deep":
        from brain import dsh_channel
        run_id = body.get("run_id") or uuid.uuid4().hex[:12]
        return await dsh_channel.run_dsh(
            question, history=body.get("history"), run_id=run_id,
            on_line=on_line, channel=channel)
    if mode == "fast":
        # 2026-09-05: 秒回档 = 纯 DeepSeek 直答 (不碰 claude CLI, 不碰
        # fastpath 数据链路)。设计依据: 快速档定位是 1-3s 口头问答; 个股诊断
        # 那种"取数几十秒 + 八段报告"的重活不该进快速档 (实测: fastpath 的
        # agent 式成文指令喂给 DeepSeek 直答 → 它输出"写作计划"而非成品,
        # 且取数链路 ~60s 根本不快)。问个股/大盘请用标准或深度档。
        from brain import quick_chat
        return await quick_chat.quick_answer(
            question, history=body.get("history"), on_line=on_line,
            channel=channel)
    # standard (缺省): 2026-08-13 提速 — 个股诊断→大盘/盘面 依次走快路径
    # (单次 LLM 成文), 返 None 回落原 agent loop
    from brain.claude_cli import ask_brain
    fast = await fastpath.try_stock_diagnosis(
        question, channel=channel, on_line=on_line, timeout=standard_timeout)
    if fast is not None:
        return fast
    fast = await fastpath.try_market_brief(
        question, channel=channel, on_line=on_line, timeout=standard_timeout)
    if fast is not None:
        return fast
    return await ask_brain(question, timeout=standard_timeout,
                           max_turns=standard_turns,
                           channel=channel, on_line=on_line)


@router.post("/api/research/chat")
async def research_chat(body: dict):
    """对话大脑 (同步): {question, conv, mode} → 三态分流。

    mode: fast(秒回)/standard(标准大脑)/deep(深度思考); 缺省/深=兼容旧前端。
    多会话: conv 互相隔离 (各自 claude session, 可并行)。直接等待
    (单用户), 大脑崩了返 success=False 不影响其他页。
    """
    question, err = _extract_question(body)
    if err:
        return err
    try:
        # 同步端点保留原 300s/20 轮上限 (stream 端点是 480/40)
        return await _dispatch(question, body,
                               standard_timeout=300, standard_turns=20)
    except Exception as e:  # 松耦合兜底: 大脑任何异常不炸 server
        logger.warning(f"对话大脑异常 (松耦合): {e}", exc_info=True)
        return _brain_fail(e)


@router.post("/api/research/chat/stream")
async def research_chat_stream(body: dict):
    """对话大脑 SSE — asyncio.Queue 桥接 on_line 与 yield (v2 H-1)。

    body.mode 三态 (fast/standard/deep); deep 时先发 channel 事件 (run_id),
    前端拿到才能点停止。0.5s 轮询 + task.done() 检测；断开 cancel task (H-3)。"""
    import asyncio as _aio
    import json as _json

    from fastapi.responses import StreamingResponse

    question, err = _extract_question(body)
    if err:
        return err

    is_deep = _mode_of(body) == "deep"
    run_id = uuid.uuid4().hex[:12] if is_deep else None
    if run_id:
        body["run_id"] = run_id  # _dispatch 里 DSH 分支直接用, 保证事件同号

    async def generate():
        queue: _aio.Queue = _aio.Queue()
        async def on_line(line: str):
            await queue.put(line)

        if run_id:
            # 先发 channel 事件 (前端拿 run_id 才能点停止);
            # 此刻 dsh_channel.run_dsh 内部已先注册进程 (IRX 踩坑 #5)
            yield f"data: {_json.dumps({'type': 'channel', 'channel': 'dsh', 'run_id': run_id}, ensure_ascii=False)}\n\n"

        async def _run():
            return await _dispatch(question, body, on_line=on_line)

        task = _aio.create_task(_run())
        try:
            while True:
                try:
                    line = await _aio.wait_for(queue.get(), timeout=0.5)
                except _aio.TimeoutError:
                    if task.done(): break
                    continue
                yield f"data: {_json.dumps({'type':'line','text':line}, ensure_ascii=False)}\n\n"
            while not queue.empty():
                line = queue.get_nowait()
                yield f"data: {_json.dumps({'type':'line','text':line}, ensure_ascii=False)}\n\n"
        except _aio.CancelledError:
            task.cancel()
            raise
        finally:
            if not task.done():
                task.cancel()
                try: await task
                except _aio.CancelledError: pass
        try:
            result = await task
        except Exception as e:
            logger.warning(f"SSE ask_brain 异常: {e}", exc_info=True)
            result = _brain_fail(e)
        yield f"data: {_json.dumps({**result, 'type':'done'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/api/research/chat/reset")
async def research_chat_reset(body: dict):
    """重开某条会话 (清它的 claude session 记忆, 其他会话不动)。"""
    from brain import memory
    memory.reset_session(_channel_of(body))
    return {"success": True}


@router.post("/api/research/chat/stop")
async def research_chat_stop(body: dict):
    """停止一次深度思考 (杀进程树)。run_id 非法/不存在 → success=False。"""
    from brain import dsh_channel
    run_id = (body.get("run_id") or "")[:12]
    return {"success": bool(run_id) and dsh_channel.stop_dsh(run_id)}
