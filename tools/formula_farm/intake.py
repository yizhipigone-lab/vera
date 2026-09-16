# -*- coding: utf-8 -*-
"""intake: 把 md 公式文章(标题/来源URL/代码块)解析成结构化 record。

归档格式与 gongshi 语料一致:
    # <标题>
    > 来源: <gupang url>
    > 归档时间: ...
    ```...源码...```
"""
import glob
import html
import os
import re

from tools.formula_farm import common


def parse_md(path):
    """解析单个 md -> record{file,title,url,archived,code,hash}。"""
    raw = common.read_text(path, enc="utf-8", errors="ignore")
    fn = os.path.basename(path)
    m = re.search(r"来源[:：]\s*(https?://[^\s>]+)", raw)
    url = (m.group(1).strip() if m else "")
    blocks = re.findall(r"```[^\n]*\n(.*?)```", raw, re.S)
    code = max(blocks, key=len).strip() if blocks else ""
    code = html.unescape(code)  # 网页实体还原(&NBSP; 等, gongshi 导入失败教训)
    return {
        "file": fn,
        "title": os.path.splitext(fn)[0],
        "url": url,
        "raw_len": len(raw),
        "code": code,
        "hash": common.code_hash(code) if code else "",
    }


def iter_md(gongshi_dir=None):
    """按文件名排序遍历语料(排序须与 gongshi _batch_import 的 sorted(glob) 一致)。"""
    d = gongshi_dir or common.GONGSHI_DIR
    paths = sorted(glob.glob(os.path.join(d, "*.md")))
    return [parse_md(p) for p in paths]


def iter_intake(intake_dir=None):
    """递归读 data/formula_farm/intake/<feed>/*.md(farm 自己的归档结构)。"""
    from tools.formula_farm import common as _c
    d = intake_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "data", "formula_farm", "intake")
    if not os.path.isdir(d):
        return []
    paths = sorted(glob.glob(os.path.join(d, "**", "*.md"), recursive=True))
    return [parse_md(p) for p in paths]
