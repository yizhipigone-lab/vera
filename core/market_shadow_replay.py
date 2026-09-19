# -*- coding: utf-8 -*-
"""core/market_shadow_replay.py — 择时影子回放 (只记录不交易)。

2026-09-19 架构修订批次 5.1 第三刀: 自 core/market_position_runner.py 端出。
三条候选规则 (沪深300>MA20 / 宽度≥50% / 牛熊=牛) 在同一段历史上回放, 用成本、
HAC t 值、持有段、双窗口一致性四把尺子量化 —— **只报告不改交易规则**。
依赖: 共享底座 (core/market_position_io) + core/market_position 的 RET_1Y_BARS。
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from core import market_position_io as mpio
from core.market_position_io import (  # 共享底座原语 (批次 5.1)
    _f,
    _index_series,
    _year_breakdown,
    history,
)
from core.market_position import RET_1Y_BARS
from utils.logger import get_logger

_logger = get_logger(__name__)


#: 三条候选择时规则 (只记录不交易; 定义见 _shadow_states)
SHADOW_RULES = ("ma20", "breadth50", "regime")

#: 持有段数 < 该值 → 不给 t 值, 只给描述统计并标"样本不足" (§16.7 MED-4)
MIN_SEGMENTS_FOR_T = 30

#: 双窗口切分点: 前一半 / 后一半 (复用《公式因子体检方法论》纪律 2「双窗口一致才算数」)。
#: 为什么不是 70/30: 全样本 13.7 年, 70/30 会让后段只剩约 4 年, 而 `regime` 规则
#: 全样本只有 15 个持有段 → 后段约 4 段, 根本判不了。对半切每段仍有 ~6.9 年。
WINDOW_SPLIT = 0.5

#: 默认成本参数进程内缓存一次 (构造空配置引擎只为读三个默认值)
_COST_CACHE: dict = {}

def _cost_params() -> dict | None:
    """项目默认交易成本 —— **现读 `backtest/engine.py`, 不在这里写第二份**。

    单一真相源 = `BacktestEngine.__init__` 里 `config.get("commission"/"stamp_tax"/
    "slippage")` 的默认值 (实测 0.0003 / 0.0005 / 0.001)。构造一个空配置的引擎只读
    这三个数, 结果进程内缓存 (首次约 0.5 秒, 全是 import 开销)。

    **口径 (§16.2)**: 这里给出的 `round_trip` = 佣金×2 + 印花税 (卖出单边)
    = 0.11%, **不含滑点** —— 这是"单次往返至少花掉多少"的**下限**。
    另给 `round_trip_with_slippage` = 再加滑点×2 = 0.31%, 报告里一并披露,
    免得"只计 0.11%"被误读成"成本已经算全了"。

    读失败一律返 None → 上层把净口径标成【缺】, **绝不编一个成本数出来**。
    """
    if "v" in _COST_CACHE:
        return _COST_CACHE["v"]
    v = None
    try:
        from backtest.engine import BacktestEngine
        e = BacktestEngine({})
        comm, tax, slip = float(e.commission), float(e.stamp_tax), float(e.slippage)
        v = {"commission": comm, "stamp_tax": tax, "slippage": slip,
             "round_trip": comm * 2 + tax,
             "round_trip_with_slippage": comm * 2 + tax + slip * 2}
    except Exception as ex:      # fail-soft: 净口径标缺, 不影响毛口径与体温表
        _logger.warning("大盘位置: 读不到默认成本参数, 净口径标缺: %s", ex)
    _COST_CACHE["v"] = v
    return v

def _hac_tstat(x: pd.Series, *, lags: int | None = None) -> dict | None:
    """均值是否显著不为 0 的 **Newey-West (HAC) t 值 + 95% 置信区间**。

    **为什么不能直接用普通 t 检验** (§15.1 E1): 择时规则的日收益**自己跟自己相关**
    (今天持仓, 明天多半还持仓; 空仓日更是一连串的 0)。普通标准误把"天数"当成
    "独立样本数", 会把显著性吹大。Newey-West 用 Bartlett 权重给前 L 阶自协方差
    打折后重算方差, 顺带得到**方差膨胀因子** `vif` 与**有效独立样本数**
    `n_eff = n / vif` —— 这才是"真正独立的信息有多少份"。

    带宽 `lags=None` 时用经验值 `ceil(4·(n/100)^(2/9))` (n=3365 时约 9 阶)。

    返回的 `ci_low/ci_high` 是**日均收益**的区间; 上层乘 `RET_1Y_BARS` 换成"年化
    几个百分点"再展示 (t 值对线性缩放不变, 两种写法同一个数)。
    返回 None = 样本不足 20 天、或序列是常量 (方差 0), 上层标【缺】不硬算。
    """
    v = pd.Series(x).astype(float).dropna()
    n = len(v)
    if n < 20:
        return None
    d = v - float(v.mean())
    g0 = float((d * d).mean())
    # 数值噪声兜底: 常量序列的 g0 可能是 1e-35 而不是精确的 0 (浮点减法残留),
    # 不拦就会算出 t=5.5e7 这种荒唐值。判据 = 方差相对自身量级小到是噪声。
    if not g0 > 1e-12 * max(1.0, float(v.mean()) ** 2):
        return None
    if lags is None:
        lags = int(np.ceil(4.0 * (n / 100.0) ** (2.0 / 9.0)))
    lags = max(0, min(int(lags), n - 2))
    nw = g0
    dn = d.to_numpy()
    for k in range(1, lags + 1):
        gk = float((dn[k:] * dn[:-k]).mean())
        nw += 2.0 * (1.0 - k / (lags + 1.0)) * gk
    nw = max(nw, 1e-18)
    se = (nw / n) ** 0.5
    mean = float(v.mean())
    vif = nw / g0
    return {"mean": mean, "se": se, "t": (mean / se) if se > 0 else None,
            "ci_low": mean - 1.96 * se, "ci_high": mean + 1.96 * se,
            "n": n, "lags": lags, "vif": vif, "n_eff": n / vif}

def _holding_segments(pos: pd.Series) -> list[tuple[int, int, int]]:
    """持仓段 = position 连续为 1 的区间 → `[(起下标, 止下标, 长度), ...]`。

    "建仓一次"算一段 (§16.2 实测: ma20 196 次 / breadth50 181 次 / regime 15 次)。
    **持仓占比 ≠ 换手率**: 前者是"多少天在场内", 后者看的是"翻仓多少次"。
    """
    out: list[tuple[int, int, int]] = []
    start = None
    for i, on in enumerate(pos.to_numpy()):
        if on and start is None:
            start = i
        elif not on and start is not None:
            out.append((start, i - 1, i - start))
            start = None
    if start is not None:
        out.append((start, len(pos) - 1, len(pos) - start))
    return out

def _segment_stats(gross: pd.Series, segs: list[tuple[int, int, int]],
                   cost_rate: float) -> dict:
    """持有段的描述统计 + **每笔下注盈亏**的 t 检验 (§15.1 E2 + §16.7 MED-4)。

    **段收益的定义 (必须写清楚, 否则这些数没法读)**: 段收益 = 段内日收益复合
    (从第 a 天拿到第 b 天), 再扣掉 `cost_rate`。检验的是**这些段收益的均值是否为 0**
    —— 也就是"每开一次仓, 平均是赚还是亏"。

    **为什么不做长度归一化 —— 这是实测出来的坑, 不是个人偏好**: 计划书 §16.7 曾建议
    「段内日均收益」或「折算年化」。实测 (2013-01-04 ~ 2026-09-16, `ma20` 规则 196 段)
    发现这两种归一化会**把结论的符号弄反**:

    | 口径 | 结果 |
    |---|---|
    | 每笔平均盈亏 (不归一化) | **+0.28%** (中位 −0.74%, 胜率 22%) |
    | 段内日均收益 (除以段长) | **−0.39%/天** |
    | 按天数加权的真实日均收益 | **+0.028%/天** |

    原因: 除以段长会给**长段**(赢家)打折, 却不给**短段**(输家)打折 —— 长赢家被稀释、
    短输家原样保留, 均值必然被推成负数。而按天数加权的口径与"每笔口径"同号。
    所以这里**只做每笔口径**; 按天数加权的口径由主表的 HAC t 值承担。

    段数 < `MIN_SEGMENTS_FOR_T` (30) → **不给 t 值**, 只给描述统计, 并写明"样本不足"
    (实测 `regime` 只有 15 段, 硬给一个 t 值就是伪精度)。

    返回的 `t` 是**净口径**(先扣 cost_rate), `t_gross` 是毛口径, 两个一起给,
    方便看出"成本是不是把结论方向改掉了"。
    """
    if not segs:
        return {"n": 0, "note": "没有持仓段"}
    rets_net, rets_gross, days = [], [], []
    for a, b, n in segs:
        g = float((1.0 + gross.iloc[a:b + 1]).prod()) - 1.0
        rets_gross.append(g)
        rets_net.append(g - cost_rate)
        days.append(n)

    def _t(vals) -> float | None:
        if len(vals) < MIN_SEGMENTS_FOR_T:
            return None
        s = pd.Series(vals)
        sd = float(s.std(ddof=1))
        return _f(float(s.mean()) / (sd / len(s) ** 0.5), 2) if sd > 0 else None

    return {"n": len(segs),
            "median_days": _f(pd.Series(days).median(), 0),
            "p90_days": _f(pd.Series(days).quantile(0.9), 0),
            "min_days": int(min(days)), "max_days": int(max(days)),
            "mean_return_pct": _f(pd.Series(rets_net).mean() * 100, 2),
            "median_return_pct": _f(pd.Series(rets_net).median() * 100, 2),
            "win_ratio_pct": _f(sum(1 for r in rets_net if r > 0) / len(rets_net) * 100, 0),
            "t": _t(rets_net), "t_gross": _t(rets_gross),
            "note": "" if len(segs) >= MIN_SEGMENTS_FOR_T else
                    (f"持有段只有 {len(segs)} 个 (<{MIN_SEGMENTS_FOR_T}), "
                     "样本不足, 不给 t 值")}

def shadow_replay() -> dict:
    """三条候选择时规则的假想成绩 (只做研究, 绝不接仓位)。

    **T+1 口径** (项目管理纪律, 2026-08-24 教训): T 日收盘产生的信号,
    T+1 日才生效 —— 实现为把当日状态 shift(1) 后再乘当日收益。
    原"T 日当天生效"口径对高频规则系统性乐观, 本项目已因此得出过一次假结论。
    多空只有满仓/空仓两态 (空仓按 0 收益, 不计无风险利率)。

    **2026-09-17 加厚 (计划书 §15.1 / §16.2 / §16.7)**:

    1. **毛/净并列**。原来只有毛口径 —— 省略成本会把结论方向都改掉: 实测
       `ma20` 与 `breadth50` 每年翻仓约 15 次 (持有中位 4~5 天), 单次往返 ≥0.11%,
       `breadth50` 毛年化 +0.6% **扣费后转负**。
    2. **HAC(Newey-West) t 值 + 95% 置信区间 + 有效样本量**。日收益自相关会让
       普通标准误把显著性吹大; 报 `n_eff` 才知道"真正独立的信息有多少份"。
    3. **按持有段**的显著性 (§15.1 E2), 且处理**段长异质** (§16.7 MED-4) ——
       段数 <30 的规则不给 t 值。
    4. **双窗口** (§15.1 E6): 复用《公式因子体检方法论》**纪律 2「双窗口一致
       才算数」**, 前一半 / 后一半分别出净口径年化, 同号才算"一致"; 不一致的
       结论一律标"待复核"。
    5. 措辞口径 (§15.2 E4): 置信区间含 0 只能写「**无显著净边际**」,
       **不写"无效"、也不写"跑输"**。
    """
    recs = history(limit=0)
    if len(recs) < 250:
        return {"ok": False,
                "reason": f"连续录像只有 {len(recs)} 条, 至少需要 250 条才能回放"}
    hs = _index_series("000300.SH")
    if hs is None:
        return {"ok": False, "reason": "读不到沪深300日线"}
    df = pd.DataFrame(
        {rule: [(r.get("shadow") or {}).get(rule) for r in recs]
         for rule in SHADOW_RULES},
        index=pd.to_datetime([r["date"] for r in recs]))
    df = df[~df.index.duplicated(keep="last")].sort_index()
    # 回放区间 = 指数真有数据的交易日。2026-09-17 实测踩坑: 录像从 2004-02 开始,
    # 而沪深300指数文件最早只到 2013-01, 若把没指数数据的 2174 天也算进年化分母,
    # 「买入持有」会被压成 +2.6% (真实 +4.2%) —— 分母用的是录像条数而非真实交易日。
    # 状态按完整指数日历 ffill (缺录像的日子沿用上一个已知状态), 保证收益不丢天。
    hs = hs.dropna()
    hs = hs[hs > 0]
    if len(hs) < 250:
        return {"ok": False, "reason": "沪深300指数样本不足 250 个交易日"}
    df = df.reindex(hs.index).ffill()
    first_valid = df.dropna(how="all")
    if len(first_valid) < 250:
        return {"ok": False, "reason": "录像与指数日历无足够重叠"}
    df = df[df.index >= first_valid.index[0]]
    ret = hs.pct_change().reindex(df.index).fillna(0.0)
    years = max((df.index[-1] - df.index[0]).days / 365.25, 1e-9)

    from backtest.metrics import MetricsCalculator  # 指标口径不写第二份

    cost = _cost_params()
    rt = float(cost["round_trip"]) if cost else 0.0

    def _stats(series: pd.Series, yrs: float) -> dict:
        equity = (1.0 + series).cumprod()
        n = len(equity)
        if n < 2:
            return {}
        ann = float(equity.iloc[-1]) ** (1.0 / yrs) - 1   # 按真实跨度年化
        mdd = MetricsCalculator.max_drawdown(equity)
        out = {"annualized_pct": _f(ann * 100, 1),
               "max_drawdown_pct": _f(mdd * 100, 1),
               "sharpe": _f(MetricsCalculator.sharpe_ratio(equity), 2),
               "calmar": _f(MetricsCalculator.calmar_ratio(ann, mdd), 2),
               "total_pct": _f((float(equity.iloc[-1]) - 1) * 100, 1)}
        sig = _hac_tstat(series)
        if sig:
            # 置信区间换成"年化几个百分点"展示 (t 值线性缩放不变, 同一个数)
            out.update({"t": _f(sig["t"], 2),
                        "ci_low_pct": _f(sig["ci_low"] * RET_1Y_BARS * 100, 1),
                        "ci_high_pct": _f(sig["ci_high"] * RET_1Y_BARS * 100, 1),
                        "n_eff": _f(sig["n_eff"], 0), "hac_lags": sig["lags"]})
        return out

    def _cost_series(pos: pd.Series) -> pd.Series:
        """逐日摊成本: 买入当天收佣金, 卖出当天收佣金+印花税。

        单次往返合计 = 佣金×2 + 印花税 = 0.11% (项目默认值, **不含滑点**)。
        """
        if not cost:
            return pd.Series(0.0, index=pos.index)
        d = pos.diff()
        buy = (d > 0).astype(float) * float(cost["commission"])
        sell = (d < 0).astype(float) * (float(cost["commission"]) + float(cost["stamp_tax"]))
        return buy + sell

    def _windows(pos: pd.Series, entry_cost: float = 0.0) -> dict:
        """双窗口 (前一半 / 后一半) 的**净口径**成绩 + 是否同向 (§15.1 E6 / §16.3)。

        `entry_cost` = 窗口第一天就另外补一笔成本 (买入持有用: 它在窗口起点建仓,
        不经过 0→1 的跳变, 逐日摊法抓不到那一笔)。

        **每个窗口还要报自己的持有段数**（审计 F-12）：只看"两半年化同不同号"会漏掉
        一件事 —— 对 `regime` 这种全样本只有 15 段的规则，半段可能只有几段，
        同号与否几乎没有信息量。所以段数 < `MIN_SEGMENTS_FOR_T` 的窗口标
        「样本不足」，**同号也不算数**（与单窗口那条纪律一致）。
        """
        cut = int(len(pos) * WINDOW_SPLIT)
        res: dict = {}
        for name, sub in (("in", pos.iloc[:cut]), ("out", pos.iloc[cut:])):
            if len(sub) < 2:
                res[name] = {}
                continue
            yrs = max((sub.index[-1] - sub.index[0]).days / 365.25, 1e-9)
            sub_ret = ret.reindex(sub.index)
            cs = _cost_series(sub)
            if entry_cost and len(cs):
                cs.iloc[0] += entry_cost
            n_seg = len(_holding_segments(sub))
            res[name] = {"start": sub.index[0].date().isoformat(),
                         "end": sub.index[-1].date().isoformat(),
                         "years": _f(yrs, 1),
                         "round_trips": n_seg,
                         "enough": n_seg >= MIN_SEGMENTS_FOR_T,
                         **_stats(sub * sub_ret - cs, yrs)}
        a, b = res["in"].get("annualized_pct"), res["out"].get("annualized_pct")
        both_enough = bool(res["in"].get("enough") and res["out"].get("enough"))
        res["consistent"] = bool(both_enough and a is not None and b is not None
                                 and (a > 0) == (b > 0))
        res["note"] = ("" if both_enough else
                       f"有一半窗口的持有段不足 {MIN_SEGMENTS_FOR_T} 段"
                       f"（前半 {res['in'].get('round_trips')} 段 / "
                       f"后半 {res['out'].get('round_trips')} 段），同号也不算数")
        return res

    rows = []
    for rule in SHADOW_RULES:
        on = (df[rule] == "on")
        pos = on.shift(1, fill_value=False).astype(float)   # T+1 生效
        gross = pos * ret
        segs = _holding_segments(pos)
        st = _stats(gross, years)
        st.update({"rule": rule, "exposure_pct": _f(pos.mean() * 100, 1),
                   "on_days": int(on.sum()), "days": int(len(on)),
                   "round_trips": len(segs),
                   "segments": _segment_stats(gross, segs, rt),
                   "windows": _windows(pos),
                   "net": _stats(gross - _cost_series(pos), years)})
        rows.append(st)

    # 买入持有: 一次性买入 (一个往返), 成本整笔扣在起点 —— 与规则口径一致。
    bh_gross = ret
    bh_net = ret.copy()
    if cost:
        bh_net.iloc[0] = (1.0 + float(ret.iloc[0])) * (1.0 - rt) - 1.0
    bh = _stats(bh_gross, years)
    bh.update({"rule": "buy_hold", "exposure_pct": 100.0,
               "on_days": len(ret), "days": len(ret), "round_trips": 1,
               "segments": _segment_stats(bh_gross, [(0, len(ret) - 1, len(ret))], rt),
               "windows": _windows(pd.Series(1.0, index=ret.index), entry_cost=rt),
               "net": _stats(bh_net, years)})
    caliber = ("信号在**收盘后**产生、**第二天才生效**（不允许拿当天收盘价再当天吃收益，"
               "否则等于抄答案）；空仓的时段按「不赚不赔」算，不计利息；"
               "年化按**真实经过的年份长度**折算（不是拿录像条数当分母）；"
               "**毛/净并列** —— 「毛」不算交易费用，「净」按项目默认费用扣掉，"
               "只扣了佣金和印花税、**没扣滑点**，单次往返 "
               + (f"{rt * 100:.2f}%" if cost else "【缺】") +
               "，所以**净口径的结果是偏乐观的下限**；"
               "显著性用一套「会给自己人跟自己人相关的部分打折」的算法算 t 值"
               "（免得把显著性吹大 —— 相邻日子的涨跌是重叠的，不打折就会虚高），"
               "并同时报「有效独立样本数」")
    out = {"ok": True, "start": df.index[0].date().isoformat(),
           "end": df.index[-1].date().isoformat(),
           "years": _f(years, 1), "days": len(df),
           "rows": rows, "buy_hold": bh,
           "cost": ({"round_trip_pct": _f(rt * 100, 3),
                     "round_trip_with_slippage_pct":
                         _f(float(cost["round_trip_with_slippage"]) * 100, 3),
                     "note": "只计佣金+印花税, 未计滑点; 滑点按项目默认 0.1%/边 另加 0.2%"}
                    if cost else None),
           "window_split": WINDOW_SPLIT,
           "caliber": caliber}
    return out

