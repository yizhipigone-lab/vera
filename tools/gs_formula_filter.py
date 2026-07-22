"""
gs_txt 公式过滤 — 冒烟前置 (2026-07-18)

扫 gs_txt 下所有 gs_1*.txt, 按用户规则排除:
  - POLYLINE 画图函数 (用户明确: "划线只排除POLYLINE")
  - 未来函数 ZIG/PEAK/TROUGH/BACKSET/REFX (前视偏差, 用户上一条要求)
其他画图函数 (DRAWICON/DRAWTEXT/DRAWLINE/STICKLINE) 不排除 (用户: "其他无所谓").

输出:
  output/gs_filter/report.json  — 每公式 status + reason + 公式名
  stdout                         — 分类统计 (ASCII, 避免 GBK 终端乱码)

用法:
  python tools/gs_formula_filter.py
"""
import os
import re
import json

GS_DIR = r"E:\NEW_TDX\T0001\export\gs_txt"
OUT_DIR = os.path.join("output", "gs_filter")

# 画图函数: 仅 POLYLINE (用户明确)
DRAW_FUNCS = ["POLYLINE"]

# 未来函数 (前视): 转折点需未来确认 / 直接引用未来数据. 含变体 (前缀匹配防漏)
# 2026-07-20: 修词边界漏变体 (钱龙-长预警 PEAKBARS 之前 \bPEAK\b 漏)
# 2026-07-20: 加 D 系列不定周期函数 (用户要求排除; 实测达标6全用DCLOSE, 排除后达标=0)
# 2026-07-20: 网上核实扩展权威清单 (ZIGA/FLATZIG/PEAKA/TROUGHA/REFXV/BARSNEXT/XMA/FFT/DRAWLINE/#周期)
FUTURE_FUNCS = ["ZIG", "ZIGA", "ZIGBARS", "FLATZIG", "FLATZIGA",
                "PEAK", "PEAKA", "PEAKBARS", "PEAKBARSA",
                "TROUGH", "TROUGHA", "TROUGHBARS",
                "BACKSET", "REFX", "REFXV", "REFXR", "BARSNEXT",
                "DCLOSE", "DHIGH", "DLOW", "DOPEN", "DVOL",
                "DRAWLINE", "XMA", "FFT"]
# 跨周期引用 (盘中大周期数据会变, 隐前视). 字符串匹配 (# 前缀 \b 不匹配)
CROSS_FUNCS = ["#MONTH", "#WEEK", "#DAY"]


def read_source(path: str) -> str:
    """容错解码 (utf-8 失败回退 gbk/gb18030). 返回 Source Code: 之后的源码段."""
    with open(path, "rb") as f:
        raw = f.read()
    text = None
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="ignore")
    # 只扫 Source Code: 之后, 避开元信息行误判
    if "Source Code:" in text:
        text = text.split("Source Code:", 1)[1]
    return text


def scan(text: str):
    """返回 (has_polyline: bool, future_hits: list[str]). 词边界匹配防变量名误伤."""
    has_poly = any(re.search(r"\b" + f + r"\b", text, re.IGNORECASE) for f in DRAW_FUNCS)
    future_hits = [f for f in FUTURE_FUNCS
                   if re.search(r"\b" + f + r"\b", text, re.IGNORECASE)]
    future_hits += [f for f in CROSS_FUNCS if f in text.upper()]  # 跨周期 (# 前缀)
    return has_poly, future_hits


def formula_name(filename: str) -> str:
    """gs_1_<name>.txt -> <name> (保留原样含空格/符号, 交 TDX 解析)."""
    n = filename
    if n.startswith("gs_1_"):
        n = n[len("gs_1_"):]
    if n.endswith(".txt"):
        n = n[:-4]
    return n


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    files = sorted(f for f in os.listdir(GS_DIR)
                   if f.startswith("gs_1") and f.endswith(".txt"))
    report = {
        "total": len(files),
        "ok": [], "excluded_polyline": [], "excluded_future": [],
        "excluded_both": [], "errors": [],
    }
    for fn in files:
        path = os.path.join(GS_DIR, fn)
        try:
            src = read_source(path)
            has_poly, future_hits = scan(src)
            name = formula_name(fn)
            if has_poly and future_hits:
                report["excluded_both"].append({"file": fn, "name": name,
                                                "reason": "polyline+" + ",".join(future_hits)})
            elif has_poly:
                report["excluded_polyline"].append({"file": fn, "name": name,
                                                    "reason": "polyline"})
            elif future_hits:
                report["excluded_future"].append({"file": fn, "name": name,
                                                  "reason": ",".join(future_hits)})
            else:
                report["ok"].append({"file": fn, "name": name})
        except Exception as e:
            report["errors"].append({"file": fn, "err": str(e)[:80]})

    with open(os.path.join(OUT_DIR, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"[OK] total={report['total']} pass={len(report['ok'])} "
          f"excl_polyline={len(report['excluded_polyline'])} "
          f"excl_future={len(report['excluded_future'])} "
          f"excl_both={len(report['excluded_both'])} "
          f"err={len(report['errors'])}")
    print(f"[OUT] {os.path.join(OUT_DIR, 'report.json')}")


if __name__ == "__main__":
    main()
