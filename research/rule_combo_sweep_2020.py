# research/rule_combo_sweep_2020.py — 轮动规则零件组合寻优 v2 (2020 至今, 2026-08-24)
# 统一止损语义: 持仓腿从持仓期最高收盘回撤超阈值→全仓黄金;
# 重新进场条件 = 信号曾回到"无风险(全黄金)"至少一天, 之后再次给出风险仓位 (重置基准)。
import akshare as ak

SYMS = {"cyb": "sz159949", "nas": "sh513100", "gold": "sh518880"}
FEE = 0.999
data = {}
for k, s in SYMS.items():
    df = ak.fund_etf_hist_sina(symbol=s)
    data[k] = {str(r["date"])[:10]: float(r["close"]) for _, r in df.iterrows()}
dates = sorted(set(data["cyb"]) & set(data["nas"]) & set(data["gold"]))
dates = [d for d in dates if d >= "2019-01-02"]
C = {k: [data[k][d] for d in dates] for k in SYMS}
n = len(dates)
i0 = next(i for i, d in enumerate(dates) if d >= "2020-01-02")

def iso_week(d):
    from datetime import date as _d
    return _d.fromisoformat(d).isocalendar()[:2]

def met(eq, sw):
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
    m = sum(rets) / len(rets)
    sd = (sum((r - m) ** 2 for r in rets) / len(rets)) ** 0.5
    pk, mdd = 1.0, 0.0
    for v in eq:
        pk = max(pk, v); mdd = min(mdd, v / pk - 1)
    tot = eq[-1] - 1
    yrs = (len(eq) - 1) / 244
    ann = (1 + tot) ** (1 / yrs) - 1
    return dict(tot=tot * 100, ann=ann * 100, mdd=mdd * 100,
                sharpe=(m / sd * 244 ** 0.5) if sd else 0,
                calmar=(ann / abs(mdd)) if mdd else 0, sw=sw)

def ma20_up(i):
    return sum(C["cyb"][i - 19:i + 1]) > sum(C["cyb"][i - 20:i])

def dd250(i):
    return C["cyb"][i] / max(C["cyb"][max(0, i - 249):i + 1]) - 1

def mom(k, i, w=20):
    return C[k][i] / C[k][i - w] - 1

def base_target(sig, i, ctx):
    """→ (cyb_w, nas_w) 风险权重 (其余为黄金)。mom_dual 仅信号日改目标。"""
    if sig == "ma20":
        return (1.0, 0.0) if ma20_up(i) else (0.0, 0.0)
    if sig == "ma20dd":
        return (1.0, 0.0) if (ma20_up(i) and dd250(i) >= -0.2) else (0.0, 0.0)
    if sig == "santai":
        if not ma20_up(i):
            return (0.0, 0.0)
        return (1.0, 0.0) if dd250(i) >= -0.2 else (0.5, 0.0)
    if sig == "mom_single":
        return (1.0, 0.0) if mom("cyb", i) > 0 else (0.0, 0.0)
    if sig == "mom_dual":
        if i == n - 1 or iso_week(dates[i]) != iso_week(dates[i + 1]):
            mC, mN = mom("cyb", i), mom("nas", i)
            ctx["tgt"] = (0.0, 0.0) if (mC <= 0 and mN <= 0) else ((1.0, 0.0) if mC >= mN else (0.0, 1.0))
    return ctx.get("tgt", (0.0, 0.0))

def simulate(sig, stop=0.0):
    eq, sw = [1.0], 0
    ctx = {}
    prev_w = None
    hi = {"cyb": 0.0, "nas": 0.0}
    stopped = False      # 止损后等待"无风险日"出现
    saw_flat = False
    for i in range(i0 + 1, n):
        w = base_target(sig, i, ctx)
        risk = w[0] + w[1] > 0
        # 移动止损 (统一语义)
        if stop and risk and not stopped:
            for leg, lw in (("cyb", w[0]), ("nas", w[1])):
                if lw > 0:
                    hi[leg] = max(hi[leg], C[leg][i])
                    if C[leg][i] < hi[leg] * (1 - stop):
                        stopped, saw_flat = True, False
        if stopped:
            if not risk:
                saw_flat = True
            elif saw_flat:
                stopped = False
                hi = {"cyb": 0.0, "nas": 0.0}     # 重新进场, 重置基准
            else:
                w = (0.0, 0.0)                     # 仍蹲黄金
        ch = prev_w is not None and w != prev_w
        if ch:
            sw += 1
        prev_w = w
        r = (w[0] * (C["cyb"][i] / C["cyb"][i - 1] - 1)
             + w[1] * (C["nas"][i] / C["nas"][i - 1] - 1)
             + (1 - w[0] - w[1]) * (C["gold"][i] / C["gold"][i - 1] - 1))
        eq.append(eq[-1] * (1 + r) * (FEE if ch else 1))
    return met(eq, sw)

def run_hold(k):
    eq = [1.0]
    for i in range(i0 + 1, n):
        eq.append(eq[-1] * C[k][i] / C[k][i - 1])
    return met(eq, 0)

# 两个信号逐日一致率 (解释 v1 里 ma20 与 mom_single 同分疑云)
same = sum(1 for i in range(i0 + 1, n)
           if (ma20_up(i)) == (mom("cyb", i) > 0))
print(f"[校验] MA20方向 与 20日动量>0 逐日一致率: {same / (n - i0 - 1) * 100:.1f}%")

V = [
    ("买入持有创业板50", run_hold("cyb")),
    ("买入持有黄金", run_hold("gold")),
    ("MA20二值", simulate("ma20")),
    ("MA20+回撤刹车 二值", simulate("ma20dd")),
    ("三态 (MA20+回撤+半仓档)", simulate("santai")),
    ("MA20二值 +15%移动止损", simulate("ma20", 0.15)),
    ("MA20+回撤刹车 +15%移动止损", simulate("ma20dd", 0.15)),
    ("三态 +15%移动止损", simulate("santai", 0.15)),
    ("单腿动量 每日", simulate("mom_single")),
    ("单腿动量 每日 +15%移动止损", simulate("mom_single", 0.15)),
    ("双腿动量 周频 (无止损)", simulate("mom_dual")),
    ("双腿动量 周频+15%止损 (现系统)", simulate("mom_dual", 0.15)),
]
print(f"窗口: {dates[i0]} ~ {dates[-1]} ({(n - i0) / 244:.1f} 年) | 成本双边 0.1%")
print(f"{'组合':<34}{'累计':>10}{'年化':>9}{'回撤':>9}{'夏普':>7}{'Calmar':>8}{'换档':>6}")
for name, m in sorted(V, key=lambda x: -x[1]["calmar"]):
    print(f"{name:<34}{m['tot']:>8.1f}%{m['ann']:>8.1f}%{m['mdd']:>8.1f}%{m['sharpe']:>7.2f}{m['calmar']:>8.2f}{m['sw']:>6}")
