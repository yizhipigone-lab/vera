# -*- coding: utf-8 -*-
"""dedupe: '只做没做过的' — 双键去重 (URL + 归一化源码哈希)。

已做集合 = gongshi 语料来源URL ∪ 语料源码哈希 ∪ gs_txt 全库源码哈希 ∪ intake历史。
"""
import glob
import os
import re

from tools.formula_farm import common
from tools.formula_farm import intake


def _gs_txt_code(path):
    """gs_txt 官方导出: 取 'Source Code:' 之后的源码(GBK 解码)。"""
    txt = common.read_text(path, enc="gb18030", errors="ignore")
    i = txt.find("Source Code:")
    code = txt[i + len("Source Code:"):] if i >= 0 else txt
    return code.strip()


def build_done_set(gongshi_dir=None, gs_txt_dir=None):
    """返回 {urls:set, hashes:set, parse_failures:int} — 已做过的判据。

    F8 (2026-09-16): 单文件解析失败不再静默 continue —— 逐条 warning
    (文件名+异常) 并计数; 失败 >0 时末尾明确提示「N 个文件解析失败,
    判重可能漏」(失败文件没进已做集, 已做过的公式可能被判"新"重复入库)。
    """
    urls, hashes = set(), set()
    n_fail = 0
    for rec in intake.iter_md(gongshi_dir):
        if rec["url"]:
            urls.add(rec["url"])
        if rec["hash"]:
            hashes.add(rec["hash"])
    d = gs_txt_dir or common.GS_TXT_DIR
    if os.path.isdir(d):
        for p in glob.glob(os.path.join(d, "*.txt")):
            try:
                h = common.code_hash(_gs_txt_code(p))
                if h:
                    hashes.add(h)
            except Exception as e:                               # noqa: BLE001
                n_fail += 1
                print("[dedupe][WARN] 解析失败 %s: %s: %s — 该文件未进已做集"
                      % (os.path.basename(p), type(e).__name__, e), flush=True)
    if n_fail:
        print("[dedupe][WARN] %d 个文件解析失败, 判重可能漏" % n_fail, flush=True)
    return {"urls": urls, "hashes": hashes, "parse_failures": n_fail}


def is_dup(rec, done_set):
    """返回命中的判据键列表(空 = 全新公式)。"""
    hits = []
    if rec.get("url") and rec["url"] in done_set["urls"]:
        hits.append("url")
    if rec.get("hash") and rec["hash"] in done_set["hashes"]:
        hits.append("hash")
    return hits
