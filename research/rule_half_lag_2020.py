# research/rule_half_lag_2020.py — 半仓变量严格口径(T+1)对跑 (2026-08-24)
# 基线=现系统(双腿周频+15%止损,二值); 变体=强弱不明(|动量差|<3pp)时的三种处理
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

def mom(k, i):
    return C[k][i] / C[k][i - 20] - 1

def intents(mode, th=0.03):
    """mode: bin(现系统) | half_gold(不明→最强腿半仓+黄金) | split(不明→两腿各半) | dodge(不明→全躲)"""
    out, tgt_w, hi_map = [], (0.0, 0.0), {}
    for i in range(i0 + 1, n):
        is_sig = (i == n - 1) or iso_week(dates[i]) != iso_week(dates[i + 1])
        if is_sig:
            mC, mN = mom("cyb", i), mom("nas", i)
            if mC <= 0 and mN <= 0:
                nw = (0.0, 0.0)
            else:
                diff = abs(mC - mN)
                if mode != "bin" and diff < th:
                    if mode == "half_gold":
                        nw = (0.5, 0.0) if mC >= mN else (0.0, 0.5)
                    elif mode == "split":
                        nw = (0.5, 0.5)
                    else:  # dodge
                        nw = (0.0, 0.0)
                else:
                    nw = (1.0, 0.0) if mC >= mN else (0.0, 1.0)
            if nw != tgt_w:
                tgt_w = nw
                hi_map = {}
        # 日频 15% 移动止损: 任一持仓腿触发 → 全撤黄金
        trig = False
        for leg, lw in (("cyb", tgt_w[0]), ("nas", tgt_w[1])):
            if lw > 0:
                hi_map[leg] = max(hi_map.get(leg, 0.0), C[leg][i])
                if C[leg][i] < hi_map[leg] * 0.85:
                    trig = True
        if trig:
            tgt_w, hi_map = (0.0, 0.0), {}
        out.append(tgt_w)
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
    ("现系统(二值, 无半仓)", "bin"),
    ("不明→最强腿半仓+黄金半仓", "half_gold"),
    ("不明→两腿各半", "split"),
    ("不明→全躲黄金", "dodge"),
]
print(f"严格口径(T+1) | 窗口 {dates[i0]} ~ {dates[-1]} | 双腿周频+15%止损 | 不明阈值3pp | 成本0.1%")
print(f"{'变体':<28}{'累计':>10}{'年化':>9}{'回撤':>9}{'夏普':>7}{'Calmar':>8}{'换档':>6}")
rows = sorted(((nm, apply_lag(intents(md))) for nm, md in V), key=lambda x: -x[1]["calmar"])
for nm, m in rows:
    print(f"{nm:<28}{m['tot']:>8.1f}%{m['ann']:>8.1f}%{m['mdd']:>8.1f}%{m['sharpe']:>7.2f}{m['calmar']:>8.2f}{m['sw']:>6}")
# 阈值敏感度 5pp
print()
for md in ["half_gold", "dodge"]:
    m = apply_lag(intents(md, 0.05))
    print(f"阈值5pp {md:<22}{m['tot']:>8.1f}%{m['ann']:>8.1f}%{m['mdd']:>8.1f}%{m['sharpe']:>7.2f}{m['calmar']:>8.2f}{m['sw']:>6}")
