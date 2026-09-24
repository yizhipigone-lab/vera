# -*- coding: utf-8 -*-
"""pack_tni: 生成通达信官方可导入的 GBK 明文 .tni 公式包。

模板 = gongshi all_468_formulas.tni 实物样本(字节级复刻语义):
    [FORMULA]\r\nName=GSxxxx\r\nType=XG\r\nDesc=<来源URL>\r\nParam=\r\n\r\n<源码>\r\n
段间以空行分隔; 全文件 GB18030 严格编码(编不进就报错留痕, 不静默替换)。
"""
import re


def build_pack(rows):
    """rows: [{gs, url, code}, ...] -> bytes(GB18030)。Type 固定 XG(选股)。"""
    parts = [
        "; VERA formula farm pack (公式农场)",
        "; 导入方法: 通达信 → 公式管理器 → 导入 → 选择本文件",
        "",
    ]
    for r in rows:
        code = (r.get("code") or "").rstrip("\r\n")
        sec = (
            "[FORMULA]\r\n"
            "Name=%s\r\n"
            "Type=XG\r\n"
            "Desc=%s\r\n"
            "Param=\r\n"
            "\r\n"
            "%s\r\n"
        ) % (r["gs"], r.get("url", ""), code)
        parts.append(sec)
    text = "\r\n".join(parts) + "\r\n"
    try:
        return text.encode("gb18030")  # 严格: 编不进=该条公式有问题, 暴露给调用方留痕
    except UnicodeEncodeError as e:
        raise ValueError("GB18030 编码失败: %r (位置 %s) — 检查该条源码字符"
                         % (e.object[e.start:e.start + 1], e.start)) from e


def parse_pack(blob):
    """bytes -> [{name,type,desc,param,code}]。用于 round-trip 自校验。

    保留原始行尾(\r\n 或 \n), 保证源码逐字节还原。
    """
    text = blob.decode("gb18030", "ignore")
    chunks = text.split("[FORMULA]")
    secs = []
    for chunk in chunks[1:]:
        chunk = chunk.strip("\r\n")
        if not chunk:
            continue
        sep = "\r\n" if "\r\n" in chunk else "\n"
        lines = chunk.split(sep)
        meta, i = {}, 0
        while i < len(lines) and lines[i].strip():
            k, _, v = lines[i].partition("=")
            meta[k.strip()] = v.strip()
            i += 1
        code = sep.join(lines[i + 1:]).rstrip("\r\n")
        secs.append({
            "name": meta.get("Name", ""),
            "type": meta.get("Type", ""),
            "desc": meta.get("Desc", ""),
            "param": meta.get("Param", ""),
            "code": code,
        })
    return secs
