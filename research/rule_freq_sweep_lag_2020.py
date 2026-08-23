# research/rule_freq_sweep_lag_2020.py — 口径对照: T日收盘生效 vs T+1生效 (2026-08-24)
# 问题: 频率寻优里"当天信号吃当天涨幅"是否乐观偏差? 用最严格口径复跑:
# T 日收盘算信号 → 仓位从 T+1 日收益开始生效 (等价于 T+1 开盘-ish  conservative)。
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

def simulate(freq, lag):
    """lag=0: T日信号T日生效(原口径, 偏乐观); lag=1: T日信号T+1生效(严格)。
    lag=1 实现: 决策存 decision[i], 第 i 天收益用 decision[i-1] 的仓位。"""
    # 第一遍: 生成每日决策目标 (含止损), 与 lag 无关的"意图序列"
    intent = []
    tgt, hi, last_eval, sw = "gold", 0.0, None, 0
    for i in range(i0 + 1, n):
        eval_day = (last_eval is None or i - last_eval >= freq)
        if eval_day:
            last_eval = i
            mC, mN = mom("cyb", i), mom("nas", i)
            nt = "gold" if (mC <= 0 and mN <= 0) else ("cyb" if mC >= mN else "nas")
            if nt != tgt:
                tgt, hi, sw = nt, (C[nt][i] if nt != "gold" else 0.0), sw + 1
        if tgt != "gold":
            hi = max(hi, C[tgt][i])
            if C[tgt][i] < hi * 0.85:
                tgt, sw = "gold", sw + 1
        intent.append(tgt)
    # 第二遍: 按 lag 应用
    eq, chg = [1.0], 0
    prev_eff = None
    for j in range(len(intent)):
        eff = intent[j] if lag == 0 else (intent[j - 1] if j >= 1 else "gold")
        i = i0 + 1 + j
        ch = prev_eff is not None and eff != prev_eff
        if ch:
            chg += 1
        prev_eff = eff
        leg = eff if eff != "gold" else "gold"
        eq.append(eq[-1] * (C[leg][i] / C[leg][i - 1]) * (FEE if ch else 1))
    return met(eq, chg)

print(f"窗口: {dates[i0]} ~ {dates[-1]} | 双腿动量+15%止损 | 成本0.1%")
print(f"{'频率':<14}{'T日生效累计':>12}{'Calmar':>8} | {'T+1生效累计':>12}{'Calmar':>8} | {'累计差距':>10}")
for label, f in [("每1天", 1), ("每2天", 2), ("每3天", 3), ("每5天", 5), ("每10天", 10), ("每20天", 20)]:
    a = simulate(f, 0)
    b = simulate(f, 1)
    print(f"{label:<14}{a['tot']:>10.1f}%{a['calmar']:>8.2f} | {b['tot']:>10.1f}%{b['calmar']:>8.2f} | {a['tot'] - b['tot']:>+9.1f}pp")
