"""`python -m obsidian` 入口 — 等价于 `python -m obsidian.export`。"""
from __future__ import annotations

from obsidian.export import main

if __name__ == "__main__":
    raise SystemExit(main())
