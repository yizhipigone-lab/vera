# -*- coding: utf-8 -*-
"""tools/tools_inventory.py — tools/ 脚本淤积清单 (2026-09-19 架构修订批次 3.4)。

背景: 架构审查实测 tools/ 130 个脚本里 **82 个零引用 (63%)** —— 多为一次性
研究脚本; 历史上已清过两批 (2026-07-13 清 35 个 / 2026-07-14 清 34 个),
又长回来了。但**不能一刀切删**: 很多脚本是研究报告的证据链 (被 docs/、
research/ 引用), 删了等于毁掉结论的可复核性。

本工具做的是"分级"不是"删除", 输出三级清单供人过目:
  ① production — 被生产代码 / 测试 / .bat 引用 (可能是入口或被当库用): **保留**
  ② docs_only  — 只被 docs/、research/、notes/ 引用: 研究证据链, **保留**
  ③ orphan     — 全仓零引用: 候选删除 (每季度跑一次, 人工过目后删)

用法 (5 分钟量级):
  python tools/tools_inventory.py            # 人看的清单
  python tools/tools_inventory.py --json     # 机器可读 (落 output/ 供比对)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: 生产代码目录 (引用 = "被当库用/被程序拉起")
PROD_DIRS = ("backtest", "brain", "core", "evolution", "llm", "notes_gen",
             "pipeline", "policy_pipeline", "qmt", "report", "scheduler",
             "selection", "trade", "utils", "tests")

#: 文档目录 (引用 = 研究证据链)
DOC_DIRS = ("docs", "research", "notes")

#: 扫描引用时排除的目录 (运行时产物/缓存, 不是引用来源)
SKIP_PARTS = {".git", "__pycache__", "node_modules", "data", "output",
              "logs", "scratch", ".venv", "vera_obs_vault", "obsidian"}


def _iter_ref_texts(root: Path, dirs, patterns=("*.py", "*.md", "*.bat", "*.js",
                                                "*.mjs", "*.html", "*.yaml",
                                                "*.yml", "*.json", "*.txt")):
    """产出 (文件路径, 文本) —— 只扫给定顶层目录, 跳过运行时产物。"""
    for d in dirs:
        base = root / d
        if not base.exists():
            continue
        for pat in patterns:
            for f in base.rglob(pat):
                if any(p in SKIP_PARTS for p in f.parts):
                    continue
                try:
                    yield f, f.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
    # 顶层散文件 (.bat/.md/.py)
    for f in root.iterdir():
        if f.is_file() and f.suffix in (".bat", ".md", ".py", ".yaml", ".yml"):
            try:
                yield f, f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue


def classify(root: Path) -> dict:
    """三级分类 tools/ 下的 .py 脚本 (纯函数, root 可注入以便测试)。"""
    tools_dir = root / "tools"
    scripts = sorted(p for p in tools_dir.rglob("*.py")
                     if p.name != "__init__.py")

    prod_texts = list(_iter_ref_texts(root, PROD_DIRS))
    doc_texts = list(_iter_ref_texts(root, DOC_DIRS))

    levels: dict[str, list[str]] = {"production": [], "docs_only": [], "orphan": []}
    for s in scripts:
        rel = str(s.relative_to(root)).replace("\\", "/")
        stem = s.stem
        # 引用判据: 文件名出现 (覆盖 `python tools/x.py`、`tools.x`、import 三种写法)
        in_prod = any(stem in t and f != s for f, t in prod_texts)
        if in_prod:
            levels["production"].append(rel)
            continue
        in_docs = any(stem in t and f != s for f, t in doc_texts)
        levels["docs_only"].append(rel) if in_docs else levels["orphan"].append(rel)
    return {"levels": levels,
            "counts": {k: len(v) for k, v in levels.items()},
            "total": len(scripts)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="tools/ 脚本三级分类清单 (不删任何文件)")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--root", default=None, help="项目根 (默认自动定位)")
    args = ap.parse_args(argv)

    root = Path(args.root) if args.root else Path(__file__).resolve().parents[1]
    res = classify(root)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    c = res["counts"]
    print(f"tools/ 脚本 {res['total']} 个: 被生产引用 {c['production']} / "
          f"仅文档引用(研究证据) {c['docs_only']} / 零引用候选删除 {c['orphan']}")
    for name, title in (("orphan", "零引用 (候选删除, 需人工过目)"),
                        ("docs_only", "仅文档引用 (研究证据链, 保留)")):
        print(f"\n── {title} ({c[name]}) ──")
        for rel in res["levels"][name]:
            print("  " + rel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
