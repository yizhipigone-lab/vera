# -*- coding: utf-8 -*-
"""公式农场公共常量/工具 (v0)。

v0 纪律: 只增不改、只读 gongshi/TDX gs_txt、绝不写 TDX。
token 清单复刻 gongshi 实战(_batch_import.py + _clean_formulas.json 基线)。
"""
import hashlib
import os
import re

# ---- 路径 ----
GONGSHI_DIR = r"E:\1target\gongshi"          # 历史语料(只读)
GS_TXT_DIR = r"E:\NEW_TDX\T0001\export\gs_txt"  # TDX 导出(只读)
OUT_DIR = r"E:\1target\VERA\scratch\formula_farm_v0"

# ---- 主图类剔除(复刻 _batch_import.py MAIN_TOKENS) ----
MAIN_TOKENS = [
    "DRAWKLINE", "DRAWLINE", "DRAWBAND", "STICKLINE(",
    "POLYLINE", "DRAWGBK", "DRAWNUMBER",
]
# 文件名含以下关键词也判主图/跳过(复刻 _batch_import.py SKIP_NAMES)
SKIP_NAMES = [
    "多轨趋势线", "擒庄金龙", "职业操盘", "主力资金突破", "资金三部曲",
    "分时资金异动", "强承接吸筹", "主力机构共振", "金银满贯主图",
    "强势御龙", "潜伏神底", "潜龙筑底", "趋势主升", "麒麟定妖主图之火箭图标",
]

# ---- 专有/筹码类剔除(复刻 _clean_formulas.json 基线 token 全集) ----
EXCLUDE_TOKENS = ["COST(", "WINNER(", "PPART", "DHIGH", "DLOW"]

# ---- L2 未来函数更严黑名单(仅信息报告, 不做 v0 硬闸) ----
L2_FUTURE_TOKENS = [
    "BACKSET", "REFX", "REFXV", "REFXR", "BARSNEXT",
    "DCLOSE", "DOPEN", "DVOL",
    "ZIG", "ZIGA", "ZIGBARS", "FLATZIG",
    "PEAK", "PEAKA", "PEAKBARS", "TROUGH", "TROUGHA", "TROUGHBARS",
    "XMA", "FFT", "ZXNH",
]


def normalize_code(code: str) -> str:
    """源码归一化: 去全部空白 + 大写(去重键用, 防'同源码换格式再发')。"""
    return re.sub(r"\s+", "", code).upper()


def code_hash(code: str) -> str:
    return hashlib.sha256(normalize_code(code).encode("utf-8")).hexdigest()


def read_text(path, enc="utf-8", errors="ignore"):
    with open(path, "rb") as f:
        return f.read().decode(enc, errors=errors)
