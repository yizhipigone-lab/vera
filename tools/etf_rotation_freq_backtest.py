"""tools/etf_rotation_freq_backtest.py — ETF 轮动「周频择腿 vs 每日双负保险丝」10 年对比回测 (2026-09-16)。

背景: 生产口径 (trade/rotation.py, 2026-08-20 动量改造) = 周五算 20 日动量择腿 + 日频 15%
移动止损。用户提问: 是否应该「每天看两腿动量, 都负就直接切黄金」。本脚本同尺对比三种规则:

  A 周频择腿 (生产复刻): 信号日(每周最后交易日)算动量择腿, 平时只维持 + 日频移动止损
  B 每日动量:          每个交易日都重算动量择腿 (初版 8-14 设计的动量版) + 日频移动止损
  C 折中 (双负保险丝): 周频择腿不变, 非信号日若两腿动量都 ≤ 0 → 当天切黄金; 周五再重新择腿

同尺约定 (与生产口径对齐):
  - 标的: 159949.SZ 创业板50ETF / 513100.SH 纳指ETF (风险腿), 518880.SH 黄金ETF (避险)
  - 动量: close[t] / close[t-20] - 1 (不复权, 与生产 query_daily_closes 同口径)
  - 成交: 信号日 T 收盘成交 (回测铁律; 生产 14:54 实时价近似收盘, 已注明)
  - 移动止损: 持仓期最高收盘 H, 收盘 < H*(1-0.15) → 当日收盘切黄金 (止损优先于信号)
  - 费用: ETF 佣金万 1/边 (0.0001), 一次换腿 = 卖+买 = 2 边; 无印花税; 另跑 0/万3 敏感性
  - 窗口: 2016-09-16 → 2026-09-15 (整 10 年, 159949 上市 2016-07 起全覆盖)

用法: python tools/etf_rotation_freq_backtest.py
"""
from __future__ import annotations

import json
import math
import os
import sys

import pandas as pd

CODES = {"cyb": "159949.SZ", "nas": "513100.SH", "gold": "518880.SH"}
RISK_LEGS = [CODES["cyb"], CODES["nas"]]
GOLD = CODES["gold"]
MOM_WINDOW = 20
STOP_PCT = 0.15
START, END = "2016-09-16", "2026-09-15"
RF_YEAR = 0.015  # 与 backtest/metrics.py 同口径无风险利率

# 本地 parquet 末尾之后的最近收盘 (2026-09-16 取自腾讯日线, 已与缓存重叠日核验一致, 不复权)
PATCH = {
    "159949.SZ": {"2026-09-11": 1.566, "2026-09-14": 1.542, "2026-09-15": 1.519},
    "513100.SH": {"2026-09-11": 2.201, "2026-09-14": 2.191, "2026-09-15": 2.196},
    "518880.SH": {
        "2026-08-28": 9.475, "2026-08-31": 9.135, "2026-09-01": 9.118,
        "2026-09-02": 8.902, "2026-09-03": 9.105, "2026-09-04": 9.164,
        "2026-09-07": 9.024, "2026-09-08": 9.047, "2026-09-09": 9.041,
        "2026-09-10": 9.083, "2026-09-11": 8.943, "2026-09-14": 8.875,
        "2026-09-15": 8.829,
    },
}


def load_closes(root: str = "data/kline_cache/1d") -> pd.DataFrame:
    """三只 ETF 不复权收盘, 并上共同交易日历, 缺的日子用前一收盘填 (视同停牌)。"""
    frames = {}
    for code in CODES.values():
        df = pd.read_parquet(os.path.join(root, f"{code}.parquet"))
        s = pd.Series(df["close"].values,
                      index=pd.to_datetime(df["date"]), name=code)
        for d, v in PATCH.get(code, {}).items():
            s[pd.Timestamp(d)] = v
        frames[code] = s.sort_index()
    px = pd.DataFrame(frames).sort_index().ffill().dropna()
    return px


def iso_signal_days(dates: pd.DatetimeIndex, anchor: int = 4) -> set:
    """信号日 = 每周最后一个「weekday ≤ 锚定 weekday」的交易日
    (复刻 _is_signal_day: anchor 4=周五=全周最后交易日; 2=周三=周一~周三里的最后交易日)。"""
    s = pd.Series(dates, index=dates)
    wd = pd.Series(dates.weekday, index=dates)
    eligible = s[wd <= anchor]
    g = eligible.groupby([eligible.index.isocalendar().year,
                          eligible.index.isocalendar().week])
    return set(g.max())


def simulate(px: pd.DataFrame, variant: str, fee: float, anchor: int = 4) -> dict:
    """variant ∈ {A 周频, B 每日动量, C 折中}; fee = 单边费率; anchor = 信号日锚定 weekday。
    T 收盘成交。"""
    dates = px.index
    sig_days = iso_signal_days(dates, anchor)
    close = {c: px[c].values for c in px.columns}

    def mom(i: int, code: str):
        if i < MOM_WINDOW:
            return None
        base = close[code][i - MOM_WINDOW]
        return close[code][i] / base - 1.0 if base > 0 else None

    def pick(i: int):
        """动量择腿: 最高且 >0 的腿, 否则 None(黄金); 全 None → 'keep' 不动 (生产 fail-safe)。"""
        valid = {c: m for c in RISK_LEGS if (m := mom(i, c)) is not None}
        if not valid:
            return "keep"
        best = max(valid, key=valid.get)
        return best if valid[best] > 0 else None

    holding = None          # None=空仓(起始), 'GOLD'=黄金, 或风险腿代码
    entry_high = 0.0
    equity, curve = 1.0, []
    switches = 0
    legs_roundtrips, cur_leg_entry_eq = [], None
    prev_close = None

    start_i = dates.searchsorted(pd.Timestamp(START))
    end_i = dates.searchsorted(pd.Timestamp(END))

    for i in range(start_i, end_i + 1):
        d = dates[i]
        # 1) 当日持仓涨跌 (按旧持仓计价到今日收盘)
        if holding == "GOLD":
            equity *= close[GOLD][i] / prev_close[GOLD]
        elif holding in RISK_LEGS:
            equity *= close[holding][i] / prev_close[holding]

        # 2) 日频移动止损 (止损优先, 复刻 _execute: stop 触发则今日 target=黄金)
        stopped = False
        if holding in RISK_LEGS:
            entry_high = max(entry_high, close[holding][i])
            if close[holding][i] < entry_high * (1 - STOP_PCT):
                target, stopped = "GOLD", True
            else:
                target = "keep"
        else:
            target = "keep"

        # 3) 信号 (未触发止损时)
        if not stopped:
            if variant == "B":
                target = pick(i)
            elif d in sig_days:                       # A/C 信号日: 完整择腿
                target = pick(i)
            elif variant == "C":                      # 折中: 非信号日双负保险丝
                m = {c: mom(i, c) for c in RISK_LEGS}
                if all(v is not None for v in m.values()) and all(
                        v <= 0 for v in m.values()):
                    target = "GOLD"

        # 4) 执行 (T 收盘换腿, 双边费用)
        tgt = None if target in (None,) else target
        if target == "keep":
            tgt = holding
        tgt_code = GOLD if tgt == "GOLD" else tgt
        if tgt_code != holding:
            equity *= (1 - fee) ** 2
            switches += 1
            if holding in RISK_LEGS and cur_leg_entry_eq is not None:
                legs_roundtrips.append(equity > cur_leg_entry_eq)
                cur_leg_entry_eq = None
            if tgt_code in RISK_LEGS:
                entry_high = close[tgt_code][i]
                cur_leg_entry_eq = equity
            holding = tgt_code

        prev_close = {c: close[c][i] for c in px.columns}
        curve.append((d, equity))

    if holding in RISK_LEGS and cur_leg_entry_eq is not None:
        legs_roundtrips.append(equity > cur_leg_entry_eq)

    eq = pd.Series([v for _, v in curve], index=[d for d, _ in curve])
    ret = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / years) - 1
    dd = (eq / eq.cummax() - 1).min()
    sharpe = ((ret.mean() - RF_YEAR / 252) / ret.std() * math.sqrt(252)
              if ret.std() > 0 else 0.0)
    return {
        "variant": variant, "fee": fee, "anchor": anchor,
        "final_equity": round(float(eq.iloc[-1]), 4),
        "cagr": round(cagr, 4), "max_dd": round(float(dd), 4),
        "sharpe": round(sharpe, 2),
        "calmar": round(cagr / abs(dd), 2) if dd < 0 else None,
        "switches": switches,
        "risk_roundtrips": len(legs_roundtrips),
        "risk_winrate": (round(sum(legs_roundtrips) / len(legs_roundtrips), 4)
                         if legs_roundtrips else None),
        "rt_list": legs_roundtrips,
        "eq": eq,
    }


def combine(tranches: list[dict], fee: float, name: str) -> dict:
    """多条子资金曲线等权合成一个组合 (每条起始净值 1, 组合净值 = 均值)。"""
    eq = sum(t["eq"] for t in tranches) / len(tranches)
    ret = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / years) - 1
    dd = (eq / eq.cummax() - 1).min()
    sharpe = ((ret.mean() - RF_YEAR / 252) / ret.std() * math.sqrt(252)
              if ret.std() > 0 else 0.0)
    rts = [x for t in tranches for x in t["rt_list"]]
    return {"variant": "S", "fee": fee, "name": name,
            "final_equity": round(float(eq.iloc[-1]), 4),
            "cagr": round(cagr, 4), "max_dd": round(float(dd), 4),
            "sharpe": round(sharpe, 2),
            "calmar": round(cagr / abs(dd), 2) if dd < 0 else None,
            "switches": sum(t["switches"] for t in tranches),
            "risk_roundtrips": len(rts),
            "risk_winrate": round(sum(rts) / len(rts), 4) if rts else None,
            "eq": eq}


def buy_hold(px: pd.DataFrame, code: str, fee: float) -> dict:
    s = px[code][START:END]
    eq = s / s.iloc[0] * (1 - fee)
    ret = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / years) - 1
    dd = (eq / eq.cummax() - 1).min()
    sharpe = ((ret.mean() - RF_YEAR / 252) / ret.std() * math.sqrt(252)
              if ret.std() > 0 else 0.0)
    return {"code": code, "final_equity": round(float(eq.iloc[-1]), 4),
            "cagr": round(cagr, 4), "max_dd": round(float(dd), 4),
            "sharpe": round(sharpe, 2),
            "calmar": round(cagr / abs(dd), 2) if dd < 0 else None}


def yearly_returns(eq: pd.Series) -> dict:
    y = eq.resample("YE").last()
    y = y.pct_change()
    y.iloc[0] = eq.resample("YE").last().iloc[0] / eq.iloc[0] - 1
    return {str(d.year): round(float(v), 4) for d, v in y.items()}


def main() -> None:
    px = load_closes()
    px = px[:END]
    out = {"window": [START, END], "rules": {
        "momentum_window": MOM_WINDOW, "stop_pct": STOP_PCT,
        "exec": "T 收盘", "rf_year": RF_YEAR}}

    WD = {0: "周一", 1: "周二", 2: "周三", 3: "周四", 4: "周五"}

    for fee in (0.0, 0.0001, 0.0003):
        rows = []
        for v, name in (("A", "周频择腿(生产)"), ("B", "每日动量"),
                        ("C", "折中:双负保险丝")):
            r = simulate(px, v, fee)
            r["name"] = name
            r["yearly"] = yearly_returns(r["eq"])
            r.pop("eq"); r.pop("rt_list")
            rows.append(r)
        # 错峰方案: 资金等分, 各份独立跑周频规则, 信号日错开
        d3 = combine([simulate(px, "A", fee, anchor=a) for a in (2, 3, 4)],
                     fee, "三份错峰 周三/四/五")
        d3["yearly"] = yearly_returns(d3.pop("eq"))
        d5 = combine([simulate(px, "A", fee, anchor=a) for a in range(5)],
                     fee, "五份错峰 周一~周五")
        d5["yearly"] = yearly_returns(d5.pop("eq"))
        rows.extend([d3, d5])
        out[f"fee_{fee}"] = rows

    # 单锚定日对照 (费 万1): 看「全押某一个 weekday」的运气差
    out["anchor_solo"] = []
    for a in range(5):
        r = simulate(px, "A", 0.0001, anchor=a)
        r["name"] = f"全押{WD[a]}信号"
        r.pop("eq"); r.pop("rt_list")
        out["anchor_solo"].append(r)

    out["buy_hold"] = [buy_hold(px, c, 0.0001) for c in CODES.values()]

    os.makedirs("output/etf_rotation_freq", exist_ok=True)
    path = "output/etf_rotation_freq/report.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)

    def _row(r):
        return (f"{r['name']:<18} 净值 {r['final_equity']:>7.3f}  "
                f"年化 {r['cagr']*100:>7.2f}%  回撤 {r['max_dd']*100:>7.2f}%  "
                f"夏普 {r['sharpe']:>5.2f}  Calmar {r['calmar']:>5.2f}  "
                f"换腿 {r['switches']:>3} 次  "
                f"胜率 {(r['risk_winrate'] or 0)*100:>5.1f}%")

    print(f"\n== 主对比 (费 万1 单边, {START}→{END}) ==")
    for r in out["fee_0.0001"]:
        print(_row(r))
    print("\n== 单锚定日对照: 全押某 weekday 的运气差 (费 万1) ==")
    for r in out["anchor_solo"]:
        print(_row(r))
    print("\n== 买入持有参照 ==")
    for b in out["buy_hold"]:
        print(f"{b['code']:<10} 净值 {b['final_equity']:>7.3f}  "
              f"年化 {b['cagr']*100:>7.2f}%  回撤 {b['max_dd']*100:>7.2f}%  "
              f"夏普 {b['sharpe']:>5.2f}  Calmar {b['calmar']:>5.2f}")
    print("\n== 逐年收益 (费 万1) ==")
    for r in out["fee_0.0001"]:
        print(r["name"], json.dumps(r["yearly"], ensure_ascii=False))
    print("\n== 费用敏感性 (年化) ==")
    for fee in (0.0, 0.0001, 0.0003):
        line = "  ".join(f"{r['name'].split('(')[0].split(':')[0]} {r['cagr']*100:+.2f}%"
                         for r in out[f"fee_{fee}"])
        print(f"费 {fee*1e4:>4.1f}/万: {line}")
    print(f"\nJSON 已写 {path}")


if __name__ == "__main__":
    sys.exit(main())
