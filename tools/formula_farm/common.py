# -*- coding: utf-8 -*-
"""公式农场公共常量/工具 (v0)。

v0 纪律: 只增不改、只读 gongshi/TDX gs_txt、绝不写 TDX。
token 清单复刻 gongshi 实战(_batch_import.py + _clean_formulas.json 基线)。
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time

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


# ---- 入库账本 (2026-09-16 自 farm_onboard 迁入: 账本落盘是纯数据逻辑,
# 不该拖着 psutil/pyautogui/pywinauto 的 GUI 硬依赖 —— 否则测试 import
# 链在新环境收集即炸, 复审 M4) ----

def save_onboard(ob_path, date_str, items):
    """合并写 onboard.json (读旧 → 按文件合并 → 原子替换落盘)。

    2026-09-06 血泪教训: 覆盖写会把历史 ok 记录冲掉 → 断点失效重复入库,
    所以一律合并写 (同文件取最新一轮的结果)。
    供 farm_onboard「每入一条立即记账」逐条调用 (中途停止不丢账)。
    """
    merged = {}
    if os.path.exists(ob_path):
        try:
            with open(ob_path, encoding="utf-8") as f:
                for it in json.load(f).get("items", []):
                    merged[it.get("file")] = it
        except Exception:
            pass
    for it in items:
        merged[it["file"]] = it
    tmp = ob_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "finished_at": time.strftime("%H:%M:%S"),
                   "items": list(merged.values())}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ob_path)
