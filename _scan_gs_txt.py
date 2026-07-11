"""扫描 gs_txt 目录公式文件,标记 4 类禁用函数。

判定规则:
- 未来函数: ZIG, PEAK, PEAKBARS, TROUGH, TROUGHBARS, TROUGHF, FFTX(?),
            BACKSET, REFX (REFX 是相对引用未来数据, 也是未来函数)
- DCLOSE 系列: DCLOSE, DOPEN, DHIGH, DLOW, DVOL
- ZXNH
- 绘图函数: DRAWICON, DRAWTEXT, DRAWNULL, STICKLINE, PARTLINE, POLYLINE

匹配方式: 按行匹配 ASCII 关键字, 用 \\b 单词边界避免 PEAKCLASS 这种假阳性。
编码: 文件不是 UTF-8 (GBK/GB18030) 但函数名都是 ASCII, 直接读字节按 utf-8 解
       解不出来的字节用 'ignore' 跳过, 只搜 ASCII。
"""
import os
import re
import csv
from collections import Counter
from pathlib import Path

DIR = Path(r"E:/NEW_TDX/T0001/export/gs_txt")

# 分类清单
FUTURE_FUNCS = ["ZIG", "PEAK", "PEAKBARS", "TROUGH", "TROUGHBARS",
                "TROUGHF", "BACKSET", "REFX"]
# FFTX 在通达信里有, 但拼法是 FFT (快速傅里叶). TDX 也有 FFT, 也算未来函数
# 实际验: 通达信有 FFT() 和 FFTX(), 但极少见, 先加上
FUTURE_FUNCS += ["FFT"]
FUTURE_FUNCS = list(set(FUTURE_FUNCS))

DCLOSE_FUNCS = ["DCLOSE", "DOPEN", "DHIGH", "DLOW", "DVOL"]
ZXNH_FUNCS = ["ZXNH"]
# 绘图/Draw 系列 完整名单 (TDX 全 16 个绘图函数 + IF/COLOR* 装饰不算)
# 输出型(选股脚本不需要): DRAWKLINE, DRAWLINE, DRAWICON, DRAWTEXT,
#   DRAWNULL, DRAWNUMBER, DRAWBAND, DRAWFLOAT
# 线型/段型: STICKLINE, PARTLINE, POLYLINE
DRAW_FUNCS = ["DRAWKLINE", "DRAWLINE", "DRAWICON", "DRAWTEXT",
              "DRAWNULL", "DRAWNUMBER", "DRAWBAND", "DRAWFLOAT",
              "STICKLINE", "PARTLINE", "POLYLINE"]


def match_funcs(text: str, names):
    """严格匹配 TDX 函数: 全大写, \\b 单词边界 + 后不接字母数字/下划线."""
    hits = []
    for n in names:
        # 大小写不敏感 (TDX 函数名都大写, 但保险起见开 IGNORECASE)
        pat = re.compile(rf"(?<!\w){re.escape(n)}(?!\w)", re.IGNORECASE)
        if pat.search(text):
            hits.append(n)
    return hits


def decode_source(raw: bytes):
    """TDX 文件多是 GBK; 失败回 utf-8 sig + replace."""
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_source(text: str):
    """从 GS-TXT 格式中找出 'Source Code:' 之后到文件末尾的代码段."""
    idx = text.find("Source Code:")
    if idx == -1:
        return text  # 无 marker, 全文算
    return text[idx + len("Source Code:"):]


def main():
    files = sorted(DIR.glob("gs_*.txt"))
    print(f"找到 {len(files)} 个文件")

    rows = []
    stats = Counter()
    stat_pass = Counter()
    no_source = []

    for fp in files:
        raw = fp.read_bytes()
        text = decode_source(raw)
        src = extract_source(text)

        fut = match_funcs(src, FUTURE_FUNCS)
        dcl = match_funcs(src, DCLOSE_FUNCS)
        zxh = match_funcs(src, ZXNH_FUNCS)
        drw = match_funcs(src, DRAW_FUNCS)

        all_hits = fut + dcl + zxh + drw
        passed = not all_hits
        if passed:
            stat_pass["PASS"] += 1
        for h in all_hits:
            stats[h] += 1

        if "Source Code:" not in text:
            no_source.append(fp.name)

        rows.append({
            "file": fp.name,
            "passed": passed,
            "future": ",".join(fut),
            "dclose": ",".join(dcl),
            "zxnh": ",".join(zxh),
            "draw": ",".join(drw),
            "all_hits": ",".join(all_hits),
        })

    # 输出
    out_dir = Path("e:/1target/VERA/_gs_scan_out")
    out_dir.mkdir(exist_ok=True)

    # 全量 CSV
    csv_path = out_dir / "all_with_hits.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["file", "passed", "future",
                                          "dclose", "zxnh", "draw", "all_hits"])
        w.writeheader()
        w.writerows(rows)

    # Markdown 报告
    md_path = out_dir / "scan_report.md"
    passed = [r for r in rows if r["passed"]]
    failed = [r for r in rows if not r["passed"]]

    with md_path.open("w", encoding="utf-8") as f:
        f.write("# gs_txt 公式扫描报告\n\n")
        f.write(f"- 总公式数: **{len(files)}**\n")
        f.write(f"- 合格(无任何禁用函数): **{len(passed)}** "
                f"({len(passed)/len(files)*100:.1f}%)\n")
        f.write(f"- 含禁用函数被排除: **{len(failed)}** "
                f"({len(failed)/len(files)*100:.1f}%)\n")
        if no_source:
            f.write(f"- ⚠️ 无 'Source Code:' marker 文件: **{len(no_source)}**\n")
        f.write("\n## 禁用函数命中统计\n\n")
        f.write("| 函数 | 出现文件数 | 类别 |\n")
        f.write("|---|---:|---|\n")

        cat = {**{h: "未来函数" for h in FUTURE_FUNCS},
               **{h: "DCLOSE 系列" for h in DCLOSE_FUNCS},
               **{h: "ZXNH 系列" for h in ZXNH_FUNCS},
               **{h: "绘图函数" for h in DRAW_FUNCS}}
        for h, c in sorted(stats.items(), key=lambda x: -x[1]):
            f.write(f"| `{h}` | {c} | {cat.get(h, '?')} |\n")

        # 完整逐文件表
        f.write("\n## 全部文件清单 (按命中状态排序)\n\n")
        f.write("| # | 文件 | 是否合格 | 命中禁用函数 |\n")
        f.write("|---:|---|---|---|\n")
        for i, r in enumerate(rows, 1):
            ok = "✅" if r["passed"] else "❌"
            hits = r["all_hits"] or "—"
            f.write(f"| {i} | `{r['file']}` | {ok} | {hits} |\n")

    print(f"合格: {len(passed)}/{len(files)}")
    print(f"CSV: {csv_path}")
    print(f"MD:  {md_path}")
    print(f"\n禁用函数命中 TOP:")
    for h, c in stats.most_common(20):
        print(f"  {h:12} {c}")


if __name__ == "__main__":
    main()
