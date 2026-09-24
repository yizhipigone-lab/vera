# -*- coding: utf-8 -*-
"""brain/quick_chat.py — 秒回轻问答通道 (研究对话三态之「快速」档)。

动机 (2026-09-05 用户拍板): 研究 TAB 对话原来不管勾不勾「深度思考」都走
深度 agent (claude CLI / DSH), 端到端几十秒起。新增一档: 不套 agent、不调
工具, 直接 DeepSeek HTTP 单次成文 — 实测 1~3 秒。

与 ask_brain / run_dsh 同款返回契约 {answer, success, low_confidence,
warnings, session_id} — 路由层/前端零适配。

公开接口:
- quick_answer(question, history, on_line, channel) 纯知识秒回 (快速档唯一入口)

快速档边界 (2026-09-05 实测校正): 只答纯知识/轻问题 (1-3s)。个股诊断那种
"取数几十秒 + 八段报告"的重活不进快速档 — 实测把 fastpath 的 agent 式成文
指令喂给 DeepSeek 直答, 模型会输出"写作计划"而非成品。问个股/大盘请用
「标准」或「深度思考」档 (research_api._dispatch)。

关键坑 (实测): llm.providers.LLMClient 默认 temperature=0.0, DeepSeek
flash 在低温下会「只思考不输出」(reasoning_content 有、content 空), 重试
3 次仍空 → 9 秒空转。秒回通道必须显式 temperature=0.7。

松耦合: DeepSeek 挂了 / 无 key → 返 {"success": False, ...} 不抛, 调用方
可提示用户改用标准/深度档。不走 subprocess (无冷启动), 无 session (headless
单发), 无工具。

channel 归档 (2026-09-05): 本通道绕过 ask_brain 内部归档, 必须自补 vault
沉淀 (对话沉淀铁律 2026-07-28; 与 DSH 通道同款理由, 见 dsh_channel D12)。
传 channel 才归档; 脚本/测试不传不污染 vault。
"""
from __future__ import annotations

from utils.logger import get_logger

logger = get_logger(__name__)

_QUICK_SYSTEM = (
    "你是 VERA 量化研究助手的「快速问答」档。用户想要秒回。\n"
    "规则: ① 直接回答, 结论先行, 用大白话 (术语首次出现配一句通俗解释); "
    "② 不要反问澄清, 不要描述你的工作环境, 不要调用任何工具; "
    "③ 只凭你已有的知识回答 — 你没被提供实时行情/持仓数据, 涉及具体数字"
    "且不确定就直说『该数字需要实时数据, 建议切标准/深度档查证』; "
    "④ 回答控制在 150 字左右, 别写长篇; ⑤ 不构成投资建议。"
)


def _base_messages(question: str, history: list[dict] | None) -> list[dict]:
    """近几轮对话打包 (仅 user/assistant, 与 DSH 同口径) + 当前问题。"""
    msgs: list[dict] = [{"role": "system", "content": _QUICK_SYSTEM}]
    if history:
        for m in history[-6:]:
            role = m.get("role")
            if role in ("user", "assistant"):
                msgs.append({"role": role, "content": (m.get("content") or "")[:2000]})
    msgs.append({"role": "user", "content": question[:4000]})
    return msgs


async def _chat_once(question: str, history: list[dict] | None) -> str | None:
    """DeepSeek 单次直答 (线程内同步 HTTP + temp0.7 防空 content)。失败返 None。"""
    import asyncio

    from llm.providers import get_client  # 延迟: 依赖 requests/.env

    client = get_client()
    text = await asyncio.to_thread(
        client.chat, _base_messages(question, history),
        temperature=0.7, max_tokens=1500, timeout=60)
    return text


async def quick_answer(question: str, history: list[dict] | None = None,
                       on_line=None, channel: str | None = None) -> dict:
    """纯知识秒回 (快速档唯一入口): 不套 agent / 不调工具 / 不碰 claude。

    on_line 兼容参数: 秒回太快无进度事件, 调用方不用传 (保留签名一致性)。
    返 ask_brain 同款契约 {answer, success, low_confidence, warnings,
    session_id} — 路由层/前端零适配。
    """
    result = {"answer": "", "success": False, "low_confidence": False,
              "warnings": [], "session_id": None}
    try:
        text = await _chat_once(question, history)
    except Exception as e:
        logger.warning(f"秒回通道异常(松耦合): {type(e).__name__}: {e}", exc_info=True)
        result["answer"] = f"秒回通道调用失败: {type(e).__name__}: {e}"
        _archive(channel, question, result)
        return result
    if not text:
        result["answer"] = ("秒回通道无响应 (DeepSeek 可能未配 key 或限流)。"
                            "可改用「标准」或「深度思考」档提问。")
        result["low_confidence"] = True
        _archive(channel, question, result)
        return result
    result.update({"answer": text.strip(), "success": True})
    _archive(channel, question, result)
    return result


def _archive(channel: str | None, question: str, result: dict) -> None:
    """vault 对话沉淀 (松耦合, 自身不抛; 铁律 2026-07-28)。"""
    if not channel:
        return
    try:
        from brain.archive import archive_exchange
        archive_exchange(channel, question, result)
    except Exception as e:  # noqa: BLE001  归档失败不影响答题
        logger.warning(f"秒回通道 vault 归档失败(松耦合): {e}")
