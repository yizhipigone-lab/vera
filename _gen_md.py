"""Generate Markdown scan report (split version, robust rendering).

Reads `_gs_scan_out/all_with_hits.csv` produced by `_scan_gs_txt.py`,
emits a Markdown report split into:
- 合格 / gs_0_ and gs_1_ sub-tables (smaller rows render reliably)
- 排除 / bucketed by primary forbidden category
"""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Iterable, Mapping

OUT = Path(r"e:/1target/VERA/_gs_scan_out/scan_report.md")
CSV_PATH = Path(r"e:/1target/VERA/_gs_scan_out/all_with_hits.csv")

# Forbidden-function family → set of TDX function names
FUTURE_FUNCS: set[str] = {
    "ZIG", "PEAK", "PEAKBARS", "TROUGH", "TROUGHBARS",
    "TROUGHF", "BACKSET", "REFX", "FFT",
}
DCLOSE_FUNCS: set[str] = {"DCLOSE", "DOPEN", "DHIGH", "DLOW", "DVOL"}
ZXNH_FUNCS: set[str] = {"ZXNH"}
DRAW_FUNCS: set[str] = {
    "DRAWKLINE", "DRAWLINE", "DRAWICON", "DRAWTEXT", "DRAWNULL",
    "DRAWNUMBER", "DRAWBAND", "DRAWFLOAT",
    "STICKLINE", "PARTLINE", "POLYLINE",
}
CAT_ORDER: tuple[str, ...] = ("未来函数", "DCLOSE", "ZXNH", "绘图")


def category_of(func_name: str) -> str:
    if func_name in FUTURE_FUNCS:
        return "未来函数"
    if func_name in DCLOSE_FUNCS:
        return "DCLOSE"
    if func_name in ZXNH_FUNCS:
        return "ZXNH"
    if func_name in DRAW_FUNCS:
        return "绘图"
    return "?"


def render_table(rows: Iterable[Mapping[str, str]],
                 label2key: Mapping[str, str | None]) -> list[str]:
    """Build Markdown table lines.

    `label2key`: column display-name → row dict key (None = 1-based index).
    """
    cols = list(label2key.keys())
    out: list[str] = ["| " + " | ".join(cols) + " |"]
    out.append("|" + "|".join(["---" for _ in cols]) + "|")
    for i, r in enumerate(rows, 1):
        cells: list[str] = []
        for c in cols:
            k = label2key[c]
            cells.append(str(i) if k is None else str(r.get(k or "", "")))
        out.append("| " + " | ".join(cells) + " |")
    return out


def split_passed(passed: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    g0 = sorted(
        [r for r in passed if r["file"].startswith("gs_0_")],
        key=lambda r: r["file"],
    )
    g1 = sorted(
        [r for r in passed if r["file"].startswith("gs_1_")],
        key=lambda r: r["file"],
    )
    covered = {r["file"] for r in g0} | {r["file"] for r in g1}
    other = [r for r in passed if r["file"] not in covered]
    return g0, g1, other


def main() -> None:
    rows = sorted(
        csv.DictReader(open(CSV_PATH, encoding="utf-8-sig")),
        key=lambda r: r["file"],
    )
    total = len(rows)
    passed = [r for r in rows if r["passed"] == "True"]
    failed = [r for r in rows if r["passed"] == "False"]
    passed_g0, passed_g1, passed_other = split_passed(passed)

    # Build per-func hit counts
    stats: Counter[str] = Counter()
    for r in rows:
        for h in r.get("all_hits", "").split(","):
            if h:
                stats[h] += 1

    # Failures bucketed by PRIMARY category (first hit's category wins)
    fail_buckets: dict[str, list[dict]] = {c: [] for c in CAT_ORDER}
    for r in failed:
        hits = r["all_hits"].split(",")
        primary = category_of(hits[0])
        fail_buckets.setdefault(primary, []).append(r)

    # Emit report (newline=LF, utf-8 no BOM)
    with OUT.open("w", encoding="utf-8", newline="\n") as f:
        f.write("# gs_txt 公式扫描报告 (2026-07-07)\n\n")
        f.write(f"- 扫描目录: `E:/NEW_TDX/T0001/export/gs_txt`\n")
        f.write(f"- 扫描脚本: `e:/1target/VERA/_scan_gs_txt.py`\n\n")

        f.write("## 总览\n\n")
        f.write("| 项目 | 数值 |\n|---|---:|\n")
        f.write(f"| 公式总数 | {total} |\n")
        f.write(f"| 合格 (不含任何禁用函数) | {len(passed)} "
                f"({len(passed)/total*100:.1f}%) |\n")
        f.write(f"| 被排除 (含至少 1 类禁用) | {len(failed)} "
                f"({len(failed)/total*100:.1f}%) |\n\n")

        f.write("## 禁用函数命中统计\n\n")
        f.write("| 函数 | 命中文件数 | 类别 |\n|---|---:|---|\n")
        for h, c in sorted(stats.items(), key=lambda x: (-x[1], x[0])):
            f.write(f"| `{h}` | {c} | {category_of(h)} |\n")
        f.write("\n")

        f.write("## 禁用函数清单\n\n")
        f.write("- 未来函数: ZIG, PEAK, PEAKBARS, TROUGH, TROUGHBARS, "
                "TROUGHF, BACKSET, REFX, FFT\n")
        f.write("- DCLOSE 系列: DCLOSE, DOPEN, DHIGH, DLOW, DVOL\n")
        f.write("- ZXNH 系列: ZXNH\n")
        f.write("- 绘图函数: DRAWKLINE, DRAWLINE, DRAWICON, DRAWTEXT, "
                "DRAWNULL, DRAWNUMBER, DRAWBAND, DRAWFLOAT, STICKLINE, "
                "PARTLINE, POLYLINE\n\n")

        # 合格清单
        f.write(f"## 合格公式清单 (共 {len(passed)} 个)\n\n")
        f.write(f"### 副图/指标类 gs_0_ ({len(passed_g0)} 个)\n\n")
        f.write("\n".join(render_table(
            passed_g0, {"#": None, "文件名": "file"})) + "\n\n")
        f.write(f"### 选股条件类 gs_1_ ({len(passed_g1)} 个)\n\n")
        f.write("\n".join(render_table(
            passed_g1, {"#": None, "文件名": "file"})) + "\n\n")
        if passed_other:
            f.write(f"### 其他 ({len(passed_other)} 个)\n\n")
            f.write("\n".join(render_table(
                passed_other, {"#": None, "文件名": "file"})) + "\n\n")

        # 排除清单
        f.write(f"## 被排除公式清单 (共 {len(failed)} 个)\n\n")
        f.write("每个公式归到「第一个命中」所属类别。一公式若含多类禁用函数,"
                "仅在主类别段列出,完整命中见 CSV。\n\n")
        for cat in CAT_ORDER:
            bucket = fail_buckets.get(cat, [])
            if not bucket:
                continue
            f.write(f"### {cat} ({len(bucket)} 个)\n\n")
            f.write("\n".join(render_table(
                bucket,
                {"#": None, "文件名": "file", "命中禁用函数": "all_hits"}))
                + "\n\n")


if __name__ == "__main__":
    main()
    # 用 pathlib.stat 报告
    size = OUT.stat().st_size
    print(f"MD: {OUT} ({size} bytes)")
