"""brain/__main__.py — CLI: python -m brain "问题" [--session ID] [--fresh]。

--fresh: 忘掉上文开新对话（重置本 channel 的 session）。
"""
from __future__ import annotations

import argparse

from brain import memory
from brain.claude_cli import ask_brain_sync
from utils.sysutil import ensure_utf8_stdout


def main(argv: list[str] | None = None) -> int:
    # Windows GBK 控制台打印 LLM 回答里的 emoji/特殊字符会炸（端到端实测 \u23f1）
    ensure_utf8_stdout()
    ap = argparse.ArgumentParser(prog="python -m brain", description="VERA 对话大脑 CLI")
    ap.add_argument("question", help="自然语言问题")
    ap.add_argument("--session", default=None, help="指定 claude session id（多轮）")
    ap.add_argument("--channel", default="cli", help="会话通道（默认 cli）")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--max-turns", type=int, default=20)
    ap.add_argument("--fresh", action="store_true", help="重置 channel 对话记忆")
    args = ap.parse_args(argv)

    if args.fresh:
        memory.reset_session(args.channel)

    r = ask_brain_sync(args.question, session_id=args.session,
                       timeout=args.timeout, max_turns=args.max_turns,
                       channel=args.channel)
    print(r["answer"])
    for w in r["warnings"]:
        print(f"[warn] {w}")
    if r["low_confidence"]:
        print("[note] 本回答低置信（缺反证段或引用依据）")
    return 0 if r["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
