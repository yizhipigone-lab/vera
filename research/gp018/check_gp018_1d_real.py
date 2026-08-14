# -*- coding: utf-8 -*-
"""GUPIAO_018 1D REAL 回测结果核查 (自检 — 搞清 max_hold=26 与 -79% 异常单笔)"""
import pandas as pd

TAG = "gp018_1d_real_2019_2026"
DIR = "research/gp018/results"
df = pd.read_parquet(f"{DIR}/{TAG}_trades.parquet")
df["entry_date"] = pd.to_datetime(df["entry_date"])
df["exit_date"] = pd.to_datetime(df["exit_date"])
df["exit_year"] = df["exit_date"].dt.year
N = len(df)

print("=" * 64)
print(f"总笔数 {N}   sum(pnl)={df['pnl'].sum():.0f} 元 = {df['pnl'].sum()/1e4:.1f} 万")

print("\n[exit_reason 分布]")
g = df.groupby("exit_reason").agg(
    n=("return", "size"),
    avg_ret=("return", "mean"),
    sum_pnl_wan=("pnl", lambda x: x.sum() / 1e4),
    avg_hold=("hold_days", "mean"),
)
g["占%"] = (g["n"] / N * 100).round(1)
print(g.round(4).to_string())

print("\n[hold_days 自然日分位]")
for q in [0.5, 0.75, 0.9, 0.95, 0.99, 1.0]:
    print(f"  q{int(q*100):>3}: {df['hold_days'].quantile(q):.0f} 天")

print("\n[hold_days 最大的 5 笔 — 核查 vs time_stop=12]")
print(df.nlargest(5, "hold_days")[
    ["stock_code", "entry_date", "exit_date", "hold_days", "exit_reason", "return"]
].to_string(index=False))

print("\n[return 最差的 10 笔 — 核查 -79% 异常]")
print(df.nsmallest(10, "return")[
    ["stock_code", "entry_date", "exit_date", "entry_price", "exit_price",
     "exit_reason", "return", "hold_days"]
].to_string(index=False))

n_abn = (df["return"].abs() > 0.15).sum()
print(f"\n[|return|>15% 异常笔数: {n_abn} / {N}  ({n_abn/N*100:.1f}%)]")
if n_abn:
    print("  按原因:")
    print(df[df["return"].abs() > 0.15].groupby("exit_reason").size().to_string())

print("\n[按退出年份]")
gy = df.groupby("exit_year").agg(
    n=("return", "size"),
    sum_pnl_wan=("pnl", lambda x: x.sum() / 1e4),
    avg_ret=("return", "mean"),
    win_rate=("return", lambda x: (x > 0).mean()),
)
print(gy.round(4).to_string())

print("\n[return 分位]")
for q in [0.05, 0.25, 0.5, 0.75, 0.95]:
    print(f"  q{int(q*100):>3}: {df['return'].quantile(q)*100:+.1f}%")
