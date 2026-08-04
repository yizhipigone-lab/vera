# -*- coding: utf-8 -*-
"""1D vs 5M 对比实验分析 (2026-08-04): 同参数同区间, 找出收益差来源。"""
import sys, glob
import pandas as pd

pd.set_option("display.width", 200)
f1 = sorted(glob.glob("output/reports/CMP_1D对比_*_trades.csv"))[-1]
f5 = sorted(glob.glob("output/reports/CMP_5M对比_*_trades.csv"))[-1]
print("1D:", f1.split("/")[-1])
print("5M:", f5.split("/")[-1])

d1 = pd.read_csv(f1, encoding="utf-8")
d5 = pd.read_csv(f5, encoding="utf-8")

print("\n=== 总量 ===")
for tag, d in [("1D", d1), ("5M", d5)]:
    print(f"{tag}: 笔数={len(d)} 总盈亏={d['pnl'].sum():>12.0f} 均盈亏%={d['profit_pct'].mean()*100:+.2f} "
          f"胜率={(d['pnl']>0).mean()*100:.1f}% 均持仓={d['hold_days'].mean():.1f}天")

print("\n=== 退出原因分布 (笔数 / 平均盈亏% / 总盈亏) ===")
for tag, d in [("1D", d1), ("5M", d5)]:
    g = d.groupby("exit_reason").agg(
        n=("pnl", "size"),
        avg_pct=("profit_pct", lambda s: s.mean() * 100),
        sum_pnl=("pnl", "sum")).sort_values("sum_pnl")
    print(f"--- {tag} ---")
    print(g.to_string(float_format=lambda x: f"{x:,.1f}"))

# ── 按 (股票, 入场日) 配对 ──
d1["entry_day"] = pd.to_datetime(d1["entry_date"]).dt.strftime("%Y-%m-%d")
d5["entry_day"] = pd.to_datetime(d5["entry_date"]).dt.strftime("%Y-%m-%d")
k1 = d1.groupby(["stock_code", "entry_day"]).agg(
    ret1=("profit_pct", "sum"), n1=("pnl", "size"),
    pnl1=("pnl", "sum"), exit1=("exit_reason", "last"),
    exit_date1=("exit_date", "last"), epx1=("entry_price", "first"))
k5 = d5.groupby(["stock_code", "entry_day"]).agg(
    ret5=("profit_pct", "sum"), n5=("pnl", "size"),
    pnl5=("pnl", "sum"), exit5=("exit_reason", "last"),
    exit_date5=("exit_date", "last"), epx5=("entry_price", "first"))
m = k1.join(k5, how="inner")
print(f"\n=== 配对: 共同仓位 {len(m)} 组; 仅1D有 {len(k1)-len(m)}; 仅5M有 {len(k5)-len(m)} ===")
m["ret_diff"] = m["ret5"] - m["ret1"]
m["pnl_diff"] = m["pnl5"] - m["pnl1"]
print(f"配对仓位: 1D平均收益 {m['ret1'].mean()*100:+.2f}%  5M平均收益 {m['ret5'].mean()*100:+.2f}%")
print(f"入场价一致性: 价格不同的仓位组 {(abs(m['epx1']-m['epx5'])>0.001).sum()} / {len(m)}")
print(f"退出原因一致的组 {(m['exit1']==m['exit5']).sum()} / {len(m)}")

print("\n=== 5M 相对 1D 亏损最大的 15 组仓位 ===")
worst = m.nsmallest(15, "pnl_diff")[["ret1", "ret5", "pnl_diff", "exit1", "exit5",
                                     "exit_date1", "exit_date5"]]
print(worst.to_string(float_format=lambda x: f"{x:+.3f}"))

print("\n=== 差异按 (1D退出原因, 5M退出原因) 交叉 ===")
ct = m.groupby(["exit1", "exit5"]).agg(n=("pnl_diff", "size"),
                                       avg_diff_pct=("ret_diff", lambda s: s.mean() * 100),
                                       sum_pnl_diff=("pnl_diff", "sum"))
print(ct.sort_values("sum_pnl_diff").to_string(float_format=lambda x: f"{x:,.1f}"))
