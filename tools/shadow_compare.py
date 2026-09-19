"""tools/shadow_compare.py — 影子校尺季度对比 (2026-08-23, P0)。

用法: python tools/shadow_compare.py [--days 900] [--edge 10]

做什么:
    从 TDX 拉三只 ETF (创50/纳指/黄金) 前复权日线, 在同一价格序列上重放
    两套规则——影子 MA20 三态 (复用 trade/legacy_three_state.compute_signal) vs
    实跑动量择腿 (复用 trade.rotation.compute_momentum_signal + 周频信号日
    + 日频 15% 移动止损)——换腿成本双边 0.1%, 输出滚动 90 交易日收益对比。
    裁决规则 (2026-08-23 研判报告): 影子滚动 90 日收益连续两个季度
    赢动量超 --edge 个百分点 → 提示人工复审是否换规则。

口径: 规则重放 (同尺对比), 非实盘账户净值; QMT 账户是实盘真相,
本工具回答的是"规则 A 换成规则 B 会不会更好", 不是"实盘赚了多少"。
产物: output/shadow_compare/YYYY-MM-DD_影子校尺对比.md
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CYB, NAS, GOLD = "159949.SZ", "513100.SH", "518880.SH"
FEE = 0.001          # 换腿成本双边合计 0.1% (ETF 免印花税, 与回测口径一致)


def fetch_closes(code: str, days: int) -> dict:
    """日线 {YYYY-MM-DD: close}: TDX 前复权 (主) → 新浪 ETF (备, 通达信未开时)。

    降级语义照 `trade.rotation_feed.IndexFeed.closes` 的 QMT→TDX→腾讯链: 主源挂了用备源,
    同腿内口径一致即可 (规则重放只用自己的相对序列)。

    (2026-09-20 审计 P3-8: 原文指 `rotation._fetch_closes` —— 那是 2026-09-19 批次 4.5
    端出前的旧名, 取数降级链现在住在 `trade/rotation_feed.py`。)"""
    try:
        from core.data_fetcher import DataFetcher
        end = date.today().strftime("%Y%m%d")
        start = (date.today() - timedelta(days=days * 2 + 60)).strftime("%Y%m%d")
        kl = DataFetcher.get_kline([code], start, end, period="1d",
                                   dividend_type="front", use_cache=True)
        s = (kl or {}).get("Close")
        if s is not None and code in s.columns:
            s = s[code].dropna()
            idx = [d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
                   for d in s.index]
            out = dict(zip(idx, [float(x) for x in s.tolist()]))[-days:]
            if len(out) >= 300:
                print(f"[数据] {code} 走 TDX 前复权, {len(out)} 根")
                return out
    except Exception as e:
        print(f"[数据] {code} TDX 不可用 ({type(e).__name__}), 降级新浪")
    import akshare as ak
    num, ex = code.split(".")
    df = ak.fund_etf_hist_sina(symbol=f"{ex.lower()}{num}")
    if df is None or df.empty or "close" not in df.columns:
        raise SystemExit(f"取数失败: {code} (TDX/新浪双挂)")
    df = df.tail(days)
    out = {str(r["date"])[:10]: float(r["close"]) for _, r in df.iterrows()}
    if len(out) < 300:
        raise SystemExit(f"数据不足: {code} 仅 {len(out)} 根")
    print(f"[数据] {code} 走新浪 ETF 日线, {len(out)} 根")
    return out


def iso_week(d: str) -> tuple:
    dt = date.fromisoformat(d)
    return dt.isocalendar()[:2]


def replay(dates, px, i0):
    """同尺重放两套规则, 从 i0 起记账 (warmup 用完整历史前缀, 无截断偏差)。

    返回 (影子权益, 动量权益, 影子换档数, 动量换腿数), 权益起点 i0=1.0,
    对应交易日 dates[i0:]。"""
    from trade.rotation import compute_momentum_signal, compute_signal
    c = {k: [px[k][d] for d in dates] for k in (CYB, NAS, GOLD)}
    # 口径 (2026-08-24 修正): T 日收盘算信号 → 仓位 T+1 生效 (先生成"意图序列",
    # 第 j 天收益吃第 j-1 天意图)。原"当天信号吃当天涨幅"口径对日频策略有
    # 系统性乐观偏差 (交易越勤偷到的当天涨幅越多; 对照: 日频 +17644% → 严格
    # +587%), 且对两套规则的偏袒不均 (日频影子占便宜更多)。修正后与仓库正式
    # 研究脚本 ("t-1 信号 → t 日收益") 同口径。
    intent_s, intent_m = [], []
    tgt, hi, sw_s, sw_m = "gold", 0.0, 0, 0
    for i in range(i0 + 1, len(dates)):
        st = compute_signal(c[CYB][:i + 1])
        state = st["state"] or "full_gold"
        intent_s.append({"full_cyb": (1.0, 0.0), "half": (0.5, 0.5),
                         "full_gold": (0.0, 1.0)}[state])
        is_sig = (i == len(dates) - 1) or iso_week(dates[i]) != iso_week(dates[i + 1])
        if is_sig:
            sig = compute_momentum_signal(
                {CYB: c[CYB][:i + 1], NAS: c[NAS][:i + 1]}, 20)
            nt = sig.get("target") or "gold"
            if nt != tgt:
                tgt, hi, sw_m = nt, (c[nt][i] if nt != "gold" else 0.0), sw_m + 1
        if tgt != "gold":
            hi = max(hi, c[tgt][i])
            if c[tgt][i] < hi * 0.85:
                tgt, sw_m = "gold", sw_m + 1
        intent_m.append(tgt)
    eq_s, eq_m = [1.0], [1.0]
    prev_s, prev_m = None, None
    for j in range(len(intent_s)):
        i = i0 + 1 + j
        eff_s = intent_s[j - 1] if j >= 1 else (0.0, 1.0)
        eff_m = intent_m[j - 1] if j >= 1 else "gold"
        chg_s = prev_s is not None and eff_s != prev_s
        chg_m = prev_m is not None and eff_m != prev_m
        if chg_s:
            sw_s += 1
        prev_s, prev_m = eff_s, eff_m
        r = (eff_s[0] * (c[CYB][i] / c[CYB][i - 1] - 1)
             + eff_s[1] * (c[GOLD][i] / c[GOLD][i - 1] - 1))
        eq_s.append(eq_s[-1] * (1 + r) * (1 - FEE if chg_s else 1))
        leg = eff_m if eff_m != "gold" else GOLD
        eq_m.append(eq_m[-1] * (c[leg][i] / c[leg][i - 1]) * (1 - FEE if chg_m else 1))
    return eq_s, eq_m, sw_s, sw_m


def rolling_ret(eq: list[float], i: int, win: int = 90) -> float:
    j = max(0, i - win)
    return eq[i] / eq[j] - 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=900)
    ap.add_argument("--edge", type=float, default=10.0,
                    help="滚动90日收益差阈值(个百分点), 默认 10")
    args = ap.parse_args()

    px = {k: fetch_closes(k, args.days) for k in (CYB, NAS, GOLD)}
    all_dates = sorted(set(px[CYB]) & set(px[NAS]) & set(px[GOLD]))
    i0 = 250                       # 三态 warmup 对齐点 (前缀仍参与计算)
    eq_s, eq_m, sw_s, sw_m = replay(all_dates, px, i0)
    dates = all_dates[i0:]
    ns, nm = eq_s, eq_m            # 起点已是 i0=1.0

    shadow_log = os.path.join("data", "shadow_rotation.jsonl")
    n_log = 0
    if os.path.exists(shadow_log):
        n_log = sum(1 for ln in open(shadow_log, encoding="utf-8") if ln.strip())

    # 季度末滚动 90 日对比
    rows, streak = [], 0
    quarters = sorted({(d[:4], (int(d[5:7]) - 1) // 3 + 1) for d in dates})
    for y, q in quarters:
        qi = max(i for i, d in enumerate(dates)
                 if d[:4] == y and (int(d[5:7]) - 1) // 3 + 1 == q)
        rs, rm = rolling_ret(ns, qi), rolling_ret(nm, qi)
        diff = (rs - rm) * 100
        win = diff > args.edge
        streak = streak + 1 if win else 0
        rows.append((f"{y}Q{q}", dates[max(0, qi - 90)], dates[qi],
                     rs * 100, rm * 100, diff, "影子+" if win else ""))
    verdict = ("⚠️ 影子连续 %d 个季度滚动90日赢动量超 %.0fpp → 建议人工复审是否换规则"
               % (streak, args.edge)) if streak >= 2 else (
        "✅ 动量规则当前无需复审 (影子未连续两季显著跑赢)")

    lines = [f"# 影子校尺季度对比 ({date.today().isoformat()})",
             "",
             f"- 窗口: {dates[0]} ~ {dates[-1]} ({len(dates)} 交易日, 归一起点=三态 warmup 后)",
             f"- 口径: 同价序列规则重放, 换腿成本双边 {FEE * 100}%, 非实盘账户净值",
             f"- 生产影子 JSONL 已落盘 {n_log} 条 (data/shadow_rotation.jsonl)",
             "",
             f"| 季度 | 90日窗口 | 影子三态 | 实跑动量 | 差(pp) | 标记 |",
             f"|---|---|---|---|---|---|"]
    for q, d0, d1, rs, rm, diff, mark in rows:
        lines.append(f"| {q} | {d0}~{d1} | {rs:+.1f}% | {rm:+.1f}% | {diff:+.1f} | {mark} |")
    lines += ["",
              f"## 裁决",
              "",
              verdict,
              "",
              f"- 全窗口: 影子 {((ns[-1] - 1) * 100):+.1f}% (换档 {sw_s} 次) vs "
              f"动量 {((nm[-1] - 1) * 100):+.1f}% (换腿 {sw_m} 次)",
              "- 提醒: 本对比是规则重放, 实盘差异 (滑点/废单/预算帽) 不在其内"]
    out_dir = os.path.join("output", "shadow_compare")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{date.today().isoformat()}_影子校尺对比.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\n已写入: {out}")


if __name__ == "__main__":
    main()
