# research/rule_combo_sweep_lag_2020.py — 组合寻优严格口径复跑 (T+1 生效, 2026-08-24)
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
def mom(k, i):
    return C[k][i] / C[k][i - 20] - 1

def intents(sig, stop):
    out, tgt, hi_map = [], "gold", {}
    for i in range(i0 + 1, n):
        if sig == "ma20":
            w = (1.0, 0.0) if ma20_up(i) else (0.0, 0.0)
        elif sig == "ma20dd":
            w = (1.0, 0.0) if (ma20_up(i) and dd250(i) >= -0.2) else (0.0, 0.0)
        elif sig == "santai":
            w = (0.0, 0.0) if not ma20_up(i) else ((1.0, 0.0) if dd250(i) >= -0.2 else (0.5, 0.0))
        else:
            eval_day = True
            if sig == "mom_dual":
                eval_day = (i == n - 1) or iso_week(dates[i]) != iso_week(dates[i + 1])
            if eval_day:
                mC, mN = mom("cyb", i), mom("nas", i)
                nt = "gold" if (mC <= 0 and mN <= 0) else ("cyb" if mC >= mN else "nas")
                if nt != tgt:
                    tgt = nt
                    hi_map = {}
            w = {"gold": (0.0, 0.0), "cyb": (1.0, 0.0), "nas": (0.0, 1.0)}[tgt]
        if stop:
            held = [leg for leg, lw in (("cyb", w[0]), ("nas", w[1])) if lw > 0]
            triggered = False
            for leg in held:
                hi_map[leg] = max(hi_map.get(leg, 0.0), C[leg][i])
                if C[leg][i] < hi_map[leg] * (1 - stop):
                    triggered = True
            if triggered:
                w = (0.0, 0.0)
                hi_map = {}
                if sig in ("mom_dual", "mom_dual_daily"):
                    tgt = "gold"
        out.append(w)
    return out

def apply_lag(ws):
    eq, prev, sw = [1.0], None, 0
    for j in range(len(ws)):
        i = i0 + 1 + j
        eff = ws[j - 1] if j >= 1 else (0.0, 0.0)
        ch = prev is not None and eff != prev
        sw += 1 if ch else 0
        prev = eff
        r = (eff[0] * (C["cyb"][i] / C["cyb"][i - 1] - 1)
             + eff[1] * (C["nas"][i] / C["nas"][i - 1] - 1)
             + (1 - eff[0] - eff[1]) * (C["gold"][i] / C["gold"][i - 1] - 1))
        eq.append(eq[-1] * (1 + r) * (FEE if ch else 1))
    return met(eq, sw)

V = [
    ("MA20二值", intents("ma20", 0)),
    ("MA20二值 +15%止损", intents("ma20", 0.15)),
    ("MA20+回撤刹车 二值", intents("ma20dd", 0)),
    ("三态(MA20+回撤+半仓)", intents("santai", 0)),
    ("三态 +15%止损", intents("santai", 0.15)),
    ("双腿动量 周频+15%止损 (现系统)", intents("mom_dual", 0.15)),
    ("双腿动量 日频+15%止损", intents("mom_dual_daily", 0.15)),
]
rows = sorted(((nm, apply_lag(ws)) for nm, ws in V), key=lambda x: -x[1]["calmar"])
print(f"严格口径 (T日信号 T+1生效) | 窗口 {dates[i0]} ~ {dates[-1]} | 成本0.1%")
print(f"{'组合':<32}{'累计':>10}{'年化':>9}{'回撤':>9}{'夏普':>7}{'Calmar':>8}{'换档':>6}")
for nm, m in rows:
    print(f"{nm:<32}{m['tot']:>8.1f}%{m['ann']:>8.1f}%{m['mdd']:>8.1f}%{m['sharpe']:>7.2f}{m['calmar']:>8.2f}{m['sw']:>6}")
