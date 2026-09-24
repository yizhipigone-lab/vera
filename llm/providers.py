"""llm providers — LLM 客户端公共层 (政策研究平台, 计划书 §3)。

DeepSeek (OpenAI 兼容) 为主, 自研薄壳 (不引 agno/langgraph 重型框架)。
借鉴 ValueCell ModelProvider 范式 (Apache2.0 思路, 不搬代码): 抽象 + 单例 + 松耦合。

公开接口 (≤8):
- get_client(provider) → LLMClient 单例 (默认 deepseek)
- LLMClient.chat(messages, model, ...) → response text | None  (失败返 None, 松耦合)

设计:
- OpenAI 兼容 /chat/completions 直调 (requests), 不依赖 openai sdk
- key/base_url/默认模型三级解析 (2026-09-06 AI 设置页):
  显式传参 > config/ai.json fast 段 > .env (DEEPSEEK_API_KEY) > 内置默认
  —— 用户在新页签配了 fast 档 (Key/地址/模型) 后, 快速档对话大脑 + 政策
  提取 extractor + 交易复盘 llm_review 共用同一份配置, 保存即热生效
  (每次 chat 现读 ai_config, 无缓存); 未配置时行为与原来完全一致。
- chat 失败 (HTTP 非200/网络/超时/无key) → 返 None + warning, 调用方兜底
"""
from __future__ import annotations

import os
from typing import Optional

import requests
from dotenv import load_dotenv

from utils.logger import get_logger

logger = get_logger(__name__)

load_dotenv()  # 读 .env (DEEPSEEK_API_KEY 等)

_DEEPSEEK_BASE = "https://api.deepseek.com"
# 2026-08-13 用户要求: 显式锁定 flash, 不用 deepseek-chat 别名
# (别名指向由官方侧控制, 可能漂移; /models 实测可用: deepseek-v4-flash / deepseek-v4-pro)
_DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"


def _fast_cfg() -> dict:
    """config/ai.json 的 fast 段 (AI 设置页签配置; 松耦合: 异常返 {})。"""
    try:
        from llm.ai_config import section
        return section("fast") or {}
    except Exception as e:
        logger.debug(f"ai_config.fast 读取失败(回落 .env 默认): {e}")
        return {}


class LLMClient:
    """LLM 客户端 (OpenAI 兼容 /chat/completions)。

    构造时只记显式参数, key/base_url/默认模型在每次 chat 现取
    (ai_config.fast → .env), 保证"保存即热生效、无需重启"。
    松耦合: chat 失败返 None, 调用方兜底 (不抛, 不影响选股交易)。
    """

    def __init__(self, provider: str = "deepseek", api_key: Optional[str] = None,
                 base_url: Optional[str] = None):
        self.provider = provider
        # 显式传入的 Key/地址永不覆盖 (构造期专用, 如测试/单发通道)
        self._fixed_key = api_key
        self._fixed_base = base_url

    def _resolve(self) -> tuple[str, str, str]:
        """每次调用现取 (显式 > ai_config.fast > .env > 内置默认)。

        返 (api_key, base_url, default_model); 不抛, 无 key 时 api_key=""。
        """
        cfg = _fast_cfg()
        api_key = (self._fixed_key or cfg.get("api_key")
                   or os.environ.get("DEEPSEEK_API_KEY", ""))
        base_url = (self._fixed_base or cfg.get("base_url")
                    or _DEEPSEEK_BASE).rstrip("/")
        default_model = cfg.get("model") or _DEEPSEEK_DEFAULT_MODEL
        return api_key, base_url, default_model

    def chat(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        timeout: int = 60,
        response_json: bool = False,
        **kw,
    ) -> Optional[str]:
        """OpenAI 兼容 chat → response text. 失败返 None (松耦合).

        response_json=True 时请求 JSON 输出 (DeepSeek 支持 response_format json_object;
        其他厂商端点若不支持, 会失败返 None — 调用方松耦合兜底, 页面有提示)。
        """
        api_key, base_url, default_model = self._resolve()
        if not api_key:
            logger.warning(f"LLM chat 跳过 ({self.provider} 无 api_key)")
            return None
        payload = {
            "model": model or default_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kw,
        }
        if response_json:
            payload["response_format"] = {"type": "json_object"}
        # 2026-08-21: DeepSeek 对相同请求输出不稳定 —— 模型可能"只思考不输出"
        # (reasoning_content 有内容, content 空), temperature 越低概率越高。
        # 策略: 最多重试 3 次拿 content; 全空则用 reasoning_content 兜底
        # (思考草稿也是完整复盘), 再空返回 None。调用方按失败跳过。
        rc_fallback: Optional[str] = None
        for attempt in range(3):
            try:
                r = requests.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json=payload,
                    timeout=timeout,
                )
            except Exception as e:
                logger.warning(f"LLM chat 失败(松耦合返None): {type(e).__name__}: {e}")
                return None
            if r.status_code != 200:
                logger.warning(f"LLM chat HTTP {r.status_code}: {r.text[:200]}")
                return None
            # 2026-09-16 审计 S3 修复: 响应解析包进 try — HTTP 200 但结构异常
            # (代理网关返回非 OpenAI 形状) 时原实现在 try 外异常直接穿透调用方。
            try:
                msg = r.json()["choices"][0]["message"]
            except Exception as e:
                logger.warning(f"LLM chat 失败(松耦合返None): {type(e).__name__}: {e}")
                return None
            content = msg.get("content") or ""
            if content:
                return content
            rc = msg.get("reasoning_content") or ""
            if rc and not rc_fallback:
                rc_fallback = rc
            logger.warning(
                f"LLM chat 第 {attempt + 1} 次返回空 content (仅 reasoning_content), 重试")
        logger.warning("LLM chat 3 次均空 content, 用 reasoning_content 兜底")
        return rc_fallback


_client: Optional[LLMClient] = None


def get_client(provider: str = "deepseek") -> LLMClient:
    """单例 LLMClient (默认 DeepSeek; 配置热生效在 chat 内现取, 单例无碍)。"""
    global _client
    if _client is None or _client.provider != provider:
        _client = LLMClient(provider=provider)
    return _client


if __name__ == "__main__":
    # python -m llm.providers  → 测 DeepSeek 连通
    c = get_client()
    r = c.chat([{"role": "user", "content": "只回5个字: providers OK"}])
    print(f"[OK] chat 回复: {r}")
