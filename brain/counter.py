"""brain/counter.py — 反证段强制校验（B 模式，不靠 prompt 自觉）。

判断类回答必须含 <counter_evidence>...</counter_evidence> 段（对立事实）。
缺失不拦截回答，而是追加低置信标记 —— 用户看得见，比静默放行诚实。

2026-08-12 意图分类（用户反馈：连 "test"、"/reload" 都被标低置信，太吵）：
调用方传入 question 时先做意图判断 —— 只有判断类问题（值不值得/该不该/
怎么看/预判…）才强制反证；查询/生成/命令类问题不强制，直接放行。
question 缺省 = 保持旧的严格行为（一律要求反证），eval 等老调用不受影响。
"""
from __future__ import annotations

import re

COUNTER_RE = re.compile(r"<counter_evidence>.*?</counter_evidence>", re.S)

LOW_CONFIDENCE_TAG = "\n\n> ⚠ 低置信：本回答缺少反证段（<counter_evidence>），结论可能片面。"

# 判断类问题的口语特征（覆盖大白话提问："要不要抄底""明天怎么看"）
_JUDGMENT_RE = re.compile(
    r"(值不值得|该不该|要不要|能不能|可以买|可以卖|哪些好|哪个好|怎么看|如何看"
    r"|预判|预测|形势|抄底|追涨|杀跌|止损吗|止盈吗|建议买|建议卖|值得"
    r"|风险评估|还能涨|还会跌|后市|仓位建议)")


def is_judgment_question(question: str) -> bool:
    """是否判断类问题（需要反证段的那种）。"""
    return bool(_JUDGMENT_RE.search(question or ""))


def has_counter_evidence(answer: str) -> bool:
    return bool(COUNTER_RE.search(answer or ""))


def ensure_counter_evidence(answer: str, question: str = "") -> tuple[str, bool]:
    """(标注后回答, 是否过关)。

    - 已含反证段 → 直接过关。
    - 传了 question 且不是判断类 → 不强制反证，放行（2026-08-12 意图分类）。
    - 判断类缺反证 → 追加低置信标记。
    """
    if has_counter_evidence(answer):
        return answer, True
    if question and not is_judgment_question(question):
        return answer, True
    return (answer or "") + LOW_CONFIDENCE_TAG, False
