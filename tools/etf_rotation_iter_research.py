"""tools/etf_rotation_iter_research.py — ETF 轮动基线诊断与改良迭代研究 (2026-09-16)。

以「资金三份错峰、双风险腿 20 日动量择腿、黄金避险、日频 15% 移动止损」为基线,
按统一口径跑基线 + 多组改良候选:
  - 成交口径: **信号日 T 日收盘算信号, T+1 日开盘价成交** (用户 2026-09-16 拍板:
    调仓以收盘信号、次日开盘执行为主; 与 tools/etf_rotation_freq_backtest.py 的
    T 收盘口径**不可直接比较**, 本脚本全部自洽)。
  - 费用: ETF 佣金万 1/边 (0.0001), 一次换腿 = 卖+买 = 2 边; 无印花税。
  - 数据: 本地日线缓存 (data/kline_cache/1d, **前复权** —— 缓存只支持前复权,
    红利/国债等分红品种的总回报才能算对; 与生产信号用的不复权价口径不同, 报告里声明)。
  - 移动止损判定: 当日收盘 < 持仓期最高价×(1-stop) → T+1 开盘切避险 (生产是 14:54
    实时价判定, 回测用收盘价近似, 报告里声明)。
  - 窗口: 2016-09-16 → 2026-09-15 (与上一轮 10 年研究同窗, 可对照)。

  记账纪律 (2026-09-16 v2 修复隔夜跳空双计):
    T 日收盘决策 → 记「待执行」; T+1 日开盘先给旧持仓算「昨收→今开」隔夜段并扣费换仓,
    再给新持仓算「今开→今收」当日段 —— 同一段跳空只计一次, 净值曲线每点对应当日收盘。

输出: output/etf_rotation_iter/report.json + 汇总表打印。
用法: python tools/etf_rotation_iter_research.py
"""
from __future__ import annotations

import json
import math
import os
import sys

import pandas as pd

ROOT = "data/kline_cache/1d"
OUT_DIR = "output/etf_rotation_iter"
START, END = "2016-09-16", "2026-09-15"
RF_YEAR = 0.015
FEE = 0.0001

CYB, NAS, GOLD = "159949.SZ", "513100.SH", "518880.SH"
DIV, SPX, SZ50 = "510880.SH", "513500.SH", "510050.SH"
HSI, GER = "159920.SZ", "513030.SH"
GOV, GOV10 = "511010.SH", "511260.SH"
CSI300, CYB_IDX = "000300.SH", "399006.SZ"

WEEKDAY_CN = {0: "周一", 1: "周二", 2: "周三", 3: "周四", 4: "周五"}


# ── 数据 ─────────────────────────────────────────────────────────────

def load_ohlc(codes: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(收盘价表, 开盘价表) 同日历, 缺日 ffill (视同停牌)。"""
    closes, opens = {}, {}
    for c in codes:
        df = pd.read_parquet(os.path.join(ROOT, f"{c}.parquet"))
        idx = pd.to_datetime(df["date"])
        closes[c] = pd.Series(df["close"].values, index=idx, name=c)
        opens[c] = pd.Series(df["open"].values, index=idx, name=c)
    cl = pd.DataFrame(closes).sort_index().ffill()
    op = pd.DataFrame(opens).sort_index().ffill()
    return cl, op


def iso_signal_days(dates: pd.DatetimeIndex, anchor: int) -> set:
    """每周最后一个「weekday ≤ 锚定 weekday」的交易日 (复刻生产 _is_signal_day)。"""
    s = pd.Series(dates, index=dates)
    wd = pd.Series(dates.weekday, index=dates)
    eligible = s[wd <= anchor]
    g = eligible.groupby([eligible.index.isocalendar().year,
                          eligible.index.isocalendar().week])
    return set(g.max())


# ── 指标 ─────────────────────────────────────────────────────────────

def metrics_of(eq: pd.Series, switches: int, rts: list[float], name: str) -> dict:
    ret = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / years) - 1
    dd = float((eq / eq.cummax() - 1).min())
    sharpe = ((ret.mean() - RF_YEAR / 252) / ret.std() * math.sqrt(252)
              if ret.std() > 0 else 0.0)
    m_eq = eq.resample("ME").last()
    m_dd = float((m_eq / m_eq.cummax() - 1).min())
    m_ret = m_eq.pct_change().dropna()
    wins = [r for r in rts if r > 0]
    losses = [r for r in rts if r <= 0]
    pl = (sum(wins) / len(wins)) / abs(sum(losses) / len(losses)) \
        if wins and losses else None
    return {
        "name": name,
        "final_equity": round(float(eq.iloc[-1]), 3),
        "cagr": round(cagr, 4),
        "max_dd": round(dd, 4),
        "sharpe": round(sharpe, 2),
        "calmar": round(cagr / abs(dd), 2) if dd < 0 else None,
        "monthly_max_dd": round(m_dd, 4),
        "worst_month": round(float(m_ret.min()), 4) if len(m_ret) else None,
        "switches": switches,
        "risk_roundtrips": len(rts),
        "risk_winrate": round(sum(r > 0 for r in rts) / len(rts), 4) if rts else None,
        "pl_ratio": round(pl, 2) if pl else None,
    }


def yearly_returns(eq: pd.Series) -> dict:
    y = eq.resample("YE").last()
    out = {}
    prev = None
    for d, v in y.items():
        if prev is None:
            out[str(d.year)] = round(float(v / eq.iloc[0] - 1), 4)
        else:
            out[str(d.year)] = round(float(v / prev - 1), 4)
        prev = v
    return out


# ── 单份资金模拟 (决策 T 收盘 → 执行 T+1 开盘) ─────────────────────

def simulate_one(cl: pd.DataFrame, op: pd.DataFrame, cfg: dict,
                 anchor: int, fee: float) -> dict:
    """单个子资金 (一份) 全程模拟。"""
    # 第二轮扩展参数 (默认零行为变化) —— 必须先于信号日计算读取:
    freq = cfg.get("freq", "w")           # w=周频 d=每日 bw=双周 m=月频
    pick_rank = cfg.get("pick_rank", 1)   # 择第 N 强的腿 (默认第 1)
    stop_check = cfg.get("stop_check", "daily")   # daily=每日检查 / signal=仅信号日
    dates = cl.index
    start_i = dates.searchsorted(pd.Timestamp(START))
    end_i = dates.searchsorted(pd.Timestamp(END))
    if freq == "d":
        sig_days = set(dates)
    elif freq == "m":
        # 每月最后一个交易日 (与 anchor 无关)
        s = pd.Series(dates, index=dates)
        sig_days = set(s.groupby([s.index.year, s.index.month]).max())
    elif freq == "bw":
        # 双周频: 只在偶数 ISO 周信号 (相位敏感, 报告里声明)
        sig_days = {d for d in iso_signal_days(dates, anchor)
                    if d.isocalendar()[1] % 2 == 0}
    else:
        sig_days = iso_signal_days(dates, anchor)
    legs = cfg["risk_legs"]
    hedges = cfg["hedge_legs"]
    win = cfg["mom_window"]
    thr = cfg.get("mom_threshold", 0.0)
    stop = cfg["stop_pct"]
    tiers = cfg.get("tiers")
    regime = cfg.get("regime")
    hedge_mode = cfg.get("hedge_mode", "fixed")
    hedge_weights = cfg.get("hedge_weights")

    C = {c: cl[c].values for c in cl.columns}
    O = {c: op[c].values for c in op.columns}

    def mom(i: int, code: str):
        if i < win:
            return None
        b = C[code][i - win]
        return C[code][i] / b - 1.0 if b > 0 else None

    def gate_ok(i: int, leg: str) -> bool:
        if not regime:
            return True
        g = regime["gate_by_leg"].get(leg)
        if g is None:
            return True
        gcode = leg if g == "self" else g
        ma_n = regime["ma"]
        if i < ma_n:
            return False
        ma = sum(C[gcode][i - ma_n + 1:i + 1]) / ma_n
        return C[gcode][i] > ma

    def pick_leg(i: int):
        """→ (腿代码|None=避险, 仓位系数) 或 'keep' (动量全不可算, 不动)。

        pick_rank: 取动量第 N 名且过阈值/闸门的腿; 不足 N 名 → 避险。"""
        valid = {c: m for c in legs if (m := mom(i, c)) is not None}
        if not valid:
            return "keep"
        ordered = sorted(valid, key=valid.get, reverse=True)
        passed = [c for c in ordered if valid[c] > thr and gate_ok(i, c)]
        if len(passed) >= pick_rank:
            c = passed[pick_rank - 1]
            m = valid[c]
            if tiers:
                for lo, frac in tiers:
                    if m >= lo:
                        return c, frac
                return None, 0.0
            return c, 1.0
        return None, 0.0

    def pick_hedge(i: int) -> str:
        if len(hedges) == 1:
            return hedges[0]
        if hedge_mode == "momentum":
            valid = {c: m for c in hedges if (m := mom(i, c)) is not None}
            if valid:
                return max(valid, key=valid.get)
        return hedges[0]

    # 持仓状态: 风险腿代码/仓位系数 + 避险持仓 (fixed 篮子或单码)
    hold_leg = None
    hold_frac = 0.0
    hold_hedge = hedges[0] if hedge_mode == "momentum" else None
    in_cash = True               # 首个信号日前 = 纯现金 (收益 0)
    # fixed 篮子权重: 没给 = 全部押第一个避险码
    eff_weights = (hedge_weights if hedge_weights
                   else [1.0] + [0.0] * (len(hedges) - 1))
    entry_high = 0.0
    equity = 1.0
    curve = []
    switches = 0
    rts: list[float] = []
    rt_entry_eq = None
    pending = None               # (new_leg, new_frac, new_hedge, cause) 待 T+1 开盘执行
    sw_stop = 0                  # 止损触发的换仓次数
    sw_signal = 0                # 信号触发的换仓次数
    sw_log: list[tuple] = []     # 逐笔换仓日志 (debug 用)

    def leg_ret(code: str, i: int, base: str) -> float:
        src = O if base == "open" else C
        return src[code][i] / (C[code][i - 1] if base == "open" else C[code][i - 1]) - 1

    def hedge_ret(i: int, base: str) -> float:
        """避险侧收益 (base='open' → 今开/昨收; base='close' → 今收/昨收)。"""
        if in_cash or cfg.get("hedge_as_cash"):
            return 0.0
        if hedge_mode == "momentum":
            src = O if base == "open" else C
            return src[hold_hedge][i] / C[hold_hedge][i - 1] - 1
        # fixed 篮子: 加权 (连续再平衡, 不计费 —— 报告声明的简化)
        r = 0.0
        for h, w in zip(hedges, eff_weights):
            src = O if base == "open" else C
            r += w * (src[h][i] / C[h][i - 1] - 1)
        return r

    def do_pending_exec(i: int):
        """T+1 开盘执行 pending: 旧持仓隔夜段 → 扣费换仓 → 新持仓当日段。"""
        nonlocal hold_leg, hold_frac, hold_hedge, entry_high, equity, in_cash, switches, rt_entry_eq
        nonlocal sw_stop, sw_signal
        new_leg, new_frac, new_hedge, cause = pending
        # 1) 旧持仓: 昨收 → 今开
        r = (hold_frac * leg_ret(hold_leg, i, "open") if hold_leg else 0.0) \
            + (1 - hold_frac) * hedge_ret(i, "open")
        equity *= (1 + r)
        # 2) 费用 + 往返记录
        if in_cash:
            # 首次建仓 (买风险腿或买避险都算一次)
            changed = True
        else:
            changed = (new_leg != hold_leg) \
                or abs(new_frac - hold_frac) > 1e-9 \
                or (hedge_mode == "momentum" and new_hedge != hold_hedge)
        if changed:
            equity *= (1 - fee) ** 2
            switches += 1
            sw_log.append((str(dates[i].date()), hold_leg, new_leg, cause))
            if cause == "stop":
                sw_stop += 1
            else:
                sw_signal += 1
        if hold_leg is not None and rt_entry_eq is not None and new_leg is None:
            rts.append(equity / rt_entry_eq - 1)
            rt_entry_eq = None
        if new_leg is not None and hold_leg is None:
            rt_entry_eq = equity
        # 3) 换仓
        if new_leg is not None and new_leg != hold_leg:
            entry_high = O[new_leg][i]
        hold_leg, hold_frac = new_leg, new_frac
        if hedge_mode == "momentum":
            hold_hedge = new_hedge
        in_cash = False
        # 4) 新持仓: 今开 → 今收
        if cfg.get("hedge_as_cash"):
            r_hedge = 0.0
        elif hedge_mode == "momentum":
            r_hedge = C[hold_hedge][i] / O[hold_hedge][i] - 1
        else:
            r_hedge = sum(w * (C[h][i] / O[h][i] - 1)
                          for h, w in zip(hedges, eff_weights))
        r = (hold_frac * (C[hold_leg][i] / O[hold_leg][i] - 1) if hold_leg else 0.0) \
            + (1 - hold_frac) * r_hedge
        equity *= (1 + r)

    for i in range(start_i, end_i + 1):
        d = dates[i]
        if pending is not None and i > start_i:
            do_pending_exec(i)
            pending = None
        elif i > start_i and not in_cash:
            # 无换仓日: 昨收 → 今收
            r = (hold_frac * leg_ret(hold_leg, i, "close") if hold_leg else 0.0) \
                + (1 - hold_frac) * hedge_ret(i, "close")
            equity *= (1 + r)

        # 日频移动止损 (收盘判定, 止损优先; stop_check=signal 时仅信号日检查,
        # 但持仓期最高价仍然每日抬 —— 与生产语义一致)
        stopped = False
        if hold_leg is not None:
            entry_high = max(entry_high, C[hold_leg][i])
            if (stop_check == "daily" or d in sig_days) \
                    and C[hold_leg][i] < entry_high * (1 - stop):
                stopped = True

        if i + 1 <= end_i and pending is None:
            if stopped:
                pending = (None, 0.0, pick_hedge(i), "stop")
            elif d in sig_days:
                pick = pick_leg(i)
                if pick != "keep":
                    new_leg, new_frac = pick
                    pending = (new_leg, new_frac, pick_hedge(i), "signal")

        curve.append((d, equity))

    if hold_leg is not None and rt_entry_eq is not None:
        rts.append(equity / rt_entry_eq - 1)
    eq = pd.Series([v for _, v in curve], index=[d for d, _ in curve])
    return {"eq": eq, "switches": switches, "rts": rts,
            "sw_stop": sw_stop, "sw_signal": sw_signal, "sw_log": sw_log}


def run_variant(cl: pd.DataFrame, op: pd.DataFrame, cfg: dict,
                fee: float = FEE) -> dict:
    anchors = cfg.get("anchors", [2, 3, 4])
    tranches = [simulate_one(cl, op, cfg, a, fee) for a in anchors]
    eq = sum(t["eq"] for t in tranches) / len(tranches)
    switches = sum(t["switches"] for t in tranches)
    rts = [r for t in tranches for r in t["rts"]]
    m = metrics_of(eq, switches, rts, cfg["name"])
    m["yearly"] = yearly_returns(eq)
    m["sw_stop"] = sum(t["sw_stop"] for t in tranches)
    m["sw_signal"] = sum(t["sw_signal"] for t in tranches)
    return {"metrics": m, "eq": eq}


# ── 大势分段 ─────────────────────────────────────────────────────────

def regime_split(cl_idx: pd.DataFrame, idx_code: str, eq: pd.Series,
                 ma: int = 200) -> dict:
    """按指数与 200 日均线关系分 牛/震荡/熊, 各段折算年化。

    口径: 先算完整日收益序列, 再按"当日属于哪个环境"筛选 —— 不连续日子之间
    不做跨段差分 (否则把多日收益当单日收益复合, 年化会虚高)。"""
    s = cl_idx[idx_code].reindex(eq.index).ffill()
    ma_s = s.rolling(ma).mean()
    daily_all = eq.pct_change()
    out = {}
    for label, cond in (("牛市(指数>200线+3%)", s > ma_s * 1.03),
                        ("熊市(指数<200线-3%)", s < ma_s * 0.97),
                        ("震荡(±3%带内)", (s >= ma_s * 0.97) & (s <= ma_s * 1.03))):
        mask = cond.reindex(daily_all.index).fillna(False)
        daily = daily_all[mask].dropna()
        if len(daily) < 60:
            out[label] = None
            continue
        wealth = float((1 + daily).prod())
        yrs = len(daily) / 252
        out[label] = round(wealth ** (1 / yrs) - 1, 4) if yrs > 0.2 else None
    return out


# ── 变体清单 ─────────────────────────────────────────────────────────

def variant_list() -> list[dict]:
    base = dict(risk_legs=[CYB, NAS], hedge_legs=[GOLD], hedge_mode="fixed",
                mom_window=20, mom_threshold=0.0, stop_pct=0.15,
                anchors=[2, 3, 4])
    vs = []
    vs.append(dict(base, name="B0 基线(三份·双腿·20日·黄金)"))
    vs.append(dict(base, name="B0x 对照·旧口径(避险期当现金, 对齐旧研究脚本)",
                   hedge_as_cash=True))
    vs.append(dict(base, name="V7 废三份(单份周五)", anchors=[4]))
    vs.append(dict(base, name="V1 多腿(+红利+标普500)",
                   risk_legs=[CYB, NAS, DIV, SPX]))
    vs.append(dict(base, name="V2a 动量10日", mom_window=10))
    vs.append(dict(base, name="V2b 动量60日", mom_window=60))
    vs.append(dict(base, name="V2c 动量阈值2%", mom_threshold=0.02))
    vs.append(dict(base, name="V3a 避险=黄金/国债各半",
                   hedge_legs=[GOLD, GOV], hedge_weights=[0.5, 0.5]))
    vs.append(dict(base, name="V3b 避险择强(黄金vs国债动量)",
                   hedge_legs=[GOLD, GOV], hedge_mode="momentum"))
    vs.append(dict(base, name="V4 大势过滤(A股腿过300闸+海外腿过自身200线)",
                   regime={"gate_by_leg": {CYB: CSI300, NAS: "self"}, "ma": 200}))
    vs.append(dict(base, name="V5 仓位梯度(≥8%满仓/2%~8%半仓/0~2%两成半)",
                   tiers=[(0.08, 1.0), (0.02, 0.5), (0.0, 0.25)]))
    vs.append(dict(base, name="V6a 止损10%", stop_pct=0.10))
    vs.append(dict(base, name="V6b 止损20%", stop_pct=0.20))
    # ── 第二批: 围绕"阈值"与"大势过滤"的组合与微调 ──
    vs.append(dict(base, name="W1 动量阈值1%", mom_threshold=0.01))
    vs.append(dict(base, name="W2 动量阈值1.5%", mom_threshold=0.015))
    vs.append(dict(base, name="W6 动量15日", mom_window=15))
    vs.append(dict(base, name="W3 阈值2%+止损20%", mom_threshold=0.02,
                   stop_pct=0.20))
    vs.append(dict(base, name="W4 大势过滤+阈值2%", mom_threshold=0.02,
                   regime={"gate_by_leg": {CYB: CSI300, NAS: "self"},
                           "ma": 200}))
    vs.append(dict(base, name="W5 大势过滤+阈值1%", mom_threshold=0.01,
                   regime={"gate_by_leg": {CYB: CSI300, NAS: "self"},
                           "ma": 200}))
    return vs


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    codes = sorted({CYB, NAS, GOLD, DIV, SPX, SZ50, HSI, GER, GOV, GOV10,
                    CSI300, CYB_IDX})
    cl, op = load_ohlc(codes)
    cl = cl[:END]
    op = op[:END]
    results = []
    for cfg in variant_list():
        r = run_variant(cl, op, cfg, FEE)
        m = r["metrics"]
        m["regime"] = regime_split(cl, CSI300, r["eq"])
        results.append({"variant": cfg["name"], "metrics": m})
        print(f"{cfg['name']:44s} 年化 {m['cagr'] * 100:6.2f}%  "
              f"回撤 {m['max_dd'] * 100:6.2f}%  夏普 {m['sharpe']:5.2f}  "
              f"卡玛 {str(m['calmar']):>5s}  月内回撤 {m['monthly_max_dd'] * 100:6.2f}%  "
              f"换手 {m['switches']:4d}  胜率 {m['risk_winrate']}  盈亏比 {m['pl_ratio']}")
        r["eq"].to_csv(os.path.join(
            OUT_DIR, f"eq_{cfg['name'].split()[0]}.csv"), header=["equity"])

    sens = {}
    for fee in (0.0, 0.0001, 0.0003):
        r = run_variant(cl, op, variant_list()[0], fee)
        sens.setdefault("B0", {})[str(fee)] = r["metrics"]["cagr"]

    with open(os.path.join(OUT_DIR, "report.json"), "w", encoding="utf-8") as f:
        json.dump({"window": [START, END],
                   "rules": {"exec": "T收盘信号/T+1开盘成交", "fee": FEE,
                              "rf_year": RF_YEAR, "data": "前复权"},
                   "results": results, "fee_sensitivity": sens},
                  f, ensure_ascii=False, indent=2)
    print("\n已写", os.path.join(OUT_DIR, "report.json"))


if __name__ == "__main__":
    main()
