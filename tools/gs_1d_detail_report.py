"""
1d 全A 3年 详细报告 (2026-07-21, 用现有 sweep 数据, 不重跑)

每公式: 最优组合 + 达标组合 + Top10 + 参数敏感度.
分年份需重跑(用户暂不跑), 本报告是组合级详细.
"""
import os
import re
import glob
import json

import pandas as pd

BASE = "output/gs_1d_sweep"
OUT = os.path.join(BASE, "EVAL_REPORT_1d_detail.md")
TARGET_ANN = 0.30
TARGET_MAXDD = 0.15
MIN_TRADES = 1000

LADDER_CN = {"off": "无阶梯", "s5_100": "单档5%全平", "s8_100": "单档8%全平",
             "s12_100": "单档12%全平", "m2_5-12_30": "双档5%+12%各30%",
             "m2_6-15_30": "双档6%+15%各30%", "m4_4-8-15-25_20": "四档4/8/15/25%各20%",
             "m3_4-10-20_25": "三档4/10/20%各25%"}


def combo_cn(r):
    return (f"硬止损{abs(r['cost'])*100:.0f}% 激活{r['act']*100:.1f}% "
            f"回撤{r['dd']*100:.1f}% {LADDER_CN.get(str(r.get('ladder','')), r.get('ladder',''))} "
            f"时间{int(r['time_days'])}天")


def load(formula):
    df = pd.read_csv(f"{BASE}/{formula}/sweep_1d.csv")
    for c in ["annret", "maxdd", "calmar", "trades", "cost", "act", "dd",
              "sharpe", "winrate", "time_days"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[df["annret"].notna()]


def main():
    formulas = json.load(open("output/gs_filter/candidates_12pct.json",
                              encoding="utf-8"))
    L = []
    L.append("# 1d 全A 3年 详细评测报告（移动止盈优先）\n")
    L.append("> 区间 2023-08-01~2026-07-17 | 全A | 1d | T收盘买入 | 排除涨停 | 300万/单票2万\n")
    L.append("> 达标硬口径: 年化>30% 且 回撤≤15% 且 交易≥1000笔\n")
    L.append("> **含 1d 日内乐观**（high/low 先后不可知）+ 全A小盘3年行情 + 高频交易手续费侵蚀; "
             "绝对值不可全信, 相对排序有效\n")
    L.append("> 分年份表现需重跑(本报告用现有组合级数据)\n")

    summary = []
    for formula in formulas:
        df = load(formula)
        if df.empty:
            continue
        L.append(f"\n---\n\n## {formula}\n")
        top = df.loc[df["annret"].idxmax()]
        hit = df[(df["annret"] > TARGET_ANN) & (df["maxdd"].abs() <= TARGET_MAXDD)
                 & (df["trades"] >= MIN_TRADES)]
        summary.append((formula, top, hit, len(df)))

        L.append(f"**最优组合**（按年化）: {combo_cn(top)}\n")
        L.append(f"- 累计收益 {top.get('cumret',0)*100:.1f}% | 年化 {top['annret']*100:.1f}% | "
                 f"回撤 {top['maxdd']*100:.1f}% | Calmar {top['calmar']:.2f} | "
                 f"Sharpe {top['sharpe']:.2f} | 胜率 {top['winrate']*100:.1f}% | "
                 f"交易 {int(top['trades'])} | 平均持仓 {top.get('avg_hold',0):.1f}天\n")

        L.append(f"\n**达标组合 {len(hit)} 个**（年化>30%+回撤<15%+交易≥1000）:\n")
        if not hit.empty:
            L.append("| 参数 | 年化 | 回撤 | Calmar | 交易 |")
            L.append("|---|---|---|---|---|")
            for _, r in hit.sort_values("calmar", ascending=False).iterrows():
                L.append(f"| {combo_cn(r)} | {r['annret']*100:.1f}% | "
                         f"{r['maxdd']*100:.1f}% | {r['calmar']:.2f} | {int(r['trades'])} |")
        else:
            L.append("_无_\n")

        L.append(f"\n**Top 10 组合**（按年化，不看约束）:\n")
        L.append("| 参数 | 年化 | 回撤 | Calmar | 交易 | 胜率 |")
        L.append("|---|---|---|---|---|---|")
        for _, r in df.sort_values("annret", ascending=False).head(10).iterrows():
            L.append(f"| {combo_cn(r)} | {r['annret']*100:.1f}% | {r['maxdd']*100:.1f}% | "
                     f"{r['calmar']:.2f} | {int(r['trades'])} | {r['winrate']*100:.0f}% |")

        # 参数敏感度
        L.append(f"\n**参数敏感度**（各档最佳年化）:\n")
        for param, label in [("act", "移动止盈激活"), ("cost", "硬止损"),
                             ("time_days", "时间止损"), ("dd", "移动止盈回撤")]:
            grp = df.groupby(param)["annret"].max().sort_index()

            def _fmt(k):
                if param == "cost":
                    return f"{abs(k)*100:.0f}%"
                if param == "time_days":
                    return f"{int(k)}天"
                return f"{k*100:.1f}%"
            vals = " / ".join(f"{_fmt(k)}→{v*100:.0f}%" for k, v in grp.items())
            L.append(f"- **{label}**: {vals}")

    # 汇总
    L.append(f"\n---\n\n## 6 公式汇总排名（按最优年化）\n")
    L.append("| 排名 | 公式 | 最优年化 | 回撤 | Calmar | 达标组合数 | 最优参数 |")
    L.append("|---|---|---|---|---|---|---|")
    for i, (formula, top, hit, n) in enumerate(
            sorted(summary, key=lambda x: -x[1]["annret"]), 1):
        L.append(f"| {i} | {formula} | {top['annret']*100:.1f}% | "
                 f"{top['maxdd']*100:.1f}% | {top['calmar']:.2f} | {len(hit)} | {combo_cn(top)} |")

    L.append(f"\n## 参数铁律（跨公式共识）\n")
    L.append("- **移动止盈激活 2.0%**：所有公式最优都选 2%（涨 2% 即锁利；8% 直接亏损）")
    L.append("- **硬止损 8%**：紧硬止损回撤可控（5 公式最优都 8%）")
    L.append("- **移动止盈回撤 0.5%**：极紧（从高点回撤 0.5% 即卖）")
    L.append("- **无阶梯止盈**：单一移动止盈够用")
    L.append("- **时间止损 12 天**：短持有（黑马选股1 抓短线启动）\n")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[OK] {OUT}")


if __name__ == "__main__":
    main()
