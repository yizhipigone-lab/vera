# -*- coding: utf-8 -*-
"""归因四刀 (tdx-strategy-research §3): exit_reason / 持仓天数 / 年度 / 板块前缀。

用法: python -X utf8 research/cmp_1d_5m/attribute.py <trades.csv|parquet>
"""
import sys
import pandas as pd

pd.set_option("display.width", 200)


def load(path):
    if path.endswith(".parquet"):
        return pd.read_parquet(path)
    return pd.read_csv(path, encoding="utf-8")


def main(path):
    d = load(path)
    print(f"trades={len(d)} 总盈亏={d['pnl'].sum():,.0f}")

    print("\n══ 刀1: 退出原因 (找最大出血点) ══")
    g = d.groupby("exit_reason").agg(
        n=("pnl", "size"), avg_pct=("profit_pct", lambda s: s.mean() * 100),
        sum_pnl=("pnl", "sum")).sort_values("sum_pnl")
    print(g.to_string(float_format=lambda x: f"{x:,.1f}"))

    print("\n══ 刀2: 持仓天数分桶 (找利润主区间) ══")
    d["hold_bucket"] = pd.cut(d["hold_days"], [-1, 1, 3, 5, 8, 12, 100],
                              labels=["0-1天", "2-3天", "4-5天", "6-8天", "9-12天", ">12天"])
    g = d.groupby("hold_bucket", observed=True).agg(
        n=("pnl", "size"), avg_pct=("profit_pct", lambda s: s.mean() * 100),
        sum_pnl=("pnl", "sum"))
    print(g.to_string(float_format=lambda x: f"{x:,.1f}"))

    print("\n══ 刀3: 年度 (找衰减段) ══")
    d["year"] = pd.to_datetime(d["entry_date"]).dt.year
    g = d.groupby("year").agg(
        n=("pnl", "size"), avg_pct=("profit_pct", lambda s: s.mean() * 100),
        sum_pnl=("pnl", "sum"))
    print(g.to_string(float_format=lambda x: f"{x:,.1f}"))

    print("\n══ 刀4: 板块前缀 (00深主/30创业/60沪主/68科创/92北交) ══")
    d["board"] = d["stock_code"].str[:2]
    g = d.groupby("board").agg(
        n=("pnl", "size"), avg_pct=("profit_pct", lambda s: s.mean() * 100),
        sum_pnl=("pnl", "sum"))
    print(g.to_string(float_format=lambda x: f"{x:,.1f}"))


if __name__ == "__main__":
    main(sys.argv[1])
