"""
gs_txt 公式筛选 —— 遍历 TDX 公式目录，过滤出：
  无未来函数 + 无 DYNAINFO + 无绘图画线函数 + 无 D 系列 + 无跨周期引用 + 无争议函数 + 产出信号非0
输出的 MD 报告放到 docs/ 下。
"""
import os
import re
from datetime import datetime

GS_DIR = r"E:\NEW_TDX\T0001\export\gs_txt"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "gs_formula_signal_scan.md")

# ── 禁用函数 ────────────────────────────────────
FUTURE_FNS = [
    "ZIG", "PEAK", "PEAKBARS", "TROUGH", "TROUGHBARS",
    "BACKSET", "REFX", "REFXV", "FILTERX",
]

DRAWING_FNS = [
    "DRAWTEXT", "DRAWICON", "DRAWLINE", "DRAWKLINE",
    "STICKLINE", "POLYLINE", "PLOYLINE",
    "FILLRGN", "DRAWNUMBER", "DRAWSL",
    "DRAWBAND", "DRAWCOLOR",
]

OTHER_BLOCKED = [
    "DYNAINFO",
    "DCLOSE", "DOPEN", "DHIGH", "DLOW", "DVOL",
]

CONTESTED_FNS = ["COST", "WINNER", "LWINNER", "CONST", "ZXNH"]
CROSS_PERIOD = ["#WEEK", "#MONTH", "#YEAR"]

# ── 公式类型解码 ────────────────────────────────
_TYPE_MAP = {"0": "副图", "1": "主图", "2": "选股", "4": "其他"}

def _decode_type(line2: str) -> str:
    """第二行 '15,0' → '副图(用户)'"""
    parts = line2.strip().split(",")
    if len(parts) >= 2:
        sub = parts[1].strip()
        return _TYPE_MAP.get(sub, f"类型{sub}")
    return line2.strip()

# ── 信号判定 ──────────────────────────────────────
_OUTPUT_RE = re.compile(
    r"^(?![Dd][Rr][Aa][Ww])([A-Za-z\u4e00-\u9fff_][A-Za-z0-9\u4e00-\u9fff_]*)"
    r"\s*:(?!=)\s*(.+)",
    re.UNICODE,
)

def _has_word(text: str, word: str) -> bool:
    return bool(re.search(r"\b" + re.escape(word) + r"\b", text, re.IGNORECASE))

def check_forbidden(source: str):
    for fn in FUTURE_FNS:
        if _has_word(source, fn):
            return True, f"未来函数:{fn}"
    for fn in DRAWING_FNS:
        if _has_word(source, fn):
            return True, f"绘图函数:{fn}"
    for fn in OTHER_BLOCKED:
        if _has_word(source, fn):
            return True, f"禁止函数:{fn}"
    for fn in CONTESTED_FNS:
        if _has_word(source, fn):
            return True, f"争议函数:{fn}"
    for cp in CROSS_PERIOD:
        if cp in source.upper():
            return True, f"跨周期引用:{cp}"
    return False, None

def has_signal(source: str) -> bool:
    for line in source.splitlines():
        line = line.strip()
        if not line or line.startswith("{"):
            continue
        m = _OUTPUT_RE.match(line)
        if m:
            expr = m.group(2).strip().rstrip(";")
            if re.match(r"^[0-9.]+$", expr):
                continue
            return True
    return False

def main():
    total = 0
    passed = 0
    rejected: dict[str, list[str]] = {}
    results: list[dict] = []

    for fname in sorted(os.listdir(GS_DIR)):
        if not fname.endswith(".txt"):
            continue
        total += 1
        fpath = os.path.join(GS_DIR, fname)

        try:
            with open(fpath, encoding="gbk") as fh:
                raw = fh.read()
        except Exception:
            try:
                with open(fpath, encoding="utf-8") as fh:
                    raw = fh.read()
            except Exception:
                rejected.setdefault("读取失败", []).append(fname)
                continue

        clean = re.sub(r"\{[^}]*\}", "", raw)

        bad, reason = check_forbidden(clean)
        if bad:
            rejected.setdefault(reason, []).append(fname)
            continue

        if not has_signal(clean):
            rejected.setdefault("无输出信号(无:输出变量)", []).append(fname)
            continue

        passed += 1
        raw_lines = raw.splitlines()
        name = raw_lines[0].strip() if raw_lines else "?"
        ftype = _decode_type(raw_lines[1]) if len(raw_lines) > 1 else "?"
        upd_date = ""
        for l in raw_lines[2:6]:
            if l.startswith("UpdateDate:"):
                upd_date = l.split(":", 1)[-1].strip()
                break

        results.append({
            "name": name,
            "file": fname,
            "type": ftype,
            "date": upd_date,
        })

    # ── 写 MD ────────────────────────────────────
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rejected_total = total - passed

    # 按类型分组统计通过数
    from collections import Counter
    type_counts = Counter(r["type"] for r in results)

    # 淘汰原因排序
    elim_order = sorted(rejected.items(), key=lambda kv: -len(kv[1]))

    out_lines = []
    def w(s=""):
        out_lines.append(s)

    w("# gs_txt 公式信号筛选报告")
    w()
    w("> {}  |  总 {} 个 → 通过 **{}** 个，淘汰 {} 个".format(now, total, passed, rejected_total))
    w()
    w("---")
    w()
    w("## 一、筛选用途")
    w()
    w("从 TDX 本地 {} 个 gs_txt 公式中，筛掉含未来函数/绘图函数/不定周期/跨周期/争议函数的公式，保留**可用于策略回测的信号型公式**。".format(total))
    w()
    w("---")
    w()
    w("## 二、筛选规则")
    w()
    w("共 7 条硬性规则，任一条命中即淘汰：")
    w()
    w("### ① 未来函数 — 信号会随行情变化而漂移，回测不可信")
    w()
    w("`ZIG` `PEAK` `PEAKBARS` `TROUGH` `TROUGHBARS` — 之字转向类，事后找顶底")
    w("`BACKSET` — 向前赋值，后面条件成立就改写前面 K 线状态")
    w("`REFX` `REFXV` — 引用未来 N 周期数据")
    w("`FILTERX` — 反向过滤，与普通 FILTER 完全不同，高危")
    w()
    w("### ② 绘图画线函数 — 仅用于图表展示，不产生可回测信号")
    w()
    w("`DRAWTEXT` `DRAWICON` `DRAWLINE` `DRAWKLINE` `STICKLINE` `POLYLINE` `PLOYLINE` `FILLRGN` `DRAWNUMBER` `DRAWSL` `DRAWBAND` `DRAWCOLOR`")
    w()
    w("### ③ D 系列不定周期 — 引用非固定周期数据，隐含未来信息")
    w()
    w("`DCLOSE` `DOPEN` `DHIGH` `DLOW` `DVOL`")
    w()
    w("### ④ 跨周期引用 — 日线引用周/月/年线，当周/月未收盘时数据持续变动")
    w()
    w("`#WEEK` `#MONTH` `#YEAR`")
    w()
    w("### ⑤ 实时动态数据 — 仅盘中有效，无法回测")
    w()
    w("`DYNAINFO`")
    w()
    w("### ⑥ 争议函数 — 含未来/统计偏差，不建议用于策略回测")
    w()
    w("`COST` `WINNER` `LWINNER` — 获利盘/成本分布类，含全样本统计信息")
    w("`CONST` — 常数函数，取值依赖数据起点")
    w()
    w("### ⑦ 必须产出信号")
    w()
    w("公式至少有一个输出变量（`:` 而非 `:=`），且不是纯数字常量。全是中间赋值的公式不产生选股信号。")
    w()
    w("---")
    w()
    w("## 三、筛选结果总览")
    w()
    w("| | 数量 |")
    w("|---|---|")
    w("| 总公式 | {} |".format(total))
    w("| ✅ 通过 | **{}** |".format(passed))
    w("| ❌ 淘汰 | {} |".format(rejected_total))
    w()
    w("### 通过公式类型分布")
    w()
    w("| 类型 | 数量 |")
    w("|---|---|")
    for t in ["选股", "副图", "主图", "其他"]:
        if t in type_counts:
            w("| {} | {} |".format(t, type_counts[t]))
    for t, c in type_counts.items():
        if t not in ["选股", "副图", "主图", "其他"]:
            w("| {} | {} |".format(t, c))
    w()
    w("### 淘汰原因分布")
    w()
    w("| 淘汰原因 | 数量 |")
    w("|---|---|")
    for reason, files in elim_order:
        w("| {} | {} |".format(reason, len(files)))
    w()
    w("---")
    w()
    w("## 四、通过公式清单（{} 个）".format(passed))
    w()
    if results:
        w("| # | 公式名 | 文件 | 类型 | 更新日期 |")
        w("|---|--------|------|------|----------|")
        for i, r in enumerate(results, 1):
            w("| {} | {} | `{}` | {} | {} |".format(i, r['name'], r['file'], r['type'], r['date']))
    else:
        w("（无）")
    w()
    w("---")
    w()
    w("## 五、淘汰明细")
    w()
    for reason, files in elim_order:
        w("### {}（{} 个）".format(reason, len(files)))
        w()
        for fn in sorted(files):
            # 读第一行取公式名
            try:
                with open(os.path.join(GS_DIR, fn), encoding="gbk") as fh:
                    first = fh.readline().strip()
            except Exception:
                first = fn
            w("- {}  `{}`".format(first, fn))
        w()

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out_lines))

    print("总={}  通过={}  淘汰={}".format(total, passed, rejected_total))
    print("报告 → {}".format(OUT))

if __name__ == "__main__":
    main()
