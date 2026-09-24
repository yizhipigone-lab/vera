"""
gs_txt 5m 扫描 — MD 评测报告生成器 (阶段C, 2026-07-18)

遍历 output/gs_5m_sweep/<formula>/report_merged.csv, 汇总:
  - 达标公式 Top (年化>30% + 回撤≤15% + 交易≥1000, 按 Calmar)
  - 每公式最佳组合
  - 参数分布 (哪些止盈止损档位出达标多)
  - 零达标公式清单
输出 output/gs_5m_sweep/EVAL_REPORT.md

用法: python tools/gs_make_report.py
"""
import glob
import os
import re
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.tdx_path import tdx_home

BASE = "output/gs_5m_sweep"
OUT_MD = os.path.join(BASE, "EVAL_REPORT.md")
# 2026-09-11: 达标线收口到 core/farm_rules (单一真相源; 2026-09-22 起年化≥10%
# 且 |回撤|≤15% 且 笔数≥20)。
# 此前本文件硬编码 0.30/0.15/1000, 与 09-09 批实际在用的 15% 冲突。
from core import farm_rules  # noqa: E402

TARGET_ANN = farm_rules.TARGET_ANN
TARGET_MAXDD = farm_rules.TARGET_MAXDD
MIN_TRADES = farm_rules.MIN_TRADES

# 未来函数排除 (权威清单, 2026-07-20 网上核实). 报告只含干净公式
# 2026-09-16 F4 收口: 原手写副本改引单一真相源 tools/future_tokens.py
# (第一轮自查抓到的漏网副本, 比并集少 ZXNH)
GS_DIR = os.path.join(tdx_home(), "T0001", "export", "gs_txt")
from tools.future_tokens import FUTURE_TOKEN_BLACKLIST as EXCL_FUNCS  # noqa: E402
CROSS_FUNCS = ["#MONTH", "#WEEK", "#DAY"]


def has_future(name: str) -> list:
    """读公式源码, 返回含的未来函数列表 (空=干净)."""
    f = os.path.join(GS_DIR, f"gs_1_{name}.txt")
    if not os.path.exists(f):
        return []
    raw = open(f, "rb").read()
    t = None
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            t = raw.decode(enc)
            break
        except Exception:
            pass
    src = t.split("Source Code:", 1)[1] if t and "Source Code:" in t else (t or "")
    hits = [kw for kw in EXCL_FUNCS if re.search(r"(?i)\b" + kw + r"\b", src)]
    hits += [kw for kw in CROSS_FUNCS if kw in src.upper()]
    return hits


LADDER_CN = {
    "off": "无阶梯", "s5_100": "单档5%全平", "s8_100": "单档8%全平",
    "s12_100": "单档12%全平", "m2_5-12_30": "双档5%+12%各30%",
    "m2_6-15_30": "双档6%+15%各30%", "m4_4-8-15-25_20": "四档4/8/15/25%各20%",
    "m3_4-10-20_25": "三档4/10/20%各25%",
}


def combo_cn(r) -> str:
    """参数行 -> 中文描述 (CLAUDE.md 术语中文化)."""
    ladder = LADDER_CN.get(str(r.get("ladder", "")), str(r.get("ladder", "")))
    return (f"硬止损{abs(r['cost'])*100:.0f}% 移动止盈激活{r['act']*100:.1f}% "
            f"回撤{r['dd']*100:.1f}% {ladder} 时间止损{int(r['time_days'])}天")



def main():
    rows = []
    read_fail = []  # G5: 读取失败的公式名单, 报告末尾列出 (不再静默消失)
    dirs = sorted(d for d in glob.glob(os.path.join(BASE, "*")) if os.path.isdir(d))
    if not dirs:
        print("[ERR] 无公式目录")
        return
    for d in dirs:
        formula = os.path.basename(d)
        if has_future(formula):
            continue  # 排除含未来函数的公式 (权威清单 2026-07-20)
        sweeps = sorted(glob.glob(os.path.join(d, "sweep_*.csv")))
        if not sweeps:
            rows.append({"formula": formula, "n_combos": 0, "hit": 0,
                         "best_annret": None, "best_calmar": None,
                         "best_maxdd": None, "best_trades": None, "best_combo": ""})
            continue
        try:
            df = pd.concat([pd.read_csv(s) for s in sweeps], ignore_index=True)
        except Exception as e:                                   # noqa: BLE001
            read_fail.append((formula, f"{type(e).__name__}: {e}"))
            continue
        for col in ["annret", "maxdd", "calmar", "trades", "cost", "act", "dd"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df[df["annret"].notna()]
        if df.empty:
            rows.append({"formula": formula, "n_combos": 0, "hit": 0,
                         "best_annret": None, "best_calmar": None,
                         "best_maxdd": None, "best_trades": None,
                         "best_combo": ""})
            continue
        hit = df[df.apply(farm_rules.is_pass, axis=1)]
        if not hit.empty:
            best = hit.loc[hit["calmar"].idxmax()]
            rows.append({"formula": formula, "n_combos": len(df), "hit": len(hit),
                         "best_annret": best["annret"], "best_calmar": best["calmar"],
                         "best_maxdd": best["maxdd"], "best_trades": best["trades"],
                         "best_combo": combo_cn(best)})
        else:
            # G1: 无达标组合时, "最佳"也只在笔数 ≥ MIN_TRADES 的组合里选
            # (farm_rules.pick_best); 全不足样本退回最高年化但标注「样本不足」
            top = farm_rules.pick_best(df.to_dict("records"))
            thin = farm_rules.verdict(top.get("annret"), top.get("maxdd"),
                                      top.get("trades"))["code"] == farm_rules.THIN
            rows.append({"formula": formula, "n_combos": len(df), "hit": 0,
                         "best_annret": top["annret"], "best_calmar": top["calmar"],
                         "best_maxdd": top["maxdd"], "best_trades": top["trades"],
                         "best_combo": combo_cn(top) + (
                             "（样本不足（笔数<20），数字不作数）" if thin else "")})
    rep = pd.DataFrame(rows)
    rep.to_csv(os.path.join(BASE, "eval_summary.csv"), index=False, encoding="utf-8")

    hit_rep = rep[rep["hit"] > 0].sort_values("best_calmar", ascending=False)
    n_formulas = len(rep)
    n_hit = len(hit_rep)
    n_zero_combo = (rep["n_combos"] == 0).sum()

    lines = []
    lines.append("# gs_txt 5m 全参数扫描 — 评测报告\n")
    lines.append("> 生成: 阶段C 汇总 | 区间 2024-08-01 ~ 2026-07-17 | "
                 "5m | T收盘买入 | 沪深300 | 300万/单票2万 | 移动止盈优先\n")
    lines.append(f"> {farm_rules.describe()}\n")
    lines.append("> **已排除未来函数** (权威清单: ZIG/PEAK/TROUGH/BACKSET/REFX/DCLOSE/DRAWLINE/XMA/FFT/#周期等) "
                 "— 含 DCLOSE/DRAWLINE 的公式(之前回测虚高)已剔除\n")
    lines.append("\n## 一、总览\n")
    lines.append(f"- 扫描公式数: **{n_formulas}**")
    lines.append(f"- 达标公式数: **{n_hit}** ({n_hit/max(n_formulas,1)*100:.1f}%)")
    lines.append(f"- 零组合公式(选股零信号/prep失败): {n_zero_combo}\n")

    lines.append("## 二、达标公式 Top（按 Calmar 降序）\n")
    if hit_rep.empty:
        lines.append("_无公式达标_\n")
    else:
        lines.append("| 排名 | 公式 | 达标组合数 | 最佳年化 | 回撤 | Calmar | 交易笔数 | 最佳参数 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for i, r in hit_rep.head(50).iterrows():
            lines.append(f"| {i+1} | {r['formula']} | {int(r['hit'])} | "
                         f"{r['best_annret']*100:.1f}% | {r['best_maxdd']*100:.1f}% | "
                         f"{r['best_calmar']:.2f} | {int(r['best_trades'])} | {r['best_combo']} |")
        lines.append("")

    lines.append("\n## 三、全部公式最佳组合（按年化降序 Top 30）\n")
    lines.append("| 公式 | 达标组合数 | 最佳年化 | 回撤 | Calmar | 交易 | 最佳参数 |")
    lines.append("|---|---|---|---|---|---|---|")
    for _, r in rep.sort_values("best_annret", ascending=False).head(30).iterrows():
        ann = f"{r['best_annret']*100:.1f}%" if pd.notna(r['best_annret']) else "-"
        dd = f"{r['best_maxdd']*100:.1f}%" if pd.notna(r['best_maxdd']) else "-"
        cal = f"{r['best_calmar']:.2f}" if pd.notna(r['best_calmar']) else "-"
        tr = int(r['best_trades']) if pd.notna(r['best_trades']) else "-"
        lines.append(f"| {r['formula']} | {int(r['hit'])} | {ann} | {dd} | {cal} | {tr} | {r['best_combo']} |")
    lines.append("")

    # 参数分布 (Top 30 公式最佳组合, 从 best_combo 中文解析)
    lines.append("\n## 四、参数分布（Top 30 公式最佳组合）\n")
    top30 = rep.sort_values("best_annret", ascending=False).head(30)
    from collections import Counter

    def _extract(pat):
        c = Counter()
        for _, r in top30.iterrows():
            m = re.search(pat, str(r["best_combo"]))
            if m:
                c[m.group(1)] += 1
        return c

    lines.append("**硬止损档位:**")
    for k, v in _extract(r"硬止损(\d+%)").most_common():
        lines.append(f"  - {k}: {v} 个")
    lines.append("\n**移动止盈激活线:**")
    for k, v in _extract(r"移动止盈激活([\d.]+%)").most_common():
        lines.append(f"  - {k}: {v} 个")
    lines.append("\n**时间止损:**")
    for k, v in _extract(r"时间止损(\d+天)").most_common():
        lines.append(f"  - {k}: {v} 个")
    lines.append("\n**阶梯止盈:**")
    for k, v in _extract(r"回撤[\d.]+%\s*(.+?)\s*时间止损").most_common():
        lines.append(f"  - {k}: {v} 个")
    lines.append("")

    if read_fail:
        lines.append("\n## 五、读取失败\n")
        lines.append("以下公式的 sweep CSV 读取失败, 已从本报告剔除"
                     "（2026-09-16 G5 修复前是静默消失, 无清单）:\n")
        for f_name, err in read_fail:
            lines.append(f"- **{f_name}**: {err}")
        lines.append("")

    lines.append("\n---\n> 详细每公式结果: output/gs_5m_sweep/<公式>/report_merged.csv")
    lines.append("> 汇总 CSV: output/gs_5m_sweep/eval_summary.csv\n")

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[OK] 公式数={n_formulas} 达标={n_hit} 零组合={n_zero_combo} "
          f"读取失败={len(read_fail)}")
    print(f"[OUT] {OUT_MD}")


if __name__ == "__main__":
    main()
