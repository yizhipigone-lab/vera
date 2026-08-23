# research/rule_freq_sweep_2020.py — 信号评估频率寻优 (2020 至今, 2026-08-24)
# 固定: 双腿动量择腿 + 黄金避险 + 换腿成本双边0.1%; 变量: 每 N 个交易日评估一次信号
# 止损语义对齐现系统实盘: 每日检查15%移动止损, 触发切黄金, 下个评估日按新信号可立即回场
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

def simulate(freq, stop=0.15, legs=2):
    """freq: 每 freq 个交易日评估一次; freq=0 表示每周最后一个交易日 (现系统语义)"""
    eq, sw = [1.0], 0
    tgt, hi, last_eval = "gold", 0.0, None
    for i in range(i0 + 1, n):
        if freq == 0:
            eval_day = (i == n - 1) or (iso_week(dates[i]) != iso_week(dates[i + 1]))
        else:
            eval_day = (last_eval is None or i - last_eval >= freq)
        ch = False
        if eval_day:
            last_eval = i
            mC = mom("cyb", i)
            mN = mom("nas", i) if legs == 2 else -1
            nt = "gold" if (mC <= 0 and mN <= 0) else ("cyb" if mC >= mN else "nas")
            if nt != tgt:
                tgt, hi, sw, ch = nt, (C[nt][i] if nt != "gold" else 0.0), sw + 1, True
        if stop and tgt != "gold":
            hi = max(hi, C[tgt][i])
            if C[tgt][i] < hi * (1 - stop):
                tgt, sw, ch = "gold", sw + 1, True
        leg = tgt if tgt != "gold" else "gold"
        eq.append(eq[-1] * (C[leg][i] / C[leg][i - 1]) * (FEE if ch else 1))
    return met(eq, sw)

rows = []
for f_label, f in [("每1天(日频)", 1), ("每2天", 2), ("每3天", 3), ("每4天", 4),
                   ("每5天", 5), ("每周最后交易日(现系统)", 0), ("每7天", 7),
                   ("每10天", 10), ("每20天", 20)]:
    m_stop = simulate(f, 0.15)
    m_nostop = simulate(f, 0.0)
    rows.append((f_label, m_stop, m_nostop))
print(f"窗口: {dates[i0]} ~ {dates[-1]} ({(n - i0) / 244:.1f} 年) | 双腿动量+黄金 | 成本双边0.1%")
print(f"{'评估频率':<22}{'止损累计':>10}{'止损回撤':>9}{'夏普':>7}{'Calmar':>8}{'换腿':>6} | {'无止损累计':>10}{'Calmar':>7}")
for label, a, b in rows:
    print(f"{label:<22}{a['tot']:>8.1f}%{a['mdd']:>8.1f}%{a['sharpe']:>7.2f}{a['calmar']:>8.2f}{a['sw']:>6} | {b['tot']:>9.1f}%{b['calmar']:>7.2f}")
