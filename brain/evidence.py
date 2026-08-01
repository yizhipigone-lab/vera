"""brain/evidence.py — 引用溯源校验（每答附依据，缺失标低置信）。

"有依据"的启发式判定（宁可宽松放过，不搞复杂 NLP）：
- 反引号包裹的路径/代码（含 / 或 .py/.db/.md 等），或
- 出现"依据/来源/据 ... 显示"等引用词 + 数字，或
- <evidence> 段。
缺失不拦截，追加低置信标记（与 counter.py 同哲学）。
"""
from __future__ import annotations

import re

_BACKTICK_PATH_RE = re.compile(r"`[^`]*(?:/|\.py|\.db|\.md|\.json|\.yaml|SELECT)[^`]*`")
_CITATION_WORD_RE = re.compile(r"(依据|来源|引用|据.{0,20}显示|evidence)", re.I)
_NUMBER_RE = re.compile(r"\d")
_EVIDENCE_TAG_RE = re.compile(r"<evidence>.*?</evidence>", re.S)

LOW_CONFIDENCE_TAG = "\n\n> ⚠ 低置信：本回答未附引用依据（文件路径/查询/数字）。"


def has_citation(answer: str) -> bool:
    a = answer or ""
    if _BACKTICK_PATH_RE.search(a) or _EVIDENCE_TAG_RE.search(a):
        return True
    return bool(_CITATION_WORD_RE.search(a) and _NUMBER_RE.search(a))


def ensure_citation(answer: str) -> tuple[str, bool]:
    """(标注后回答, 是否含引用)。缺失 → 追加低置信标记。"""
    if has_citation(answer):
        return answer, True
    return (answer or "") + LOW_CONFIDENCE_TAG, False
