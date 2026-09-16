# -*- coding: utf-8 -*-
"""static_vetting: 静态体检 — 复刻 gongshi 基线(select 448 / clean 397 / excluded 51)。

顺序(与 gongshi 一致):
  1) is_main: 主图类(MAIN_TOKENS / SKIP_NAMES) → 非纯选股, 移出 select;
  2) exclude_tokens: EXCLUDE_TOKENS(筹码 COST/WINNER/PPART + D 系列 DHIGH/DLOW) → 排除留痕;
  3) GS 编号 = select 列表内位置(1..448), 排除项占用编号(与基线 gs 值一致)。
L2 未来函数更严扫描只做信息报告。
"""
import os
import re

from tools.formula_farm import common
from tools.formula_farm import intake


def is_main(rec) -> bool:
    code = (rec.get("code") or "").upper()
    fn = rec.get("file") or ""
    if any(k in code for k in common.MAIN_TOKENS):
        return True
    return any(k in fn for k in common.SKIP_NAMES)


def exclude_tokens(rec):
    """命中 EXCLUDE_TOKENS 的清单(空=通过)。按 token 表顺序。"""
    code = (rec.get("code") or "").upper()
    return [t for t in common.EXCLUDE_TOKENS if t in code]


def build_clean(gongshi_dir=None):
    """跑完整基线流程, 返回 {total_md,total_select,clean_count,excluded_count,
    select(带gs), excluded, clean}。"""
    records = intake.iter_md(gongshi_dir)
    select = [r for r in records if not is_main(r)]
    for i, r in enumerate(select, start=1):
        r["gs"] = "GS%04d" % i
    excluded, clean = [], []
    for r in select:
        toks = exclude_tokens(r)
        if toks:
            excluded.append({"gs": r["gs"], "file": r["file"], "tokens": toks,
                             "url": r.get("url", "")})
        else:
            clean.append(r)
    return {
        "total_md": len(records),
        "total_select": len(select),
        "clean_count": len(clean),
        "excluded_count": len(excluded),
        "select": select,
        "excluded": excluded,
        "clean": clean,
    }


def l2_future_scan(clean_records):
    """信息报告: 干净集里若按 farm 更严黑名单还会命中哪些(不做硬闸)。"""
    hits = []
    for r in clean_records:
        code = (r.get("code") or "").upper()
        found = []
        for t in common.L2_FUTURE_TOKENS:
            # 词边界匹配防误伤(如 XMA 别命中 EXMA)
            if re.search(r"(?<![A-Z0-9_])" + re.escape(t) + r"(?![A-Z0-9_])", code):
                found.append(t)
        if found:
            hits.append({"gs": r["gs"], "file": r["file"], "tokens": found})
    return hits


# ---- v1 运行时体检(比 gongshi 基线更严: 未来函数/跨周期为硬闸) ----

FUTURE_TOKENS = [
    "BACKSET", "REFX", "REFXV", "REFXR", "BARSNEXT",
    "DCLOSE", "DOPEN", "DVOL", "DHIGH", "DLOW",
    "DRAWLINE", "POLYLINE", "XMA", "FFT",
    "ZIG", "ZIGA", "ZIGBARS", "FLATZIG",
    "PEAK", "PEAKA", "PEAKBARS", "TROUGH", "TROUGHA", "TROUGHBARS",
    "ZXNH",
]
CROSS_PERIOD = re.compile(r"#\s*(MONTH|WEEK|DAY|MINUTE|YEAR|SECOND)")


def vet_runtime(rec, _done=None):
    """每日上架前的运行时体检(硬闸)。返回 (ok, [原因...])。

    与 gongshi 基线的关系: 基线只剔主图类+筹码函数(397 净); 运行时再加:
    未来函数权威清单(DCLOSE/ZIG/... 当年 48% 虚高教训) + 跨周期 + 输出行检查。
    """
    code = (rec.get("code") or "")
    up = code.upper()
    reasons = []
    if is_main(rec):
        reasons.append("主图/绘图函数(非纯选股)")
    for t in common.EXCLUDE_TOKENS:
        if t in up:
            reasons.append("筹码/专有函数:%s" % t)
    for t in FUTURE_TOKENS:
        if re.search(r"(?<![A-Z0-9_])" + re.escape(t) + r"(?![A-Z0-9_])", up):
            reasons.append("未来函数:%s" % t)
            break  # 一类即可, 逐条见 l2
    if CROSS_PERIOD.search(up):
        reasons.append("跨周期引用(#MONTH 等)")
    if not code.strip():
        reasons.append("无源码")
    else:
        # 输出行 = 单冒号输出(NAME:expr;), ':=' 是赋值不算输出
        has_out = any(re.match(r"^[A-Za-z_一-龥][\w一-龥]*\s*:(?!=)", l.strip())
                      for l in code.splitlines() if l.strip())
        if not has_out:
            reasons.append("无输出行(选股公式需 NAME:expr;)")
    return (not reasons), reasons
