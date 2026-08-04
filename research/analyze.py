"""A/B 分析: 周期分层 + 显著性检验。

输入: research/results/{tag}_{V}_equity.parquet (date, equity, drawdown)
输出: markdown 表格到 stdout + research/results/analysis_{tag}.md
"""
import os, sys, glob
import numpy as np
import pandas as pd

REGIME = "research/signals/index_regime.parquet"


def load_equity(tag, variant):
    p = f"research/results/{tag}_{variant}_equity.parquet"
    if not os.path.exists(p):
        return None
    df = pd.read_parquet(p)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()


def ann_from_equity(eq: pd.Series) -> float:
    if len(eq) < 2:
        return 0.0
    days = (eq.index[-1] - eq.index[0]).days
    if days <= 0 or eq.iloc[0] <= 0:
        return 0.0
    return (eq.iloc[-1] / eq.iloc[0]) ** (365.25 / days) - 1


def maxdd(eq: pd.Series) -> float:
    return float((eq / eq.cummax() - 1).min())


def yearly_returns(eq: pd.Series) -> dict:
    y = eq.resample("YE").last()
    prev = eq.iloc[0]
    out = {}
    for dt, v in y.items():
        out[dt.year] = v / prev - 1
        prev = v
    return out


def regime_table(eq: pd.Series, reg: pd.Series) -> dict:
    df = pd.DataFrame({"eq": eq}).join(reg.rename("regime"), how="left").ffill()
    out = {}
    for r in ["bull", "bear", "range"]:
        sub = df[df["regime"] == r]["eq"]
        if len(sub) > 20:
            rets = sub.pct_change().dropna()
            out[r] = {
                "days": len(sub),
                "ann": float(rets.mean() * 252),
                "dd": maxdd(sub),
                "win": float((rets > 0).mean()),
            }
    return out


def block_bootstrap_diff(eq_a: pd.Series, eq_b: pd.Series, n_boot=2000, block=20, seed=42):
    """A vs B 日收益差的块自助检验。返回 (diff_ann, ci_lo, ci_hi, p_le0)。"""
    ra = eq_a.pct_change().dropna()
    rb = eq_b.pct_change().dropna()
    idx = ra.index.intersection(rb.index)
    d = (ra[idx] - rb[idx]).to_numpy()
    n = len(d)
    if n < 60:
        return None
    rng = np.random.default_rng(seed)
    stats = []
    nblocks = int(np.ceil(n / block))
    for _ in range(n_boot):
        starts = rng.integers(0, n - block, size=nblocks)
        samp = np.concatenate([d[s:s + block] for s in starts])[:n]
        stats.append(samp.mean() * 252)
    stats = np.array(stats)
    return {
        "diff_ann": float(d.mean() * 252),
        "ci": (float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5))),
        "p_le0": float((stats <= 0).mean()),
    }


def main(tag, baseline="V0", out_md=None):
    reg = pd.read_parquet(REGIME).set_index("date")["regime"]
    eqs = {}
    for p in glob.glob(f"research/results/{tag}_*_equity.parquet"):
        v = os.path.basename(p).replace(f"{tag}_", "").replace("_equity.parquet", "")
        e = load_equity(tag, v)
        if e is not None:
            eqs[v] = e["equity"]
    base = eqs.get(baseline)
    lines = [f"# 分析 {tag} (baseline={baseline})", ""]
    lines.append("| 变体 | 年化 | 最大回撤 | 年化 vs 基线 | 逐年收益 |")
    lines.append("|---|---|---|---|---|")
    for v, eq in sorted(eqs.items()):
        ann = ann_from_equity(eq)
        dd = maxdd(eq)
        bann = ann_from_equity(base) if base is not None else np.nan
        rel = (ann / bann - 1) if base is not None and bann else np.nan
        yr = " ".join(f"{y}:{r*100:.0f}%" for y, r in yearly_returns(eq).items())
        lines.append(f"| {v} | {ann*100:.2f}% | {dd*100:.2f}% | {rel*100:+.1f}% | {yr} |")
    lines.append("")
    for v, eq in sorted(eqs.items()):
        lines.append(f"## {v} 周期分层")
        lines.append("| 周期 | 天数 | 年化(日均×252) | 区间内回撤 | 日胜率 |")
        lines.append("|---|---|---|---|---|")
        for r, s in regime_table(eq, reg).items():
            lines.append(f"| {r} | {s['days']} | {s['ann']*100:.1f}% | {s['dd']*100:.1f}% | {s['win']*100:.0f}% |")
        if base is not None and v != baseline:
            bt = block_bootstrap_diff(eq, base)
            if bt:
                lines.append(f"\nvs {baseline} 日收益差年化: {bt['diff_ann']*100:+.2f}%, "
                             f"95%CI [{bt['ci'][0]*100:+.2f}%, {bt['ci'][1]*100:+.2f}%], "
                             f"P(差≤0)={bt['p_le0']:.3f}")
        lines.append("")
    txt = "\n".join(lines)
    print(txt)
    if out_md:
        with open(out_md, "w", encoding="utf-8") as f:
            f.write(txt)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "V0",
         sys.argv[3] if len(sys.argv) > 3 else None)
