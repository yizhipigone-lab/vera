"""llm providers — LLM 客户端公共层 (政策研究平台, 计划书 §3)。

DeepSeek (OpenAI 兼容) 为主, 自研薄壳 (不引 agno/langgraph 重型框架)。
借鉴 ValueCell ModelProvider 范式 (Apache2.0 思路, 不搬代码): 抽象 + 单例 + 松耦合。

公开接口 (≤8):
- get_client(provider) → LLMClient 单例 (默认 deepseek)
- LLMClient.chat(messages, model, ...) → response text | None  (失败返 None, 松耦合)

设计:
- DeepSeek 用 requests 直调 (OpenAI 兼容 /chat/completions), 不依赖 openai sdk
- key 从 .env 读 (DEEPSEEK_API_KEY), 不硬编码
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


class LLMClient:
    """LLM 客户端 (DeepSeek OpenAI 兼容为主)。

    松耦合: chat 失败返 None, 调用方兜底 (不抛, 不影响选股交易)。
    """

    def __init__(self, provider: str = "deepseek", api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.provider = provider
        if provider == "deepseek":
            self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
            self.base_url = (base_url or _DEEPSEEK_BASE).rstrip("/")
        else:
            # P1 先只接 DeepSeek; Claude/Qwen/Ollama 后续扩展 (计划书 §3 注册表)
            raise ValueError(f"未支持 provider: {provider} (P1 先只接 deepseek)")

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

        response_json=True 时请求 JSON 输出 (DeepSeek 支持 response_format json_object).
        """
        if not self.api_key:
            logger.warning(f"LLM chat 跳过 ({self.provider} 无 api_key)")
            return None
        payload = {
            "model": model or _DEEPSEEK_DEFAULT_MODEL,
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
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}",
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
            msg = r.json()["choices"][0]["message"]
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
    """单例 LLMClient (默认 DeepSeek)."""
    global _client
    if _client is None or _client.provider != provider:
        _client = LLMClient(provider=provider)
    return _client


if __name__ == "__main__":
    # python -m llm.providers  → 测 DeepSeek 连通
    c = get_client()
    r = c.chat([{"role": "user", "content": "只回5个字: providers OK"}])
    print(f"[OK] chat 回复: {r}")
