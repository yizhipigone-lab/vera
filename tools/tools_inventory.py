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
from pathlib import Path

#: 生产代码目录 (引用 = "被当库用/被程序拉起")
#: 2026-09-20 审计 P2-9: 补 `tools` 自身 —— 工具链互相拉起 (或经 .bat 批量调度)
#: 也是生产引用, 原判据把它们误判成孤儿。
PROD_DIRS = ("backtest", "brain", "core", "evolution", "llm", "notes_gen",
             "pipeline", "policy_pipeline", "qmt", "report", "scheduler",
             "selection", "trade", "utils", "tests", "tools")

#: 文档目录 (引用 = 研究证据链)
DOC_DIRS = ("docs", "research", "notes")

#: 扫描引用时排除的目录 (运行时产物/缓存, 不是引用来源)
SKIP_PARTS = {".git", "__pycache__", "node_modules", "data", "output",
              "logs", "scratch", ".venv", "vera_obs_vault", "obsidian"}

#: 引用来源文件的扩展名 (生产侧: 代码/脚本/配置)
PROD_PATTERNS = ("*.py", "*.md", "*.bat", "*.js", "*.mjs", "*.html", "*.yaml",
                 "*.yml", "*.json", "*.txt")

#: 根级散文件的归属: **生产**引用只认代码/脚本/配置, **根级 .md 一律只算文档**
#: (2026-09-20 审计 P2-9: 原先根级 .md 混进生产侧, 于是 CHANGELOG 里提一句就把
#: 脚本"洗白"成生产引用 —— quantqq_5m_sweep_2010 就是这么被抬进去的)。
PROD_ROOT_SUFFIXES = (".bat", ".py", ".yaml", ".yml")
DOC_ROOT_SUFFIXES = (".md",)

#: `.bat` 递归扫描专用的排除集: **只排真正的环境目录** —— .bat 是启动器
#: (不是运行时数据), 所以 `data/` 不在排除之列 (2026-09-20 审计 P2-9:
#: data/formula_farm/runs/*.bat 是批量调 tools/ 脚本的真引用来源)。
BAT_SKIP_PARTS = {".git", "__pycache__", "node_modules", ".venv"}


def _iter_ref_texts(root: Path, dirs, patterns=PROD_PATTERNS,
                    root_suffixes=PROD_ROOT_SUFFIXES, recursive_bat=True):
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
    # 顶层散文件
    for f in root.iterdir():
        if f.is_file() and f.suffix in root_suffixes:
            try:
                yield f, f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
    # 2026-09-20 审计 P2-9: `.bat` 要**递归**扫 —— 原先只扫仓库顶层, 于是
    # "被启动器拉起"的脚本全被误判成孤儿。
    if not recursive_bat:
        return
    for f in root.rglob("*.bat"):
        if any(p in BAT_SKIP_PARTS for p in f.parts):
            continue
        if f.parent == root:
            continue        # 顶层已在上面扫过
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
    doc_texts = list(_iter_ref_texts(root, DOC_DIRS,
                                     root_suffixes=DOC_ROOT_SUFFIXES,
                                     recursive_bat=False))

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
