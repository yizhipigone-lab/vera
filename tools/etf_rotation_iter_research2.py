"""tools/etf_rotation_iter_research2.py — 第二轮补测: 覆盖允许改动清单全部维度 (2026-09-16)。

第一轮 (etf_rotation_iter_research.py) 后用户指出覆盖不全, 本轮按清单逐格补齐:
  A 资金分配比例 (50%~100% 账户级)      B 分片机制 (单周一~五/两份/五份)
  C 风险腿候选池 (单腿诊断/替换/三腿/四腿/行业腿 2019+2020 窗)
  D 动量窗口 10~60 日网格               E 动量阈值 0~5% 网格
  F 避险篮子 (纯国债/纯十年/纯城投/三篮/货币)   G 大势过滤变体 (指数×均线)
  H 止损规则 (周频检查/12%)            I 仓位梯度 (top2 各半/两档)
  J 调仓触发 (每日/双周/月频)

口径与第一轮完全一致: T 收盘信号 / T+1 开盘成交, 万 1 佣金, 前复权, 同窗
(除行业腿批注明窗口)。输出 output/etf_rotation_iter2/report.json + 打印汇总。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import pandas as pd

OUT_DIR = "output/etf_rotation_iter2"
START, END = "2016-09-16", "2026-09-15"
FEE = 0.0001


def lm(n, p):
    spec = importlib.util.spec_from_file_location(n, p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


eng = lm("eng", "tools/etf_rotation_iter_research.py")

# ── 标的池 ───────────────────────────────────────────────────────────
CYB, NAS, GOLD = eng.CYB, eng.NAS, eng.GOLD
DIV, SPX, SZ50, HSI, GER, KWEB = eng.DIV, eng.SPX, eng.SZ50, eng.HSI, eng.GER, "513050.SH"
CSI300E, ZZ500, ZZ1000, KC50 = "510300.SH", "510500.SH", "512100.SH", "588000.SH"
ZQ, SEMI, MED, MIL = "512880.SH", "512480.SH", "512010.SH", "512660.SH"
WINE, COLOR, BANK, LVBO = "512690.SH", "512400.SH", "512800.SH", "512890.SH"
PV, SOY = "515790.SH", "159985.SZ"
GOV, GOV10, GOV22, MONEY = eng.GOV, eng.GOV10, "511220.SH", "511880.SH"
CSI300, CYB_IDX = eng.CSI300, eng.CYB_IDX

ALL = sorted({CYB, NAS, GOLD, DIV, SPX, SZ50, HSI, GER, KWEB, CSI300E,
              ZZ500, ZZ1000, KC50, ZQ, SEMI, MED, MIL, WINE, COLOR, BANK,
              LVBO, PV, SOY, GOV, GOV10, GOV22, MONEY, CSI300, CYB_IDX})

BASE = dict(risk_legs=[CYB, NAS], hedge_legs=[GOLD], hedge_mode="fixed",
            mom_window=20, mom_threshold=0.0, stop_pct=0.15,
            anchors=[2, 3, 4])

ROWS = []


def run(cfg, fee=FEE, start=None, end=None, tag=None, group=""):
    old_s, old_e = eng.START, eng.END
    if start:
        eng.START = start
    if end:
        eng.END = end
    try:
        r = eng.run_variant(CL, OP, cfg, fee)
    finally:
        eng.START, eng.END = old_s, old_e
    m = r["metrics"]
    row = {"group": group, "name": tag or cfg["name"],
           "cagr": m["cagr"], "max_dd": m["max_dd"], "sharpe": m["sharpe"],
           "calmar": m["calmar"], "monthly_max_dd": m["monthly_max_dd"],
           "switches": m["switches"], "risk_winrate": m["risk_winrate"],
           "pl_ratio": m["pl_ratio"], "yearly": m["yearly"],
           "regime": eng.regime_split(CL, CSI300, r["eq"]),
           "sw_stop": m.get("sw_stop"), "window": [eng.START if not start else start, END]}
    ROWS.append(row)
    print(f"[{group}] {row['name'][:42]:44s} 年化 {m['cagr'] * 100:6.2f}%  "
          f"回撤 {m['max_dd'] * 100:6.2f}%  夏普 {m['sharpe']:5.2f}  "
          f"卡玛 {str(m['calmar']):>5s}  换手 {m['switches']:4d}  "
          f"胜率 {m['risk_winrate']}")
    return row


def account_ratio(pool_eq: pd.Series, ratio: float) -> pd.Series:
    """账户级: 轮动池 ratio + 现金 (1-ratio, 0 收益)。"""
    ret = pool_eq.pct_change().fillna(0.0)
    return (1 + ratio * ret).cumprod()


print("加载数据 ...")
CL, OP = eng.load_ohlc(ALL)
CL, OP = CL[:END], OP[:END]

# ── A 资金分配比例 (账户级: 轮动池 X% + 现金) ─────────────────────────
b0 = eng.run_variant(CL, OP, dict(BASE, name="B0"), FEE)
for ratio in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
    acc = account_ratio(b0["eq"], ratio)
    m = eng.metrics_of(acc, b0["metrics"]["switches"], b0["metrics"].get("rts", []),
                       f"轮动池占 {int(ratio * 100)}%")
    ROWS.append({"group": "A", "name": f"资金分配: 轮动池 {int(ratio * 100)}% + 现金",
                 "cagr": m["cagr"], "max_dd": m["max_dd"], "sharpe": m["sharpe"],
                 "calmar": m["calmar"], "monthly_max_dd": m["monthly_max_dd"],
                 "switches": m["switches"], "risk_winrate": None, "pl_ratio": None,
                 "yearly": eng.yearly_returns(acc), "regime": None})
    print(f"[A] 轮动池 {int(ratio * 100):3d}% + 现金 {100 - int(ratio * 100):3d}%  "
          f"年化 {m['cagr'] * 100:6.2f}%  回撤 {m['max_dd'] * 100:6.2f}%  "
          f"夏普 {m['sharpe']:5.2f}  卡玛 {m['calmar']}")

# ── B 分片机制 ──────────────────────────────────────────────────────
for anchors, label in ([0], "单份周一"), ([1], "单份周二"), ([2], "单份周三"), \
        ([3], "单份周四"), ([4], "单份周五"), ([2, 4], "两份(周三+周五)"), \
        ([0, 1, 2, 3, 4], "五份(周一~周五)"):
    run(dict(BASE, name=label, anchors=anchors), tag=f"分片: {label}", group="B")
run(dict(BASE, name="三份(基线)"), tag="分片: 三份(基线对照)", group="B")

# ── C1 单腿诊断 (每条候选腿单独 + 黄金) ─────────────────────────────
for leg, cn in [(CYB, "创业板50"), (NAS, "纳指"), (DIV, "红利"), (SPX, "标普500"),
                (SZ50, "上证50"), (CSI300E, "沪深300"), (ZZ500, "中证500"),
                (ZZ1000, "中证1000"), (HSI, "恒生"), (GER, "德国"), (KWEB, "中概互联")]:
    run(dict(BASE, name=f"单腿:{cn}", risk_legs=[leg]), tag=f"单腿: {cn}", group="C1")

# ── C2 双腿替换/三腿/四腿 ───────────────────────────────────────────
for legs, cn in [([CYB, SPX], "创业板50+标普500"), ([CYB, GER], "创业板50+德国"),
                 ([CYB, HSI], "创业板50+恒生"), ([ZZ1000, NAS], "中证1000+纳指"),
                 ([SZ50, NAS], "上证50+纳指"),
                 ([CYB, NAS, DIV], "三腿: +红利"), ([CYB, NAS, GER], "三腿: +德国"),
                 ([CYB, NAS, ZQ], "三腿: +证券"), ([CYB, NAS, SPX], "三腿: +标普500"),
                 ([CYB, NAS, DIV, GER], "四腿: +红利+德国")]:
    run(dict(BASE, name=f"腿池:{cn}", risk_legs=legs), tag=f"腿池: {cn}", group="C2")

# ── C3 行业腿 (窗口 2019-07-01 起, 基线同窗对照) ─────────────────────
W19 = "2019-07-01"
run(dict(BASE, name="行业批: 基线同窗对照"), start=W19, group="C3")
for leg, cn in [(ZQ, "证券"), (SEMI, "半导体"), (MED, "医药"), (MIL, "军工"),
                (WINE, "酒"), (COLOR, "有色"), (BANK, "银行")]:
    run(dict(BASE, name=f"行业腿:{cn}", risk_legs=[CYB, NAS, leg]),
        start=W19, tag=f"三腿含行业: {cn}", group="C3")
W20 = "2020-12-18"
run(dict(BASE, name="行业批2: 基线同窗对照"), start=W20, group="C3")
for leg, cn in [(KC50, "科创50"), (PV, "光伏"), (SOY, "豆粕")]:
    run(dict(BASE, name=f"新腿:{cn}", risk_legs=[CYB, NAS, leg]),
        start=W20, tag=f"三腿含新标的: {cn}", group="C3")

# ── D 动量窗口网格 ──────────────────────────────────────────────────
for w in (10, 15, 20, 25, 30, 40, 50, 60):
    run(dict(BASE, name=f"窗口{w}日", mom_window=w), tag=f"动量窗口: {w} 日", group="D")

# ── E 动量阈值网格 ──────────────────────────────────────────────────
for t in (0.0, 0.005, 0.01, 0.015, 0.02, 0.03, 0.04, 0.05):
    run(dict(BASE, name=f"阈值{t * 100:g}%", mom_threshold=t),
        tag=f"动量阈值: {t * 100:g}%", group="E")

# ── F 避险篮子扩充 ──────────────────────────────────────────────────
run(dict(BASE, name="避险: 纯国债511010", hedge_legs=[GOV]), group="F")
run(dict(BASE, name="避险: 纯十年国债511260", hedge_legs=[GOV10]), group="F")
run(dict(BASE, name="避险: 纯城投债511220", hedge_legs=[GOV22]), group="F")
run(dict(BASE, name="避险: 黄金/十年国债各半", hedge_legs=[GOLD, GOV10],
         hedge_weights=[0.5, 0.5]), group="F")
run(dict(BASE, name="避险: 黄金/国债/城投三等权", hedge_legs=[GOLD, GOV, GOV22],
         hedge_weights=[1 / 3, 1 / 3, 1 / 3]), group="F")
run(dict(BASE, name="避险: 纯货币511880", hedge_legs=[MONEY]), group="F")

# ── G 大势过滤变体 ──────────────────────────────────────────────────
run(dict(BASE, name="闸门: 创业板指200线(生产同款)",
         regime={"gate_by_leg": {CYB: CYB_IDX, NAS: "self"}, "ma": 200}), group="G")
run(dict(BASE, name="闸门: 沪深300 120日线",
         regime={"gate_by_leg": {CYB: CSI300, NAS: "self"}, "ma": 120}), group="G")
run(dict(BASE, name="闸门: 沪深300 250日线",
         regime={"gate_by_leg": {CYB: CSI300, NAS: "self"}, "ma": 250}), group="G")

# ── H 止损规则变体 ──────────────────────────────────────────────────
run(dict(BASE, name="止损: 仅信号日检查(周频)", stop_check="signal"), group="H")
run(dict(BASE, name="止损: 12%", stop_pct=0.12), group="H")

# ── I 仓位梯度其他形态 ──────────────────────────────────────────────
r1 = eng.run_variant(CL, OP, dict(BASE, name="top1", pick_rank=1), FEE)
r2 = eng.run_variant(CL, OP, dict(BASE, name="top2", pick_rank=2), FEE)
eq12 = (r1["eq"] + r2["eq"]) / 2
m12 = eng.metrics_of(eq12, r1["metrics"]["switches"] + r2["metrics"]["switches"],
                     [], "top2各半")
ROWS.append({"group": "I", "name": "仓位: 动量前两名各 50%",
             "cagr": m12["cagr"], "max_dd": m12["max_dd"],
             "sharpe": m12["sharpe"], "calmar": m12["calmar"],
             "monthly_max_dd": m12["monthly_max_dd"],
             "switches": m12["switches"], "risk_winrate": None, "pl_ratio": None,
             "yearly": eng.yearly_returns(eq12),
             "regime": eng.regime_split(CL, CSI300, eq12)})
print(f"[I] 动量前两名各50%  年化 {m12['cagr'] * 100:6.2f}%  "
      f"回撤 {m12['max_dd'] * 100:6.2f}%  夏普 {m12['sharpe']:5.2f}  卡玛 {m12['calmar']}")
run(dict(BASE, name="梯度2档: ≥5%满仓/0~5%半仓",
         tiers=[(0.05, 1.0), (0.0, 0.5)]), group="I")

# ── J 调仓触发逻辑 ──────────────────────────────────────────────────
run(dict(BASE, name="触发: 每日都算信号", freq="d"), group="J")
run(dict(BASE, name="触发: 双周频(偶数周)", freq="bw"), group="J")
run(dict(BASE, name="触发: 月频(每月最后交易日, 单份)", freq="m", anchors=[4]), group="J")

os.makedirs(OUT_DIR, exist_ok=True)
with open(os.path.join(OUT_DIR, "report.json"), "w", encoding="utf-8") as f:
    json.dump({"window": [START, END], "rules": "T收盘信号/T+1开盘, 万1, 前复权",
                   "results": ROWS}, f, ensure_ascii=False, indent=2)
print("\n已写", os.path.join(OUT_DIR, "report.json"), " 共", len(ROWS), "行")
