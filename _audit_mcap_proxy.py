"""审计 circ_mcap 代理 = 最新流通股本 × 历史入场价 的偏差方向与程度。

代理公式: circ_mcap = active_cap(最新流通股本,万股) * entry_price(历史价) / 10000
真实历史市值 = 历史流通股本 * 历史价  (历史股本取不到)
偏差 = active_cap_latest / active_cap_historical  (>=1, 因股本只增不减)
"""
import sys
import csv
from collections import defaultdict, Counter
from pathlib import Path
from statistics import median, mean

CSV_PATH = Path("output/mcap_analysis/trades_with_mcap.csv")
CUR_YEAR = 2026  # 最新股本对应的大致时点

def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

rows = []
with CSV_PATH.open(encoding="utf-8-sig") as fh:
    for r in csv.DictReader(fh):
        ep = f(r.get("entry_price"))
        ac = f(r.get("active_cap"))
        cm = f(r.get("circ_mcap"))
        tc = f(r.get("total_cap"))
        pp = f(r.get("pnl_pct"))
        ed = r.get("entry_date", "")
        try:
            ey = int(ed[:4])
        except (ValueError, TypeError):
            ey = None
        rows.append({
            "code": r.get("stock_code"),
            "entry_year": ey,
            "entry_price": ep,
            "active_cap": ac,        # 万股
            "total_cap": tc,         # 万股
            "circ_mcap": cm,         # 亿元
            "bucket": r.get("cap_bucket"),
            "pnl_pct": pp,
        })

N = len(rows)
print(f"=== 样本: {N} 笔交易 ===\n")

# ---- 1. 入场年份分布 (偏差暴露面: 越老越偏) ----
yr_cnt = Counter(r["entry_year"] for r in rows if r["entry_year"])
print("=== 1. 入场年份分布 (年份越早 -> 股本增长越多 -> 偏差越大) ===")
for y in sorted(yr_cnt):
    pct = 100 * yr_cnt[y] / N
    age = CUR_YEAR - y
    print(f"  {y}: {yr_cnt[y]:6d} ({pct:5.1f}%)  距今 {age} 年")
pre_2022 = sum(c for y, c in yr_cnt.items() if y and y <= 2021)
pre_2024 = sum(c for y, c in yr_cnt.items() if y and y <= 2023)
print(f"  --> entry_year <= 2021 (>=4年偏差窗口,高风险): {pre_2022} ({100*pre_2022/N:.1f}%)")
print(f"  --> entry_year <= 2023 (>=2年偏差窗口,中风险): {pre_2024} ({100*pre_2024/N:.1f}%)")

# ---- 2. cap_bucket 分布 ----
bk = Counter(r["bucket"] for r in rows)
print("\n=== 2. 市值桶分布 (代理口径) ===")
order = ["<50亿", "50-100亿", "100-200亿", "200-500亿", ">500亿"]
for k in order:
    if k in bk:
        print(f"  {k:>10s}: {bk[k]:6d} ({100*bk[k]/N:5.1f}%)")
for k, c in bk.items():
    if k not in order:
        print(f"  {k:>10s}: {c:6d} ({100*c/N:5.1f}%)")

# ---- 3. 边界非对称性检验 (偏差的直接信号) ----
# 若存在系统性高估, "本应在下桶"的股被推到上桶 -> 紧贴边界上方密度 > 下方
print("\n=== 3. 边界非对称性 (上推信号) ===")
for bnd in [50, 100, 200]:
    below = sum(1 for r in rows if r["circ_mcap"] is not None and bnd - 10 <= r["circ_mcap"] < bnd)
    above = sum(1 for r in rows if r["circ_mcap"] is not None and bnd <= r["circ_mcap"] < bnd + 10)
    ratio = above / below if below else float("inf")
    print(f"  边界 {bnd}亿: [{bnd-10},{bnd})={below}  vs  [{bnd},{bnd+10})={above}  上/下={ratio:.2f}")
print("  (上/下 > 1 表示上方密度更高, 与'下方股被上推'一致)")

# ---- 4. active_cap 分布 + 锁定股本比例 ----
acs = [r["active_cap"] for r in rows if r["active_cap"] and r["active_cap"] > 0]
acs_sorted = sorted(acs)
def pct(p):
    return acs_sorted[int(len(acs_sorted) * p)]
print("\n=== 4. 最新流通股本 active_cap (万股) 分布 ===")
print(f"  P10={pct(0.10):,.0f}  P25={pct(0.25):,.0f}  P50={pct(0.50):,.0f}  P75={pct(0.75):,.0f}  P90={pct(0.90):,.0f}  P99={pct(0.99):,.0f}")

# active/total 比例 (锁定股本占比 = 1 - ratio; 仍大量锁定 -> 未来还会膨胀)
ratios = []
for r in rows:
    if r["active_cap"] and r["total_cap"] and r["total_cap"] > 0:
        ratios.append(r["active_cap"] / r["total_cap"])
ratios.sort()
print(f"  active_cap/total_cap 比 (流通/总股本):")
print(f"    P10={ratios[int(len(ratios)*0.10)]:.2f}  P50={ratios[int(len(ratios)*0.50)]:.2f}  P90={ratios[int(len(ratios)*0.90)]:.2f}")
still_locked = sum(1 for x in ratios if x < 0.7)
print(f"  --> 流通比 < 0.7 (仍有>30%锁定,未来解禁会继续膨胀): {still_locked}/{len(ratios)} ({100*still_locked/len(ratios):.1f}%)")

# ---- 5. 反向 sanity: 高股本+低价 = 历史小盘被高估的信号 ----
print("\n=== 5. 反向 sanity check: 高最新股本 + 低入场价 ===")
ac_p75 = pct(0.75)
ac_p90 = pct(0.90)
eps = [r["entry_price"] for r in rows if r["entry_price"] and r["entry_price"] > 0]
eps.sort()
ep_p25 = eps[int(len(eps) * 0.25)]
ep_p10 = eps[int(len(eps) * 0.10)]
print(f"  entry_price P10={ep_p10:.2f}  P25={ep_p25:.2f}   active_cap P75={ac_p75:,.0f}  P90={ac_p90:,.0f}")

# 强嫌疑: 股本>=P90 且 价格<=P25 且 入场年份较早
strong = [r for r in rows
          if r["active_cap"] and r["active_cap"] >= ac_p90
          and r["entry_price"] and r["entry_price"] <= ep_p25
          and r["entry_year"] and r["entry_year"] <= 2021]
weak = [r for r in rows
        if r["active_cap"] and r["active_cap"] >= ac_p75
        and r["entry_price"] and r["entry_price"] <= ep_p25]
print(f"  强嫌疑 (cap>=P90 & price<=P25 & year<=2021): {len(strong)} ({100*len(strong)/N:.2f}%)")
print(f"  弱嫌疑 (cap>=P75 & price<=P25):           {len(weak)} ({100*len(weak)/N:.2f}%)")
sbk = Counter(r["bucket"] for r in weak)
print(f"  弱嫌疑当前桶分布: {dict(sbk)}")

# ---- 6. 嫌疑交易与正常交易的 pnl 对比 (判断对'小盘效应'结论的扭转) ----
print("\n=== 6. 嫌疑边界交易的 pnl_pct (是否是被挪走的好股) ===")
def avg(xs):
    xs = [x for x in xs if x is not None]
    return mean(xs) if xs else float("nan")
# 紧贴 50亿上方 vs 明确 <50亿 vs 明确 50-100亿
just_above_50 = [r["pnl_pct"] for r in rows if r["circ_mcap"] is not None and 50 <= r["circ_mcap"] < 65]
clear_below = [r["pnl_pct"] for r in rows if r["circ_mcap"] is not None and 30 <= r["circ_mcap"] < 50]
clear_50_100 = [r["pnl_pct"] for r in rows if r["circ_mcap"] is not None and 65 <= r["circ_mcap"] < 100]
print(f"  明确小盘 [30,50)亿:    n={len(clear_below):5d}  均价 pnl={avg(clear_below):.3f}")
print(f"  紧贴上方 [50,65)亿:    n={len(just_above_50):5d}  均价 pnl={avg(just_above_50):.3f}  <-- 若更像小盘则被错分")
print(f"  明确中盘 [65,100)亿:   n={len(clear_50_100):5d}  均价 pnl={avg(clear_50_100):.3f}")

# ---- 7. 各桶均价 pnl (当前结论口径) ----
print("\n=== 7. 各桶均价 pnl_pct (当前代理口径的小盘效应) ===")
by_bucket = defaultdict(list)
for r in rows:
    if r["pnl_pct"] is not None and r["bucket"]:
        by_bucket[r["bucket"]].append(r["pnl_pct"])
for k in order:
    if k in by_bucket:
        xs = by_bucket[k]
        print(f"  {k:>10s}: n={len(xs):5d} 均价={mean(xs):.3f}")

print("\n=== 完成 ===")
