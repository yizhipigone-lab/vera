# -*- coding: utf-8 -*-
"""小盘股效应稳健性测试：分桶边界 / 时间段 / 极端值 / 持有期+胜率。
临时脚本，不入库。"""
import sys
import numpy as np
import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CSV = r"e:/1target/VERA/output/mcap_analysis/trades_with_mcap.csv"
SURGE = 50.0  # 暴涨阈值 %


def trimmed_mean(s, trim=0.05):
    s = s.dropna().sort_values().reset_index(drop=True)
    n = len(s)
    if n == 0:
        return np.nan
    k = int(np.floor(n * trim))
    return s.iloc[k:n - k].mean() if (n - 2 * k) > 0 else s.mean()


def stats_block(df, label_col):
    """按 label_col 分组，返回每桶统计表。"""
    g = df.groupby(label_col, observed=True)
    out = pd.DataFrame({
        "笔数": g.size(),
        "平均收益%": g["pnl_pct"].mean(),
        "中位数%": g["pnl_pct"].median(),
        "trimmed5%%": g["pnl_pct"].apply(lambda x: trimmed_mean(x, 0.05)),
        "暴涨占比%": g["pnl_pct"].apply(lambda s: (s > SURGE).mean() * 100),
        "胜率%": g["pnl_pct"].apply(lambda s: (s > 0).mean() * 100),
        "持有天数": g["hold_days"].mean(),
    })
    return out


def fmt(df, floatfmt="{:.2f}".format):
    return df.to_string(float_format=floatfmt)


def main():
    df = pd.read_csv(CSV, encoding="utf-8-sig")
    print(f"原始行数: {len(df)}")
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce")
    df = df.dropna(subset=["pnl_pct", "circ_mcap", "entry_date"]).copy()
    df["year"] = df["entry_date"].dt.year
    print(f"清洗后(pnl_pct/circ_mcap/entry_date 非空): {len(df)}")
    print(f"circ_mcap 范围: {df['circ_mcap'].min():.2f} ~ {df['circ_mcap'].max():.2f} 亿")
    print(f"pnl_pct 范围: {df['pnl_pct'].min():.2f} ~ {df['pnl_pct'].max():.2f}")
    print(f"entry_date 范围: {df['entry_date'].min().date()} ~ {df['entry_date'].max().date()}")
    sp = df[["circ_mcap", "pnl_pct"]].corr(method="spearman").iloc[0, 1]
    print(f"[整体] 市值-收益 Spearman 秩相关: {sp:.4f}  (负=市值越大收益越低)")
    print(f"[整体] 暴涨(>50%)占比: {(df['pnl_pct']>SURGE).mean()*100:.2f}%")
    print()

    # ===== 基准复现：用主结论口径 <30 / 30-50 / 50-100 / 100-200 / 200-300 / 300+ =====
    print("=" * 90)
    print("【基准复现】主结论口径分桶 (验证 <30亿均值7.39%/暴涨2.09% , >300亿均值3.15%/0.27%)")
    bins = [0, 30, 50, 100, 200, 300, df["circ_mcap"].max() + 1]
    labels = ["<30亿", "30-50亿", "50-100亿", "100-200亿", "200-300亿", ">300亿"]
    df["b_base"] = pd.cut(df["circ_mcap"], bins=bins, labels=labels, right=False, include_lowest=True)
    print(fmt(stats_block(df, "b_base")))
    print()

    # ===== Part 1a: 更细分桶 <20/<50/<100/<200/<500/<500+ =====
    print("=" * 90)
    print("【Part 1a】更细分桶 <20 / <50 / <100 / <200 / <500 / 500+ 亿")
    bins_a = [0, 20, 50, 100, 200, 500, df["circ_mcap"].max() + 1]
    labels_a = ["<20亿", "20-50亿", "50-100亿", "100-200亿", "200-500亿", ">500亿"]
    df["b_fine"] = pd.cut(df["circ_mcap"], bins=bins_a, labels=labels_a, right=False, include_lowest=True)
    tb = stats_block(df, "b_fine")
    print(fmt(tb))
    means = tb["平均收益%"].dropna().values
    mono = all(means[i] >= means[i + 1] for i in range(len(means) - 1))
    print(f"  平均收益是否随市值【严格单调递减】: {mono}")
    print(f"  暴涨占比是否随市值递减: {all(tb['暴涨占比%'].dropna().values[i] >= tb['暴涨占比%'].dropna().values[i+1] for i in range(len(tb)-1))}")
    print()

    # ===== Part 1b: 四分位动态分桶 25/50/75/90 =====
    print("=" * 90)
    print("【Part 1b】四分位动态分桶 (按 circ_mcap 的 25/50/75/90 分位数)")
    q = df["circ_mcap"].quantile([.25, .5, .75, .9])
    print(f"  分位数: P25={q.iloc[0]:.1f}亿  P50={q.iloc[1]:.1f}亿  P75={q.iloc[2]:.1f}亿  P90={q.iloc[3]:.1f}亿")
    edges = [0] + list(q.values) + [df["circ_mcap"].max() + 1]
    edges = sorted(set(edges))
    labels_q = [f"Q{i}({edges[i]:.0f}-{edges[i+1]:.0f}亿)" for i in range(len(edges) - 1)]
    df["b_q"] = pd.cut(df["circ_mcap"], bins=edges, labels=labels_q, right=False, include_lowest=True)
    tq = stats_block(df, "b_q")
    print(fmt(tq))
    means_q = tq["平均收益%"].dropna().values
    surge_q = tq["暴涨占比%"].dropna().values
    print(f"  平均收益单调递减: {all(means_q[i] >= means_q[i+1] for i in range(len(means_q)-1))}  (值: {np.round(means_q,2)})")
    print(f"  暴涨占比单调递减: {all(surge_q[i] >= surge_q[i+1] for i in range(len(surge_q)-1))}  (值: {np.round(surge_q,3)})")
    print()

    # ===== Part 2: 时间段稳健性 =====
    print("=" * 90)
    print("【Part 2】时间段稳健性 (每段内 <50亿 vs >300亿 的平均收益 + 暴涨占比)")
    seg_map = {}
    for y in range(2015, 2028):
        if y <= 2020:
            seg_map[y] = "2019-2020"
        elif y <= 2022:
            seg_map[y] = "2021-2022"
        elif y <= 2024:
            seg_map[y] = "2023-2024"
        else:
            seg_map[y] = "2025-2026"
    df["seg"] = df["year"].map(seg_map)
    df_seg = df[df["seg"].notna()].copy()
    # 小盘 vs 大盘 二分
    df_seg["size2"] = np.where(df_seg["circ_mcap"] < 50, "<50亿(小盘)",
                               np.where(df_seg["circ_mcap"] > 300, ">300亿(大盘)", "中盘"))
    print(f"  各段笔数: {df_seg.groupby('seg').size().to_dict()}")
    rows = []
    for seg in ["2019-2020", "2021-2022", "2023-2024", "2025-2026"]:
        d = df_seg[df_seg["seg"] == seg]
        if len(d) == 0:
            continue
        for grp in ["<50亿(小盘)", ">300亿(大盘)"]:
            dd = d[d["size2"] == grp]
            if len(dd) == 0:
                rows.append({"段": seg, "组": grp, "笔数": 0,
                             "平均收益%": np.nan, "中位数%": np.nan,
                             "暴涨占比%": np.nan, "胜率%": np.nan})
            else:
                rows.append({"段": seg, "组": grp, "笔数": len(dd),
                             "平均收益%": dd["pnl_pct"].mean(),
                             "中位数%": dd["pnl_pct"].median(),
                             "暴涨占比%": (dd["pnl_pct"] > SURGE).mean() * 100,
                             "胜率%": (dd["pnl_pct"] > 0).mean() * 100})
    t_seg = pd.DataFrame(rows)
    # 透视：每段小盘-大盘差
    print(t_seg.to_string(index=False, float_format="{:.2f}".format))
    print("  --- 每段 [小盘-大盘] 差值 ---")
    for seg in t_seg["段"].unique():
        small = t_seg[(t_seg["段"] == seg) & (t_seg["组"] == "<50亿(小盘)")]
        big = t_seg[(t_seg["段"] == seg) & (t_seg["组"] == ">300亿(大盘)")]
        if len(small) and len(big):
            dm = small["平均收益%"].values[0] - big["平均收益%"].values[0]
            ds = small["暴涨占比%"].values[0] - big["暴涨占比%"].values[0]
            dw = small["胜率%"].values[0] - big["胜率%"].values[0]
            flag = "OK小盘高" if dm > 0 else "REV大盘高(反转)"
            print(f"  {seg}: 收益差 {dm:+.2f}pp {flag} | 暴涨差 {ds:+.2f}pp | 胜率差 {dw:+.2f}pp")
    print()

    # ===== Part 3: 极端值影响 =====
    print("=" * 90)
    print("【Part 3】极端值影响 (剔除 |pnl_pct|>100% 后; 中位数 + trimmed mean)")
    n_extreme = (df["pnl_pct"].abs() > 100).sum()
    print(f"  极端值(|pnl_pct|>100)笔数: {n_extreme} ({n_extreme/len(df)*100:.2f}%)")
    print(f"  其中 >+100% 的: {(df['pnl_pct']>100).sum()}  <-100% 的: {(df['pnl_pct']<-100).sum()}")
    df_noext = df[df["pnl_pct"].abs() <= 100].copy()
    # 用基准分桶
    print("  --- 剔除极端值后, 主结论口径分桶 ---")
    tb3 = stats_block(df_noext, "b_base")
    print(fmt(tb3))
    # 小盘 vs 大盘 对比 (含/不含极端)
    small = df[df["circ_mcap"] < 30]
    big = df[df["circ_mcap"] > 300]
    small_ne = small[small["pnl_pct"].abs() <= 100]
    big_ne = big[big["pnl_pct"].abs() <= 100]
    print("  --- <30亿 vs >300亿, 含极端 vs 剔极端 ---")
    cmp = pd.DataFrame([
        {"组": "<30亿(全样)", "笔数": len(small),
         "均值": small["pnl_pct"].mean(), "中位数": small["pnl_pct"].median(),
         "trim5%": trimmed_mean(small["pnl_pct"]), "暴涨%": (small["pnl_pct"]>SURGE).mean()*100},
        {"组": "<30亿(剔极)", "笔数": len(small_ne),
         "均值": small_ne["pnl_pct"].mean(), "中位数": small_ne["pnl_pct"].median(),
         "trim5%": trimmed_mean(small_ne["pnl_pct"]), "暴涨%": (small_ne["pnl_pct"]>SURGE).mean()*100},
        {"组": ">300亿(全样)", "笔数": len(big),
         "均值": big["pnl_pct"].mean(), "中位数": big["pnl_pct"].median(),
         "trim5%": trimmed_mean(big["pnl_pct"]), "暴涨%": (big["pnl_pct"]>SURGE).mean()*100},
        {"组": ">300亿(剔极)", "笔数": len(big_ne),
         "均值": big_ne["pnl_pct"].mean(), "中位数": big_ne["pnl_pct"].median(),
         "trim5%": trimmed_mean(big_ne["pnl_pct"]), "暴涨%": (big_ne["pnl_pct"]>SURGE).mean()*100},
    ])
    print(cmp.to_string(index=False, float_format="{:.2f}".format))
    print(f"  剔极端后中位数差(小盘-大盘): {small_ne['pnl_pct'].median()-big_ne['pnl_pct'].median():+.2f}pp")
    print(f"  剔极端后trimmed差(小盘-大盘): {trimmed_mean(small_ne['pnl_pct'])-trimmed_mean(big_ne['pnl_pct']):+.2f}pp")
    print()

    # ===== Part 4: 持有期 / 胜率 =====
    print("=" * 90)
    print("【Part 4】持有期 / 胜率 的小盘效应 (主结论口径分桶)")
    tb4 = stats_block(df, "b_base")[["笔数", "持有天数", "胜率%", "平均收益%", "暴涨占比%"]]
    print(fmt(tb4))
    sp_h = df[["circ_mcap", "hold_days"]].corr(method="spearman").iloc[0, 1]
    sp_w = df[["circ_mcap"]].copy()
    df["_win"] = (df["pnl_pct"] > 0).astype(int)
    sp_w = df[["circ_mcap", "_win"]].corr(method="spearman").iloc[0, 1]
    print(f"  市值-持有天数 Spearman: {sp_h:.4f}  (负=小盘持有更短)")
    print(f"  市值-胜率    Spearman: {sp_w:.4f}  (负=小盘胜率更低)")
    print()

    print("=" * 90)
    print("done")


if __name__ == "__main__":
    main()
