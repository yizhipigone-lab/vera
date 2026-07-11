"""敏感性上界: 把'嫌疑'交易 (高最新股本+低价) 强行挪回 <50亿, 看小盘效应结论被扭转多少。
这是偏差影响的上界估计 (现实中只有一部分嫌疑是真错分)。"""
import csv
from collections import defaultdict, Counter
from statistics import mean
from pathlib import Path

rows = []
with Path("output/mcap_analysis/trades_with_mcap.csv").open(encoding="utf-8-sig") as fh:
    for r in csv.DictReader(fh):
        def f(x):
            try: return float(x)
            except: return None
        ep, ac, cm, pp = f(r.get("entry_price")), f(r.get("active_cap")), f(r.get("circ_mcap")), f(r.get("pnl_pct"))
        try: ey = int(r.get("entry_date","")[:4])
        except: ey = None
        rows.append({"code":r.get("stock_code"),"ey":ey,"ep":ep,"ac":ac,"cm":cm,"bucket":r.get("cap_bucket"),"pp":pp})

acs = sorted([r["ac"] for r in rows if r["ac"]>0])
eps = sorted([r["ep"] for r in rows if r["ep"]>0])
ac_p75 = acs[int(len(acs)*0.75)]; ac_p90 = acs[int(len(acs)*0.90)]
ep_p25 = eps[int(len(eps)*0.25)]

def show(tag, bucket_of):
    by = defaultdict(list)
    for r in rows:
        if r["pp"] is None: continue
        b = bucket_of(r)
        if b: by[b].append(r["pp"])
    print(f"\n--- {tag} ---")
    for k in ["<50亿","50-100亿","100-200亿","200-300亿",">300亿"]:
        xs = by.get(k,[])
        if xs: print(f"  {k:>9s}: n={len(xs):5d}  avg_pnl={mean(xs):.3f}")
    small = mean(by["<50亿"]) if by.get("<50亿") else float("nan")
    rest = [x for k in ["50-100亿","100-200亿","200-300亿",">300亿"] for x in by.get(k,[])]
    rest_m = mean(rest) if rest else float("nan")
    print(f"  小盘-非小盘 价差 = {small - rest_m:.3f}")

# 口径 A: 当前代理 (原样)
def orig(r): return r["bucket"]

# 口径 B: 弱嫌疑 -> 强制 <50亿 (上界)
def move_weak(r):
    if r["ac"]>=ac_p75 and r["ep"]<=ep_p25 and r["ey"] and r["ey"]<=2023:
        return "<50亿"
    return r["bucket"]

# 口径 C: 仅紧贴边界上方 [50,65) 且嫌疑 -> <50亿 (更现实)
def move_boundary(r):
    if r["cm"] is not None and 50<=r["cm"]<65 and r["ac"]>=ac_p75 and r["ep"]<=ep_p25:
        return "<50亿"
    return r["bucket"]

show("A. 当前代理口径 (原样)", orig)
show("B. 上界: 所有弱嫌疑(cap>=P75,price<=P25,year<=2023) 强制归 <50亿", move_weak)
show("C. 现实: 仅[50,65)边界+嫌疑 归 <50亿", move_boundary)

# 也统计各口径下被挪动的交易数
moved_B = sum(1 for r in rows if move_weak(r)=="<50亿" and r["bucket"]!="<50亿")
moved_C = sum(1 for r in rows if move_boundary(r)=="<50亿" and r["bucket"]!="<50亿")
print(f"\n被挪动交易数: B={moved_B} ({100*moved_B/len(rows):.1f}%)  C={moved_C} ({100*moved_C/len(rows):.1f}%)")
