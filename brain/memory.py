"""brain/memory.py — 多轮 session 管理 + 长期认知沉淀。

【单一记忆机制】对话历史只由 claude --resume 管理（CLI 侧持久化），
本模块绝不把历史拼进 prompt —— 双写会重复计费、上下文打架（计划书 v2
落地修正：§4.1 memory.py"历史拼 prompt"与 §5.1 --resume 矛盾，取 --resume）。

本模块只干两件事：
1. channel → claude session_id 映射持久化（研究 TAB / CLI / 定时任务各一路）。
2. 长期认知沉淀（月度笔记类的"认知"追加到 notes/cognition.md），
   只存认知/政策脉络，不存持仓数字/策略意图（计划书 L-1 记忆隐私）。
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path

from utils.logger import get_logger
from utils.sysutil import load_json_or_empty, project_root

logger = get_logger(__name__)

_ROOT = project_root()
SESSIONS_PATH = _ROOT / "data" / "brain_sessions.json"
COGNITION_PATH = _ROOT / "notes" / "cognition.md"


def get_or_create_session(channel: str = "default",
                          path: Path | None = None) -> str:
    """取 channel 的 claude session_id；没有则新建并持久化。"""
    sid, _ = prepare_session(channel, path)
    return sid


def prepare_session(channel: str = "default",
                    path: Path | None = None) -> tuple[str, bool]:
    """取/建 session，返回 (session_id, 是否已存在)。

    is_new 决定调用方用哪个 flag（实测 claude 2.x）：
    已存在 → --resume <sid>；新建 → --session-id <sid>。
    对不存在的 sid 用 --resume 会直接报错（"No conversation found"）。
    """
    path = path or SESSIONS_PATH
    data = load_json_or_empty(path)
    sid = data.get(channel)
    if sid:
        return sid, True
    sid = str(uuid.uuid4())  # 必须带连字符的标准格式: uuid4().hex 无横线,
    # claude 实测报 "Invalid session ID. Must be a valid UUID"（端到端首测抓出）
    data[channel] = sid
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    logger.info(f"brain session 新建: {channel} → {sid[:8]}…")
    return sid, False


def reset_session(channel: str = "default", path: Path | None = None) -> None:
    """清掉 channel 的 session（下次提问开新对话）。"""
    path = path or SESSIONS_PATH
    data = load_json_or_empty(path)
    if channel in data:
        del data[channel]
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                        encoding="utf-8")


def append_cognition(text: str, path: Path | None = None,
                     source: str = "") -> Path | None:
    """追加一条长期认知（月结/复盘结论），带日期与来源。失败返 None（松耦合）。

    纪律（L-1）：调用方保证 text 是认知/政策脉络，不含持仓数字/策略意图。
    """
    path = path or COGNITION_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        day = dt.datetime.now().strftime("%Y-%m-%d")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n## {day}" + (f"（{source}）" if source else "") + f"\n\n{text}\n")
        return path
    except Exception as e:
        logger.warning(f"append_cognition 失败（松耦合）: {e}")
        return None
