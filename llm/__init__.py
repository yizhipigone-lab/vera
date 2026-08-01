"""llm — LLM 公共层 (政策研究平台, 计划书 §3)。

自研薄壳 (不引 agno/langgraph), 借鉴 ValueCell ModelProvider 范式 (Apache2.0 思路)。
DeepSeek (OpenAI 兼容) 为主; 后续扩展 Claude/Qwen/Ollama + fallback。

松耦合: LLM 挂/超时/无 key → chat 返 None, 调用方兜底, 选股交易零感知。
"""
