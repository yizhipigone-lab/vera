"""tools/etf_rotation_iter_research3.py — 第三轮: 主旋钮全组合网格 + 13 年稳健性 (2026-09-16)。

用户两问: ①回测周期多久? ②组合有没有竭尽全力?
前两轮是单变量对照 (~80 组), 本轮把组合网格跑满:

  G1 窗口×阈值全网格: 8 窗口 {10,15,20,25,30,40,50,60} × 8 阈值 {0,0.5,1,1.5,2,3,4,5%}
      = 64 格, 基线其余参数不动 → 热力表, 找有没有全面胜基线的格
  G2 最优格×止损×分片: G1 卡玛前 5 格 × 止损 {10,12,15,20}% × 分片 {三份, 两份}
      = 40 组, 检验"好格子"是不是靠止损/分片运气
  G3 第三腿×窗口×阈值: 第三腿 {无, 证券, 半导体, 德国, 红利} × 窗口 {15,20,25}
      × 阈值 {0, 1.5%, 3%} = 45 组
  G4 13 年稳健性: 长历史品种 (纳指 2013-05 / 上证50 2013-01 / 红利 2013-01 / 黄金 2013-07)
      窗口 2013-05-15 → 2026-09-15 (约 13.3 年):
      {上证50+纳指} × 窗口 {10,15,20,25,30,60} × 阈值 {0, 1.5%} = 24 组
      + {红利+纳指} 与 {纳指单腿} 各 2 组对照 —— 验证 "20 日为峰" 是否 10 年运气

口径与前两轮完全一致 (T 收盘信号 / T+1 开盘成交, 万 1, 前复权)。
输出 output/etf_rotation_iter3/report.json。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import pandas as pd

OUT_DIR = "output/etf_rotation_iter3"
FEE = 0.0001
START, END = "2016-09-16", "2026-09-15"


def lm(n, p):
    spec = importlib.util.spec_from_file_location(n, p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


eng = lm("eng", "tools/etf_rotation_iter_research.py")

CYB, NAS, GOLD = eng.CYB, eng.NAS, eng.GOLD
DIV, SPX, SZ50, GER = eng.DIV, eng.SPX, eng.SZ50, eng.GER
ZQ, SEMI = "512880.SH", "512480.SH"
CSI300 = eng.CSI300

ALL = sorted({CYB, NAS, GOLD, DIV, SPX, SZ50, GER, ZQ, SEMI, CSI300})
BASE = dict(risk_legs=[CYB, NAS], hedge_legs=[GOLD], hedge_mode="fixed",
            mom_window=20, mom_threshold=0.0, stop_pct=0.15,
            anchors=[2, 3, 4])


def run(cfg, start=None, tag=""):
    old = eng.START
    if start:
        eng.START = start
    try:
        r = eng.run_variant(CL, OP, cfg, FEE)
    finally:
        eng.START = old
    m = r["metrics"]
    return {"name": tag, "cagr": m["cagr"], "max_dd": m["max_dd"],
            "sharpe": m["sharpe"], "calmar": m["calmar"],
            "switches": m["switches"], "risk_winrate": m["risk_winrate"]}


print("加载数据 ...")
CL, OP = eng.load_ohlc(ALL)
CL, OP = CL[:END], OP[:END]
out = {}

# ── G1 窗口×阈值全网格 (64 格) ──────────────────────────────────────
WINS = (10, 15, 20, 25, 30, 40, 50, 60)
THRS = (0.0, 0.005, 0.01, 0.015, 0.02, 0.03, 0.04, 0.05)
g1 = []
for w in WINS:
    for t in THRS:
        r = run(dict(BASE, mom_window=w, mom_threshold=t, name=f"w{w}t{t}"))
        r.update({"window": w, "thr": t})
        g1.append(r)
out["G1_grid"] = g1
print("G1 完成: 64 格")

# 热力表打印 (年化 % / 卡玛)
def heat(rows, key):
    hdr = "窗口\\阈值 " + " ".join(f"{t * 100:>7g}" for t in THRS)
    print(hdr)
    for w in WINS:
        line = f"{w:>5d}日   "
        for t in THRS:
            v = next(r[key] for r in rows if r["window"] == w and r["thr"] == t)
            line += f"{v * 100:7.1f} " if key == "cagr" else f"{v:7.2f} "
        print(line)


print("\nG1 年化热力表 (%):")
heat(g1, "cagr")
print("\nG1 卡玛热力表:")
heat(g1, "calmar")

# ── G2 卡玛前 5 格 × 止损 × 分片 ────────────────────────────────────
top5 = sorted(g1, key=lambda r: (r["calmar"] or 0), reverse=True)[:5]
print("\nG1 卡玛前 5 格:", [(r["window"], r["thr"]) for r in top5])
g2 = []
for cell in top5:
    for sp in (0.10, 0.12, 0.15, 0.20):
        for anchors, aname in (([2, 3, 4], "三份"), ([2, 4], "两份")):
            r = run(dict(BASE, mom_window=cell["window"],
                         mom_threshold=cell["thr"], stop_pct=sp,
                         anchors=anchors, name="g2"))
            r.update({"window": cell["window"], "thr": cell["thr"],
                      "stop": sp, "anchors": aname})
            g2.append(r)
out["G2_stop_tranche"] = g2
print(f"G2 完成: {len(g2)} 组")

# ── G3 第三腿×窗口×阈值 (45 组) ──────────────────────────────────────
THIRD = [(None, "无(基线)"), (ZQ, "证券"), (SEMI, "半导体"), (GER, "德国"), (DIV, "红利")]
g3 = []
for leg3, lname in THIRD:
    for w in (15, 20, 25):
        for t in (0.0, 0.015, 0.03):
            legs = [CYB, NAS] + ([leg3] if leg3 else [])
            r = run(dict(BASE, risk_legs=legs, mom_window=w,
                         mom_threshold=t, name="g3"))
            r.update({"third": lname, "window": w, "thr": t})
            g3.append(r)
out["G3_third_leg"] = g3
print(f"G3 完成: {len(g3)} 组")

# ── G4 13 年稳健性 (长历史品种) ──────────────────────────────────────
# 起点卡在黄金ETF数据起点 (2013-07-29) 之后, 否则进黄金时吃到 NaN 全链污染
START13 = "2013-08-01"
g4 = []
for w in (10, 15, 20, 25, 30, 60):
    for t in (0.0, 0.015):
        r = run(dict(BASE, risk_legs=[SZ50, NAS], mom_window=w,
                     mom_threshold=t, name="g4"), start=START13,
                tag=f"上证50+纳指 w{w} t{t}")
        r.update({"legs": "上证50+纳指", "window": w, "thr": t})
        g4.append(r)
for legs, lname in (([DIV, NAS], "红利+纳指"), ([NAS], "纳指单腿")):
    for w in (20,):
        for t in (0.0, 0.015):
            r = run(dict(BASE, risk_legs=legs, mom_window=w,
                         mom_threshold=t, name="g4"), start=START13,
                    tag=f"{lname} w{w} t{t}")
            r.update({"legs": lname, "window": w, "thr": t})
            g4.append(r)
out["G4_13y"] = g4
print(f"G4 完成: {len(g4)} 组")

os.makedirs(OUT_DIR, exist_ok=True)
with open(os.path.join(OUT_DIR, "report.json"), "w", encoding="utf-8") as f:
    json.dump({"window": [START, END], "grid_window_13y": START13,
                   "rules": "T收盘信号/T+1开盘, 万1, 前复权", "results": out},
              f, ensure_ascii=False, indent=2)
print("\n已写", os.path.join(OUT_DIR, "report.json"))
