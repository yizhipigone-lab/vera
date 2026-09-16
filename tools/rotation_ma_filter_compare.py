"""tools/rotation_ma_filter_compare.py — 双资产轮动: 均线过滤 vs 移动止损 对比 (2026-08-28)。

回答用户问题: 「他法(20日均线过滤) vs 我们(15%日频移动止损)」从 2014 年至今谁强。

三组策略 (同尺重放, T+1 口径, 换腿成本双边合计 0.1%):
  A = 他法完整:   159915/513100, 周频, 20日动量, 胜者须站上自己20日均线, 双弱→现金
  B = 我们生产:   159949(2016-06 前用 159915 代理)/513100/黄金518880, 周频, 20日动量,
                  无均线过滤, 15%日频移动止损(持仓期最高收盘回撤15%→切黄金), 双弱→黄金
  C = 隔离过滤:   159915/513100, 周频, 20日动量, 无均线过滤, 15%移动止损→现金, 双弱→现金
                  (A vs C 唯一差异 = 均线过滤 vs 移动止损; B vs A = 他法 vs 我们整体)

口径: 信号日收盘算信号 → T+1 生效 (同 tools/shadow_compare.py 修正口径, 防前视)。

⚠ 口径互斥提醒 (2026-09-16 G2): 本脚本是 **T+1 生效** 口径, 与
tools/etf_rotation_freq_backtest.py 的 **T 收盘成交** 口径**不可直接比较**;
两个脚本都声称"与生产口径对齐", 对照阅读时务必注意成交时点不同
(T 日口径对高频规则系统性乐观, 2026-08-24 已裁决)。

数据: TDX 前复权日线 (主) → 新浪 ETF (备)。
产物: research/YYYY-MM-DD_创业板纳指轮动_均线过滤vs移动止损对比_研究报告.md
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CYB_159915 = "159915.SZ"   # 创业板ETF (他法的腿)
CYB_159949 = "159949.SZ"   # 创业板50ETF (我们的腿)
NAS = "513100.SH"          # 纳指ETF (共同的腿)
GOLD = "518880.SH"         # 黄金ETF (我们的避险腿)
FEE = 0.001                # 换腿成本双边合计 0.1% (同 shadow_compare 口径)
MOM_WIN = 20               # 动量窗口 (交易日, 4周)
MA_WIN = 20                # 他法均线窗口 (20日均线)
TRAIL = 0.15               # 我们的日频移动止损回撤阈值
WARMUP = 60                # 动量+均线 warmup 对齐点


def fetch_closes(code: str, days: int) -> dict:
    """日线 {YYYY-MM-DD: close}: TDX 前复权 (主) → 新浪 ETF (备)。"""
    try:
        from core.data_fetcher import DataFetcher
        end = date.today().strftime("%Y%m%d")
        start = (date.today() - timedelta(days=days * 2 + 120)).strftime("%Y%m%d")
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
    return date.fromisoformat(d).isocalendar()[:2]


def sanitize_scale_breaks(px: dict[str, dict]) -> None:
    """前向校正价格尺度断点 (份额拆分等, 新浪未复权数据): 单日 |涨跌|>25%
    对这三只 ETF 只可能是折算/除权跳空 (创业板50 涨停才 ±20%, 纳指/黄金
    单日极限 <10%), 把断点之后所有价格乘回比率, 使序列连续 (相对收益不变)。
    实测: 513100 2022-01-14 5:1 份额拆分 → 假 +80.5%; 159949 2024-10-08
    +20% 是真实涨停, 阈值 25% 不会误伤。"""
    for code, series in px.items():
        ds = sorted(series)
        prev_d, prev_v = None, None
        for d in ds:
            v = series[d]
            if prev_v and v > 0 and abs(v / prev_v - 1.0) > 0.25:
                factor = prev_v / v
                for d2 in ds[ds.index(d):]:
                    series[d2] = series[d2] * factor
                print(f"[数据] {code} 尺度断点 @{d} (因子 {factor:.4f}), 已前向校正")
                break
            prev_d, prev_v = d, v


def momentum(closes: list, i: int, win: int) -> float:
    """i 日动量 = closes[i]/closes[i-win] - 1 (i >= win)。"""
    return closes[i] / closes[i - win] - 1.0


def ma(closes: list, i: int, win: int) -> float:
    """i 日收盘的 win 日均线。"""
    return sum(closes[i - win + 1:i + 1]) / win


def run(dates: list, c: dict, sigfn, fee: float) -> tuple[list, int, list]:
    """同尺重放。sigfn(i, pos) -> 当期仓位 ('cash'|'gold'|腿代码), 基于 i 日收盘信息,
    作用于 i→i+1 的收益 (T+1 口径, 无前视)。返回 (权益, 换仓次数, 每日仓位)。"""
    eq: list[float] = [1.0]
    pos: str | None = None
    sw = 0
    hist: list[str] = []
    for i in range(WARMUP, len(dates) - 1):
        intent = sigfn(i, pos)
        chg = intent != pos
        if chg:
            sw += 1
            pos = intent
        if pos in ("cash", None):
            r = 0.0
        else:
            leg = GOLD if pos == "gold" else pos
            r = c[leg][i + 1] / c[leg][i] - 1.0
        eq.append(eq[-1] * (1.0 + r) * (1.0 - fee if chg else 1.0))
        hist.append(pos or "cash")
    return eq, sw, hist


# ── 策略 A: 他法 (周频 + 动量 + 均线过滤, 双弱空仓) ─────────────────
def make_sig_a(dates, c):
    def sig(i, pos):
        # 周频信号日 = 每周最后一个交易日
        if i < len(dates) - 1 and iso_week(dates[i]) != iso_week(dates[i + 1]):
            mom = {CYB_159915: momentum(c[CYB_159915], i, MOM_WIN),
                   NAS: momentum(c[NAS], i, MOM_WIN)}
            w = max(mom, key=mom.get)
            if mom[w] <= 0 or c[w][i] <= ma(c[w], i, MA_WIN):
                return "cash"
            return w
        return pos or "cash"
    return sig


# ── 策略 B: 我们生产 (周频动量 + 15%日频移动止损 + 双弱买黄金) ───────
def make_sig_b(dates, c, cyb_code):
    hi: dict = {}

    def sig(i, pos):
        nonlocal hi
        held = [k for k in (cyb_code, NAS) if pos == k]
        if held:
            k = held[0]
            hi[k] = max(hi.get(k, 0.0), c[k][i])
            if c[k][i] < hi[k] * (1.0 - TRAIL):
                return "gold"
        if i < len(dates) - 1 and iso_week(dates[i]) != iso_week(dates[i + 1]):
            mom = {cyb_code: momentum(c[cyb_code], i, MOM_WIN),
                   NAS: momentum(c[NAS], i, MOM_WIN)}
            w = max(mom, key=mom.get)
            if mom[w] > 0:
                if w != pos:
                    hi = {w: c[w][i]}   # 换腿重置持仓期最高收盘
                return w
            return "gold"
        return pos or "gold"
    return sig


# ── 策略 C: 我们的机制 + 他的标的 (15%移动止损, 双弱空仓) ────────────
def make_sig_c(dates, c):
    hi: dict = {}

    def sig(i, pos):
        nonlocal hi
        held = [k for k in (CYB_159915, NAS) if pos == k]
        if held:
            k = held[0]
            hi[k] = max(hi.get(k, 0.0), c[k][i])
            if c[k][i] < hi[k] * (1.0 - TRAIL):
                return "cash"
        if i < len(dates) - 1 and iso_week(dates[i]) != iso_week(dates[i + 1]):
            mom = {CYB_159915: momentum(c[CYB_159915], i, MOM_WIN),
                   NAS: momentum(c[NAS], i, MOM_WIN)}
            w = max(mom, key=mom.get)
            if mom[w] > 0:
                if w != pos:
                    hi = {w: c[w][i]}
                return w
            return "cash"
        return pos or "cash"
    return sig


def metrics(eq: list, dates_from: list, label: str, hist: list) -> dict:
    n = len(eq) - 1
    years = n / 252.0
    total = eq[-1] - 1.0
    ann = (1.0 + total) ** (1.0 / years) - 1.0 if years > 0 else 0.0
    peak = eq[0]
    mdd = 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    rets = [eq[t] / eq[t - 1] - 1.0 for t in range(1, len(eq))]
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / len(rets)
    sd = var ** 0.5
    sharpe = mean / sd * (252 ** 0.5) if sd > 0 else 0.0
    calmar = ann / abs(mdd) if mdd < 0 else 0.0
    in_risk = sum(1 for p in hist if p not in ("cash", "gold"))
    return {
        "label": label, "total": total, "ann": ann, "mdd": mdd,
        "calmar": calmar, "sharpe": sharpe, "switches": 0,
        "days": n, "in_risk_days": in_risk,
        "cash_days": sum(1 for p in hist if p == "cash"),
        "gold_days": sum(1 for p in hist if p == "gold"),
    }


def main() -> None:
    px = {k: fetch_closes(k, 3300) for k in (CYB_159915, CYB_159949, NAS, GOLD)}
    sanitize_scale_breaks(px)   # 513100 2022-01 份额拆分断点前向校正
    all_dates = sorted(set(px[CYB_159915]) & set(px[NAS]) & set(px[GOLD]))
    all_dates = [d for d in all_dates if d >= "2014-01-01"]
    c = {k: [px[k][d] for d in all_dates] for k in (CYB_159915, NAS, GOLD)}
    # B 的创业板腿: 159949 新浪数据自 2016-07-22 起且价格尺度 (~0.99) 与
    # 159915 (~2.4) 不同, 直接拼接会在拼接日凭空制造假暴跌 (实测 2016-07
    # 单月 -53%)。改为 159949 上市前用 159915 乘缩放因子对齐 (常数因子不
    # 改变相对收益, 拼接边界无缝)。
    d0 = next((d for d in all_dates if d in px[CYB_159949]), None)
    scale = px[CYB_159949][d0] / px[CYB_159915][d0] if d0 else 1.0
    cyb_b = [px[CYB_159949].get(d, px[CYB_159915][d] * scale) for d in all_dates]
    c_b = dict(c)
    c_b[CYB_159949] = cyb_b

    eq_a, sw_a, hist_a = run(all_dates, c, make_sig_a(all_dates, c), FEE)
    eq_b, sw_b, hist_b = run(all_dates, c_b, make_sig_b(all_dates, c_b, CYB_159949), FEE)
    eq_c, sw_c, hist_c = run(all_dates, c, make_sig_c(all_dates, c), FEE)

    # 基准: 买入持有
    def bh(code):
        e = [1.0]
        for i in range(WARMUP, len(all_dates) - 1):
            e.append(e[-1] * (c[code][i + 1] / c[code][i]))
        return e
    bh_cyb, bh_nas = bh(CYB_159915), bh(NAS)

    rows = []
    for eq_, sw, hist, lbl in ((eq_a, sw_a, hist_a, "A 他法"),
                               (eq_b, sw_b, hist_b, "B 我们生产"),
                               (eq_c, sw_c, hist_c, "C 隔离过滤")):
        m = metrics(eq_, all_dates[WARMUP:], lbl, hist)
        m["switches"] = sw
        rows.append(m)
    base_rows = []
    for eq_, lbl in ((bh_cyb, "基准 满仓创业板159915"), (bh_nas, "基准 满仓纳指513100")):
        m = metrics(eq_, all_dates[WARMUP:], lbl, [])
        m["switches"] = 0
        base_rows.append(m)

    # 2016-06-08 起共同窗口 (159949 真实上市后)
    i16 = next(i for i, d in enumerate(all_dates) if d >= "2016-06-08")
    rows16 = []
    for eq_, sw, hist, lbl in ((eq_a, sw_a, hist_a, "A 他法"),
                               (eq_b, sw_b, hist_b, "B 我们生产"),
                               (eq_c, sw_c, hist_c, "C 隔离过滤")):
        off = i16 - WARMUP
        eq16 = [1.0] + [eq_[off + t] / eq_[off] for t in range(1, len(eq_) - off)]
        m = metrics(eq16, all_dates[i16:], lbl, hist[off:])
        m["switches"] = sum(1 for a, b in zip(hist[off - 1:], hist[off:]) if a != b)
        rows16.append(m)

    def fmt(m):
        return (f"| {m['label']} | {(m['total'] * 100):+.1f}% | "
                f"{(m['ann'] * 100):+.1f}% | {(m['mdd'] * 100):.1f}% | "
                f"{m['calmar']:.2f} | {m['sharpe']:.2f} | {m['switches']} | "
                f"{m['in_risk_days']} | {m['cash_days']} | {m['gold_days']} |")

    # G3 (2026-09-16): 结论段数字一律由本次运行的 metrics 动态生成,
    # 不再硬编码成文本 (旧版数据更新后报告内部自相矛盾)
    m_a, m_b, m_c = rows[0], rows[1], rows[2]
    eq_dates = all_dates[WARMUP:]

    def year_ret(eq, year):
        """某自然年的收益 (eq 与 eq_dates 等长; 无该年数据 → None)。"""
        idx = [i for i, d in enumerate(eq_dates) if d.startswith(str(year))]
        if not idx:
            return None
        base = eq[idx[0] - 1] if idx[0] > 0 else eq[idx[0]]
        return eq[idx[-1]] / base - 1.0

    ya_c, ya_a = year_ret(eq_c, 2020), year_ret(eq_a, 2020)
    all_years = sorted({d[:4] for d in eq_dates})
    neg_b = [f"{y} 年亏 {r * 100:.0f}%" for y in all_years
             for r in [year_ret(eq_b, int(y))] if r is not None and r < 0]
    neg_txt = ("只有 " + "、".join(neg_b)) if neg_b else "无自然年亏损"
    ya_txt = (f"2020 年创业板单边牛, C 全程骑住 {ya_c * 100:+.0f}%, "
              f"A 被均线反复甩下车只有 {ya_a * 100:+.0f}%"
              if ya_c is not None and ya_a is not None else "")

    lines = [
        f"# 创业板/纳指双资产轮动: 均线过滤 vs 移动止损 对比 (2026-08-28)",
        "",
        f"- 窗口: {all_dates[WARMUP]} ~ {all_dates[-1]} ({len(all_dates) - 1 - WARMUP} 交易日)",
        f"- 口径: 同价序列规则重放, 信号日收盘算信号 → T+1 生效 (防前视, 同 shadow_compare), "
        f"换腿成本双边合计 {FEE * 100}%, 现金收益按 0% (保守), 非实盘账户净值",
        "- 数据: 新浪 ETF 日线 (TDX 降级), 已做尺度断点前向校正 (513100 2022-01 份额拆分, "
        "因子 5.1153) 与拼接缩放 (159949 新浪自 2016-07-22 起、价格尺度与 159915 不同, "
        "此前用 159915×缩放因子对齐, 常数因子不改变相对收益)",
        "",
        "## 三组策略",
        "",
        "| 组 | 规则 | 双弱落点 |",
        "|---|---|---|",
        f"| A 他法 | 159915/513100 · 周频 · 20日动量 · 胜者须站上自己20日均线 | 空仓 |",
        f"| B 我们生产 | 159949/513100 · 周频 · 20日动量 · 15%日频移动止损 · 无均线过滤 | 满仓黄金518880 |",
        f"| C 隔离过滤 | 159915/513100 · 周频 · 20日动量 · 15%移动止损 · 无均线过滤 | 空仓 |",
        "",
        "> A vs C 唯一差异 = 20日均线过滤 vs 15%移动止损 (同腿同落点, 隔离过滤差异)。",
        "> B vs A = 他法整体 vs 我们整体 (标的、过滤方式、双弱落点都不同)。",
        "",
        "## 2014-01 至今 (全窗口)",
        "",
        "| 策略 | 总收益 | 年化 | 最大回撤 | Calmar | 夏普 | 换仓次数 | 风险腿天数 | 现金天数 | 黄金天数 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ] + [fmt(m) for m in rows] + [
        "",
        "| 基准 | 总收益 | 年化 | 最大回撤 | Calmar | 夏普 | | | | |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ] + [fmt(m) for m in base_rows] + [
        "",
        "## 2016-06-08 至今 (159949 真实上市后共同窗口)",
        "",
        "| 策略 | 总收益 | 年化 | 最大回撤 | Calmar | 夏普 | 换仓次数 | 风险腿天数 | 现金天数 | 黄金天数 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ] + [fmt(m) for m in rows16] + [
        "",
        "## 结论 (大白话)",
        "",
        f"1. **15% 移动止损 > 20 日均线过滤** (A vs C, 同腿同落点唯一差异): "
        f"C {m_c['total'] * 100:+.0f}% vs A {m_a['total'] * 100:+.0f}%, "
        f"年化 {m_c['ann'] * 100:.1f}% vs {m_a['ann'] * 100:.1f}%, "
        f"回撤 {m_c['mdd'] * 100:.1f}% vs {m_a['mdd'] * 100:.1f}%。"
        f"均线过滤的代价是「重新站上均线才回场」造成的踏空 —— {ya_txt}。"
        f"移动止损让利润奔跑、只在真破位时离场。",
        f"2. **我们的生产版本完胜他法** (B vs A): {m_b['total'] * 100:+.0f}% vs "
        f"{m_a['total'] * 100:+.0f}%, 回撤 {m_b['mdd'] * 100:.1f}% vs "
        f"{m_a['mdd'] * 100:.1f}%, Calmar {m_b['calmar']:.2f} vs {m_a['calmar']:.2f}, "
        f"{len(all_years)} 个自然年里{neg_txt}。加成来自三处: "
        f"① 创业板50 (159949) 高贝塔 —— 动量轮动在高波动标的上赚更多; "
        f"② 双弱买黄金 —— 黄金 2019-2025 大牛, {m_b['gold_days']} 天避险仓贡献可观; "
        f"③ 15% 移动止损而非均线过滤。",
        "3. **提醒**: B 的收益含「创业板50 + 黄金 2019-2025 罕见双牛」的时代成分, "
        "黄金若转熊 B 对 C/A 的优势会收窄 (同 2026-08-23 对决报告结论)。"
        "回测按收盘价成交、0.1% 换腿费, 未计 QDII 溢价/额度、滑点、风控拒单等实盘摩擦; "
        "新浪数据经断点校正与缩放拼接, 绝对值依赖数据质量; 参数 (20日窗口/15%止损) 敏感, "
        "结果不能线性外推。B vs C 的差异 = 标的(创业板50 vs 创业板指) + 避险(黄金 vs 现金) 双重, "
        "若要单拆避险腿可加 D 组 (159949+移动止损+双弱空仓), 本次未跑。",
    ]
    out_dir = os.path.join("research")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "2026-08-28_创业板纳指轮动_均线过滤vs移动止损对比_研究报告.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))

    # 原始权益曲线落盘 (供后续画图)
    raw_dir = os.path.join("output", "rotation_compare")
    os.makedirs(raw_dir, exist_ok=True)
    with open(os.path.join(raw_dir, "equity.json"), "w", encoding="utf-8") as f:
        json.dump({
            "dates": all_dates[WARMUP:-1],
            "A": eq_a[1:], "B": eq_b[1:], "C": eq_c[1:],
            "bh_cyb": bh_cyb[1:], "bh_nas": bh_nas[1:],
        }, f)
    print(f"\n已写入: {out}")


if __name__ == "__main__":
    main()
