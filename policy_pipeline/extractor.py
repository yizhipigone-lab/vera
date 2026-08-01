"""policy_pipeline extractor — LLM 抽政策→行业 (P1a, 计划书 §6, NBER w33814 四步法)。

DeepSeek 在「通达信 128 行业清单」上做分类抽取 (不开放抽取, 防通用名≠行业名映射鸿沟).
→ {affected_industries:[{sector_code, name, direction, strength, evidence}], valid_from/to, policy_tools}.
sector_code 直接从清单选 → AFFECTS 边直连 A 层行业 (不用二次映射).
失败返 None (松耦合, 不影响选股交易).

公开接口 (≤8):
- extract_policy(title, text) → 结构化 dict | None
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from llm.providers import get_client
from utils.logger import get_logger

logger = get_logger(__name__)

_TDX_PRIORITY_PATH = Path(__file__).resolve().parent.parent / "policy_kb" / "tongdaxin_priority.json"


def _build_tdx_list_str() -> str:
    """加载通达信 128 行业清单 → 'code name' 每行一个 (注入 prompt 让 LLM 选 code)."""
    try:
        data = json.loads(_TDX_PRIORITY_PATH.read_text(encoding="utf-8"))
        lines = [f"{s['code']} {s.get('name', '')}" for s in data.get("sectors", []) if s.get("code")]
        return "\n".join(lines) if lines else "(清单为空)"
    except Exception as e:
        logger.warning(f"加载通达信清单失败: {e}")
        return "(清单加载失败, 请自由抽行业名)"


_EXTRACT_PROMPT = """你是政策-行业影响分析专家。读政策, 从下面通达信行业清单中选出受影响的行业 + 方向 + 力度 + 证据。

【通达信行业清单 (必须从中选 sector_code, 不要编造)】
{tdx_list}

【政策标题】{title}
【政策正文】
{text}

按以下 JSON schema 输出 (只输出 JSON, 不要任何其他文字):
{{
  "policy_id": "政策标识(发文字号或标题简写, 如 guofa_2020_2020)",
  "title": "{title}",
  "affected_industries": [
    {{
      "sector_code": "从上面清单选的 code (如 881xxx.SH, 必须清单里有的)",
      "industry_name": "对应清单的 name",
      "direction": "利好|利空|中性",
      "strength": "strong|medium|weak",
      "evidence_quote": "政策原文支撑句(50字内, 必须原文摘录不是概括)"
    }}
  ],
  "other_industries": [
    {{"name": "清单没覆盖但受影响的行业(自由文本, 不入图谱)", "direction": "利好|利空|中性", "evidence_quote": "..."}}
  ],
  "valid_from": "YYYY-MM-DD 或 null",
  "valid_to": "YYYY-MM-DD 或 null",
  "policy_tools": ["补贴/税收/标准/采购/准入 等, 没有留空数组"]
}}

铁律:
- sector_code 必须从清单选 (不编造 code); 清单没覆盖的放 other_industries
- Zero fabrication: evidence_quote 必须原文摘录
- 没明确影响的行业不列
- JSON 外不要任何文字"""


def extract_policy(title: str, text: str, client=None) -> Optional[dict]:
    """政策文本 → 结构化 dict (NBER 四步法, DeepSeek 在通达信清单上分类).

    返回: {policy_id, title, affected_industries:[{sector_code,...}], other_industries, valid_*, policy_tools}
    失败返 None (松耦合).
    """
    if not title or not text:
        return None
    c = client or get_client()
    prompt = _EXTRACT_PROMPT.format(
        tdx_list=_build_tdx_list_str(), title=title[:200], text=text[:2500]
    )
    resp = c.chat(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=2500,
        response_json=True,
    )
    if not resp:
        logger.warning(f"extract_policy DeepSeek 无响应(松耦合返None): {title[:50]}")
        return None
    try:
        result = json.loads(resp) if isinstance(resp, str) else resp
        if not isinstance(result, dict) or "affected_industries" not in result:
            logger.warning(f"extract_policy 响应格式异常: {str(resp)[:200]}")
            return None
        return result
    except json.JSONDecodeError as e:
        logger.warning(f"extract_policy JSON 解析失败: {e}; resp={str(resp)[:200]}")
        return None
