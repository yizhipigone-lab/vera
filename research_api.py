# -*- coding: utf-8 -*-
"""研究 TAB 端点 /api/research/* (2026-08-01 批次5 C4c 从 server.py 抽出, 纯移动不改行为)。

含: 政策影响查询 + 对话大脑 (P3, 2026-07-28)。
松耦合: brain/ 模块挂了只影响这两个接口, 其余页面零感知。
卸载 = 删本路由注册 + index.html 卡片 + web/js/brain_chat.js。
"""
import re

from fastapi import APIRouter

from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.get("/api/research/policy_impact")
async def research_policy_impact(stock: str):
    """研究TAB: 票 → 政策影响列表 (P1, 计划书 §6).

    GET /api/research/policy_impact?stock=300750.SZ → {stock, impacts:[{title,direction,strength,evidence}]}
    松耦合: 票不在 A 层/图谱 → impacts=[].
    """
    from kg.query import get_stock_policy_impact
    return {"stock": stock, "impacts": get_stock_policy_impact(stock)}


_BRAIN_CONV_RE = re.compile(r"^[a-zA-Z0-9_-]{1,20}$")


def _brain_channel(conv: str) -> str:
    """会话号 → brain channel。非法会话号一律归到 default (防注入/防乱建)。"""
    if conv and _BRAIN_CONV_RE.match(conv):
        return f"research_tab_{conv}"
    return "research_tab_default"


@router.post("/api/research/chat")
async def research_chat(body: dict):
    """对话大脑: {question, conv} → brain.claude_cli.ask_brain。

    多会话: conv 互相隔离 (各自 claude session, 可并行)。直接等待
    (单用户, timeout 180s), 大脑崩了返 success=False 不影响其他页。
    """
    from brain.claude_cli import ask_brain
    question = (body.get("question") or "").strip()
    if not question:
        return {"answer": "请输入问题", "success": False}
    try:
        return await ask_brain(question, timeout=300, max_turns=20,
                               channel=_brain_channel(body.get("conv") or ""))
    except Exception as e:  # 松耦合兜底: 大脑任何异常不炸 server
        logger.warning(f"对话大脑异常 (松耦合): {e}", exc_info=True)
        return {"answer": f"大脑调用失败: {e}", "success": False}


@router.post("/api/research/chat/stream")
async def research_chat_stream(body: dict):
    """对话大脑 SSE — asyncio.Queue 桥接 on_line 与 yield (v2 H-1)。
    0.5s 轮询 + task.done() 检测；客户端断开 cancel task (H-3)。"""
    import json as _json
    import asyncio as _aio
    from fastapi.responses import StreamingResponse
    from brain.claude_cli import ask_brain

    question = (body.get("question") or "").strip()
    if not question:
        return {"answer": "请输入问题", "success": False}

    async def generate():
        queue: _aio.Queue = _aio.Queue()
        async def on_line(line: str):
            await queue.put(line)
        task = _aio.create_task(ask_brain(
            question, timeout=480, max_turns=40,
            channel=_brain_channel(body.get("conv") or ""),
            on_line=on_line, archive=False))
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
            result = {"answer": f"大脑调用失败: {e}", "success": False}
        yield f"data: {_json.dumps({**result, 'type':'done'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/api/research/chat/reset")
async def research_chat_reset(body: dict):
    """重开某条会话 (清它的 claude session 记忆, 其他会话不动)。"""
    from brain import memory
    memory.reset_session(_brain_channel(body.get("conv") or ""))
    return {"success": True}
