# -*- coding: utf-8 -*-
"""AI 设置 TAB 端点 /api/ai/* (2026-09-06, AI 设置独立页签)。

对话大脑三档 (快速/标准/深度) 的接入配置 (API Key/接入地址/模型) 界面后端。
存 config/ai.json (llm/ai_config.py 负责读写/打码), 热生效:
- 快速档 (fast): 走 llm.providers (OpenAI 兼容), 保存即被各调用方现读;
- 标准档 (standard): claude_cli spawn 时读配置注入 ANTHROPIC_* env;
- 深度档 (deep): dsh_channel spawn 前按配置改写 DSH settings 模型。

路由 (薄层, 逻辑在 llm/ai_config + llm/providers):
- GET  /api/ai/config   读当前配置, Key 一律打码 (绝不明文回传)
- POST /api/ai/save     保存三档配置 (Key 留空=保留旧值), 非法 shape → 400
- POST /api/ai/test     连通测试 {which: fast|standard, base_url, api_key,
                          model} → 真实最小请求, 不写盘
松耦合: 配置损坏/缺省回落原默认; 测试失败返 success=False 不抛。
"""
import requests

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from llm import ai_config
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter()

_SECTIONS = ("fast", "standard", "deep")


def _blank_view() -> dict:
    """空配置视图: 每档 key_mask=''/key_set=False (前端初始态)。"""
    return {s: {"key_mask": "", "key_set": False} for s in _SECTIONS}


def _view_of(cfg: dict) -> dict:
    """全量配置 → 前端视图 (Key 只给 mask + 是否有 Key 标记)。"""
    view = {}
    for s in _SECTIONS:
        sec = cfg.get(s) if isinstance(cfg, dict) else None
        sec = sec if isinstance(sec, dict) else {}
        if s == "deep":
            view[s] = {"provider": sec.get("provider", ""),
                       "model": sec.get("model", "")}
        else:
            key = sec.get("api_key", "")
            view[s] = {"base_url": sec.get("base_url", ""),
                       "model": sec.get("model", ""),
                       "key_set": bool(key),
                       "key_mask": ai_config.mask_key(key)}
    return view


@router.get("/api/ai/config")
async def ai_get_config():
    """读当前配置 (Key 打码回传, 绝不明文)。文件缺失/损坏 → 空视图。"""
    try:
        cfg = ai_config.load()
        if not isinstance(cfg, dict):
            cfg = {}
        return {"success": True, "config": _view_of(cfg)}
    except Exception as e:
        logger.warning(f"AI 配置读取异常: {e}", exc_info=True)
        return {"success": False, "error": str(e),
                "config": _blank_view()}


@router.post("/api/ai/save")
async def ai_save_config(body: dict):
    """保存三档配置: {fast?: {base_url,api_key,model}, standard?: {...},
    deep?: {provider,model}, 档内可带 __clear__:true 整档清空}。

    合并语义全在 ai_config.merge_patch (唯一实现, 防三份手写漂移):
    只更新出现的档; 字段非空覆盖、空串/None 保留旧值; __clear__ 整档置空。
    非法 shape (档不是 dict) → 400 不写盘。
    """
    try:
        if not isinstance(body, dict):
            return JSONResponse({"success": False, "error": "body 需为 JSON 对象"},
                                status_code=400)
        bad = [s for s in _SECTIONS
               if s in body and not isinstance(body[s], dict)]
        if bad:
            return JSONResponse(
                {"success": False,
                 "error": f"档 {bad} 需为对象 (fast/standard 含 base_url/api_key/model, deep 含 provider/model)"},
                status_code=400)

        old = ai_config.load() if isinstance(ai_config.load(), dict) else {}
        new = ai_config.merge_patch(old, body)   # 深模块: 合并语义单一实现
        ai_config.save(new)
        return {"success": True, "config": _view_of(new)}
    except Exception as e:
        logger.warning(f"AI 配置保存异常: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.post("/api/ai/test")
async def ai_test_connection(body: dict):
    """连通测试: {which: fast|standard, base_url, api_key, model} → 最小请求。

    fast: OpenAI 兼容 POST {base}/chat/completions
    standard: Anthropic 兼容 POST {base}/v1/messages
    只测不写盘; 失败返 success=False + 错误前 200 字 (人话摘要)。
    """
    try:
        which = (body.get("which") or "").strip().lower()
        base_url = (body.get("base_url") or "").strip().rstrip("/")
        api_key = (body.get("api_key") or "").strip()
        model = (body.get("model") or "").strip()
        if which not in ("fast", "standard"):
            return JSONResponse(
                {"success": False,
                 "error": "which 需为 fast 或 standard (deep 档走 DSH 适配器, 不在此测)"},
                status_code=400)
        if not base_url:
            return {"success": False, "error": "请填接入地址 (base_url)"}
        if not api_key:
            return {"success": False, "error": "请填 API Key"}
        if not model:
            return {"success": False, "error": "请填模型名"}

        if which == "fast":
            url = f"{base_url}/chat/completions"
            payload = {"model": model, "max_tokens": 4,
                       "messages": [{"role": "user", "content": "ping"}]}
            headers = {"Authorization": f"Bearer {api_key}",
                       "Content-Type": "application/json"}
        else:
            url = f"{base_url}/v1/messages"
            payload = {"model": model, "max_tokens": 4,
                       "messages": [{"role": "user", "content": "ping"}]}
            headers = {"Authorization": f"Bearer {api_key}",
                       "anthropic-version": "2023-06-01",
                       "Content-Type": "application/json"}

        r = requests.post(url, headers=headers, json=payload, timeout=20)
        if r.status_code != 200:
            detail = (r.text or "")[:200]
            logger.warning(f"AI 连通测试 HTTP {r.status_code}: {detail}")
            return {"success": False,
                    "message": f"连接失败 HTTP {r.status_code}"
                               + (f": {detail}" if detail else "")}
        return {"success": True, "message": f"{which} 连接成功 ({model})"}
    except Exception as e:
        logger.warning(f"AI 连通测试异常: {e}", exc_info=True)
        return {"success": False, "message": f"连接异常: {type(e).__name__}: {e}"}
