# -*- coding: utf-8 -*-
"""公式农场公共常量/工具 (v0)。

v0 纪律: 只增不改、只读 gongshi/TDX gs_txt、绝不写 TDX。
token 清单复刻 gongshi 实战(_batch_import.py + _clean_formulas.json 基线)。
"""
import hashlib
import os
import re
import subprocess
import sys

from tools.future_tokens import FUTURE_TOKEN_BLACKLIST

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
# 2026-09-16 F4 收口: 权威清单在 tools/future_tokens.py (三份清单并集)
L2_FUTURE_TOKENS = FUTURE_TOKEN_BLACKLIST


def normalize_code(code: str) -> str:
    """源码归一化: 去全部空白 + 大写(去重键用, 防'同源码换格式再发')。"""
    return re.sub(r"\s+", "", code).upper()


def code_hash(code: str) -> str:
    return hashlib.sha256(normalize_code(code).encode("utf-8")).hexdigest()


def read_text(path, enc="utf-8", errors="ignore"):
    with open(path, "rb") as f:
        return f.read().decode(enc, errors=errors)


# ---- 飞书推送 (2026-09-16 F6 收口: 原 farm_onboard/farm_backtest/farm_verify
# 三份近乎逐行相同的实现合一; 推送是通知不是闸门, 失败只记日志不抛) ----
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def push_feishu(md_path, title, logger=None):
    """把 MD 报告推飞书卡片; logger 可传各脚本的 log 函数 (缺省 print)。"""
    _log = logger or (lambda s: print(s, flush=True))
    try:
        r = subprocess.run(
            [sys.executable, "-X", "utf8",
             os.path.join(_PROJECT_ROOT, "tools", "send_report_feishu.py"),
             md_path, title],
            cwd=_PROJECT_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120)
        _log("飞书推送: %s" % ("成功" if r.returncode == 0
                              else "失败 rc=%d %s" % (
                                  r.returncode,
                                  (r.stderr or r.stdout or "")[-120:])))
    except Exception as e:                                       # noqa: BLE001
        _log("飞书推送: 异常 %r" % e)


# ---- 入库索引/断点集/账本 (2026-09-16 看板审计 M2/M3: 正主已迁
# core/farm_ledger.py —— core/farm_summary 也要用, 放 tools 会造成 core→tools
# 分层倒挂, 且本文件不再继续增肥; 此处兼容再导出, 既有调用方
# (farm_onboard/farm_backtest/tests) 零改动, 新代码请直接引 core.farm_ledger) ----
from core.farm_ledger import load_done_files, load_onboard_index, save_onboard  # noqa: E402,F401
