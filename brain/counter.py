"""brain/counter.py — 反证段强制校验（B 模式，不靠 prompt 自觉）。

判断类回答必须含 <counter_evidence>...</counter_evidence> 段（对立事实）。
缺失不拦截回答，而是追加低置信标记 —— 用户看得见，比静默放行诚实。
"""
from __future__ import annotations

import re

COUNTER_RE = re.compile(r"<counter_evidence>.*?</counter_evidence>", re.S)

LOW_CONFIDENCE_TAG = "\n\n> ⚠ 低置信：本回答缺少反证段（<counter_evidence>），结论可能片面。"


def has_counter_evidence(answer: str) -> bool:
    return bool(COUNTER_RE.search(answer or ""))


def ensure_counter_evidence(answer: str) -> tuple[str, bool]:
    """(标注后回答, 是否含反证)。缺失 → 追加低置信标记。"""
    if has_counter_evidence(answer):
        return answer, True
    return (answer or "") + LOW_CONFIDENCE_TAG, False
