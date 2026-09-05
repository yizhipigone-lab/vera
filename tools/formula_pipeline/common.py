# -*- coding: utf-8 -*-
"""formula_pipeline 共用模块 (2026-08-26, 全新编写, 不依赖 tools/ 旧脚本)。

公式流水线的基础设施: 路径、run 目录管理、公式 txt 容错读取与结构解析。
项目级依赖仅限: core/tdx_path (TDX_HOME 定位)。
"""
import json
import os
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent  # 项目根
PIPELINE_DIR = Path(__file__).resolve().parent

# 公式批次目录 (2026-08-26 参数化: 支持 gs_0_/gs_1_ 任意批)
GS_DIR = Path(r"E:\NEW_TDX\T0001\export\gs_txt")
# 默认批次 (兼容旧行为); 各阶段脚本可用 --batch 覆盖
GS_PREFIX = "gs_1_"


def gs_files(batch: str = "gs_1_") -> list:
    """指定批次的全部 txt 路径 (排序稳定)。batch 如 'gs_0_'/'gs_1_'。"""
    return sorted(p for p in GS_DIR.glob(batch + "*.txt") if p.is_file())


def gs1_files() -> list:
    """兼容旧名: 默认 gs_1_ 批。"""
    return gs_files(GS_PREFIX)

# 输出目录: 每次全量 run 一个时间戳目录
OUTPUT_ROOT = ROOT / "output" / "formula_pipeline"

# 静态扫描排除清单 (2026-08-26 00:37 定稿口径, 自建清单)
FUTURE_FUNCS = [
    "ZIG", "ZIGA", "ZIGBARS", "FLATZIG", "FLATZIGA",
    "PEAK", "PEAKA", "PEAKBARS", "PEAKBARSA",
    "TROUGH", "TROUGHA", "TROUGHBARS",
    "BACKSET", "REFX", "REFXV", "REFXR", "BARSNEXT",
    "DCLOSE", "DHIGH", "DLOW", "DOPEN", "DVOL",
    "DRAWLINE", "XMA", "FFT",
]
# 漂移画图 (用户 00:37: 只排影响信号/会漂移的画图; 纯装饰画图不排, 解释器跳过)
DRIFT_DRAW_FUNCS = ["POLYLINE"]
# 通达信专有数据函数 (本地无筹码/财务/实时数据, 用户拍板直接排除)
PROPRIETARY_FUNCS = ["WINNER", "COST", "FINANCE", "DYNAINFO", "CAPITAL"]
# 跨周期引用 (盘中大周期数据会变, 隐前视)
CROSS_PERIOD = ["#MONTH", "#WEEK", "#DAY"]
# 装饰画图 (不排除; 解释器按空语句跳过; 用于输出结构统计时排除这些行)
DECORATIVE_DRAW_FUNCS = [
    "STICKLINE", "DRAWTEXT", "DRAWICON", "DRAWNUMBER", "DRAWSTRING",
    "DRAWGBK", "DRAWBAND", "DRAWKLINE", "PARTLINE", "VERTLINE", "HLINE",
    "PLOYLINE", "DRAWTEXT_FIX", "DRAWNUMBER_FIX", "FILLRGN", "RGB",
]


def read_formula_txt(path: Path) -> dict:
    """容错读取公式 txt → {name, params, source, file}。

    格式: 第1行公式名; 第2行参数默认值 (如 `15,0` → P1=15,P2=0);
    之后 UpdateDate/UpdateTime; `Source Code:` 之后为源码。
    """
    raw = Path(path).read_bytes()
    text = None
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="ignore")

    lines = [l.rstrip() for l in text.splitlines()]
    name = lines[0].strip() if lines else ""
    params = []
    if len(lines) > 1 and "," in lines[1]:
        try:
            params = [p.strip() for p in lines[1].split(",") if p.strip() != ""]
        except Exception:
            params = []
    source = text.split("Source Code:", 1)[1] if "Source Code:" in text else text
    return {"file": Path(path).name, "name": name, "params": params,
            "source": source.strip()}


def formula_display_name(filename: str) -> str:
    """gs_X_<name>.txt → <name> (兼容任意批次前缀)。"""
    n = filename
    # 剥掉 gs_N_ 前缀 (gs_0_/gs_1_/gs_2_ 等)
    import re as _re
    m = _re.match(r"^gs_\d+_", n)
    if m:
        n = n[m.end():]
    if n.endswith(".txt"):
        n = n[:-4]
    return n.strip()


def scan_static(source: str) -> dict:
    """静态扫描源码 → 命中的排除原因。词边界匹配防变量名误伤。"""
    hits = {"future": [], "polyline": [], "zxnh": [],
            "proprietary": [], "cross_period": []}
    for f in FUTURE_FUNCS:
        if re.search(r"\b" + f + r"\b", source, re.IGNORECASE):
            hits["future"].append(f)
    for f in DRIFT_DRAW_FUNCS:
        if re.search(r"\b" + f + r"\b", source, re.IGNORECASE):
            hits["polyline"].append(f)
    if re.search(r"\bZXNH\b", source, re.IGNORECASE):
        hits["zxnh"].append("ZXNH")
    for f in PROPRIETARY_FUNCS:
        if re.search(r"\b" + f + r"\b", source, re.IGNORECASE):
            hits["proprietary"].append(f)
    up = source.upper()
    for c in CROSS_PERIOD:
        if c in up:
            hits["cross_period"].append(c)
    return {k: v for k, v in hits.items() if v}


def new_run_dir() -> Path:
    """创建带时间戳的 run 目录 (静态扫描等共享)。"""
    run = OUTPUT_ROOT / time.strftime("run_%Y%m%d_%H%M%S")
    run.mkdir(parents=True, exist_ok=True)
    return run


def save_json(path: Path, obj) -> None:
    """原子写 JSON: 先写 .tmp 再 rename, 防半截文件 + 规避杀软临时锁。

    2026-08-26: 5m 分段跑批撞 PermissionError (杀软扫过落盘文件的瞬间),
    重试 3 次 (间隔 1s) 兜底。
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    data = json.dumps(obj, ensure_ascii=False, indent=2)
    for attempt in range(4):
        try:
            tmp.write_text(data, encoding="utf-8")
            tmp.replace(path)
            return
        except PermissionError:
            if attempt >= 3:
                raise
            time.sleep(1)


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def gs1_files() -> list:
    """gs_1 批全部 txt 路径 (排序稳定)。"""
    return sorted(p for p in GS_DIR.glob(GS_PREFIX + "*.txt") if p.is_file())
