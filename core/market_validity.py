# -*- coding: utf-8 -*-
"""core/market_validity.py — 指标体检 (哪些指标真有预测力)

2026-09-19 架构修订批次 5.1 第二刀: 自 core/market_position_runner.py 端出。
依赖只有共享底座 core/market_position_io (路径/原语) + core/market_erp (_erp_table)。
"""
from __future__ import annotations

import datetime as dt
import warnings

import numpy as np
import pandas as pd

from core import market_position_io as mpio
from core.market_position_io import (  # 共享底座原语 (批次 5.1)
    _f,
    _features_frame,
    _index_series,
    history,
)
from core.market_position import forward_return
from utils.logger import get_logger

_logger = get_logger(__name__)


from core.market_erp import _erp_table  # noqa: F401  (体检的估值维度用它)


#: 体检的持有期: (交易日数, 中文名)
VALIDITY_HORIZONS = ((21, "1 个月"), (63, "3 个月"), (126, "6 个月"), (252, "12 个月"))

#: 体检的指标: (特征名, 中文名, **族**) —— 族决定"算几份独立证据"(纪律 3)
VALIDITY_FIELDS = (
    ("sh_pct", "上证十年百分位", "价格位置族"),
    ("hs300_pct", "沪深300 十年百分位", "价格位置族"),
    ("above_ma20_pct", "站上 20 日均线占比", "宽度族"),
    ("hl_spread_pct", "创新高与新低的差", "宽度族"),
    ("amount_pct_1y", "成交额一年百分位", "量能族"),
    ("vol_ann_20", "20 日年化波动", "波动族"),
    ("erp", "股债性价比(ERP)", "估值族"),
)

#: 每月至少要有多少个月的样本才做体检 (少于这个数就是伪精度)
VALIDITY_MIN_MONTHS = 36

#: 一个月 ≈ 多少个交易日 (把持有期的交易日数换成"月数", 算有效独立样本用)
BARS_PER_MONTH = 21.0

def _spearman(x: list[float], y: list[float]) -> tuple[float | None, float | None]:
    """Spearman 秩相关 + p 值。scipy 不可用时**只给 rho, p 值返 None**(不编 p)。

    为什么用 Spearman 而不是 Pearson: 这些指标与收益的关系明显非线性
    (位置极高与极低都可能反转), 秩相关对异常值稳健, 也是外部研究用的口径。
    """
    if len(x) < 8 or len(x) != len(y):
        return None, None
    try:
        import warnings

        from scipy.stats import spearmanr
        with warnings.catch_warnings():
            # 常量输入时 scipy 会警告并返回 NaN —— 那是"无法定义", 不是错误
            warnings.simplefilter("ignore")
            r = spearmanr(x, y)
        rho, p = float(r.statistic), float(r.pvalue)
        if rho != rho or p != p:
            return None, None
        return rho, p
    except Exception:                 # scipy 缺失 → 自己算 rho (秩的 Pearson), 不给 p
        a = pd.Series(x).rank().to_numpy()
        b = pd.Series(y).rank().to_numpy()
        if a.std() == 0 or b.std() == 0:
            return None, None
        return float(np.corrcoef(a, b)[0, 1]), None

def _quintile_spread(vals: list[float], fwds: list[float]) -> float | None:
    """把指标从小到大分五组, 算「最高一组 − 最低一组」之后平均收益差 (百分点)。

    样本不足 25 个月**不给数** —— 5 组各 5 个点以下的分位差没有意义。
    """
    if len(vals) < 25 or len(vals) != len(fwds):
        return None
    try:
        g = pd.qcut(pd.Series(vals), 5, labels=False, duplicates="drop")
    except Exception:
        return None
    if g.nunique() < 5:
        return None
    f = pd.Series(fwds)
    hi, lo = f[g == 4].mean(), f[g == 0].mean()
    return _f(hi - lo, 2) if hi == hi and lo == lo else None

def _dimension_validity() -> dict:
    """**维度体检**: 每个现有指标 vs 未来 1/3/6/12 个月收益, 到底有没有相关性。

    这是计划书 §14.7「最高优先」那一节, 也是决定"该留哪些指标、该补什么维度"的
    唯一数据依据。**它回答的问题**: 我这套大盘指标里, 有哪一维被验证过能预测收益?

    三条纪律 (抄 `docs/公式因子体检方法论.md`, 不自创):
      1. **月频采样**: 日频观测的"有效独立样本"只有个位数 (§16.1),
         按 2200 个日频观测报 p<0.001 是**虚构精度**;
      2. **双窗口一致才算数**: 月频样本对半切, 两半同号才算数, 否则标"待复核";
      3. **数族不数因子**: 同族指标只算 1 份独立证据。
    """
    recs = history(limit=0)
    hist = _features_frame(recs)
    if len(hist) < 500:
        return {"ok": False, "reason": f"连续录像只有 {len(recs)} 条, 维度体检至少要 500 条"}
    hs = _index_series("000300.SH")
    if hs is None:
        return {"ok": False, "reason": "读不到沪深300日线"}
    hist.index = pd.to_datetime(hist.index)
    erp = _erp_table()
    if len(erp):                       # 没有 ERP 缓存就不并这一列 (不拿别的列冒充)
        hist = hist.join(erp[["erp"]], how="left")
    if "erp" not in hist.columns:
        hist["erp"] = np.nan
    # **月频采样**: 每月取月末最后一个有数据的交易日
    mon = hist.resample("ME").last()
    mon = mon.dropna(how="all")
    n_months = int(len(mon))
    if n_months < VALIDITY_MIN_MONTHS:
        return {"ok": False,
                "reason": f"月频样本只有 {n_months} 个月, 至少需要 {VALIDITY_MIN_MONTHS} 个月"}

    # 前向收益: **复用 forward_return**, 不写第二份收益定义
    fwd = {k: [forward_return(hs, d, k) for d in mon.index] for k, _ in VALIDITY_HORIZONS}
    rows = []
    for field, name, family in VALIDITY_FIELDS:
        if field not in mon.columns:
            continue
        for k, kcn in VALIDITY_HORIZONS:
            pairs = [(v, f) for v, f in zip(mon[field].tolist(), fwd[k])
                     if v == v and f is not None]
            if len(pairs) < VALIDITY_MIN_MONTHS // 2:
                continue
            xs = [float(p[0]) for p in pairs]
            ys = [float(p[1]) for p in pairs]
            rho, p = _spearman(xs, ys)
            # 双窗口切分必须**按该指标的可用样本**切, 不能按整张表切
            # (2026-09-17 实测踩到: 用全局 n//2 切, 短历史的指标前段就吃掉全部样本、
            #  后段为空 → rho_out 恒 None → 所有显著项都被误判成"两半不一致")
            half = len(pairs) // 2
            rho_i, _ = _spearman(xs[:half], ys[:half])
            rho_o, _ = _spearman(xs[half:], ys[half:])
            consistent = (rho_i is not None and rho_o is not None
                          and (rho_i > 0) == (rho_o > 0))
            if rho is None:
                verdict = "算不了"
            elif p is None or p >= 0.05:
                verdict = "看不出相关性"
            elif not consistent:
                verdict = "样本内相关但两半不一致 → 待复核"
            else:
                verdict = "样本内可用（正向）" if rho > 0 else "样本内可用（反向）"
            rows.append({"field": field, "name": name, "family": family,
                         "horizon_days": k, "horizon": kcn,
                         "n": len(pairs),
                         # **月频采样**的有效独立样本 = 月数 ÷ 持有期月数
                         # (不是 ÷ 持有期交易日数: 观测间隔本身就是一个月)
                         "n_eff": _f(len(pairs) / (k / BARS_PER_MONTH), 1),
                         "rho": _f(rho, 3) if rho is not None else None,
                         "p": _f(p, 4) if p is not None else None,
                         "rho_in": _f(rho_i, 3) if rho_i is not None else None,
                         "rho_out": _f(rho_o, 3) if rho_o is not None else None,
                         "consistent": consistent,
                         "quintile_spread_pct": _quintile_spread(xs, ys),
                         "verdict": verdict})
    if not rows:
        return {"ok": False, "reason": "没有任何指标有足够的月频样本"}
    n_tests = len(rows)
    # 每个指标取"最能说明问题"的那一行 (优先 12 个月, 没有就取最长的) 作为总结论
    summary = []
    for field, name, family in VALIDITY_FIELDS:
        mine = [r for r in rows if r["field"] == field]
        if not mine:
            continue
        best = max(mine, key=lambda r: r["horizon_days"])
        strong = [r for r in mine
                  if r["p"] is not None and r["p"] < 0.05 and r["consistent"]]
        summary.append({
            "field": field, "name": name, "family": family,
            "rho_12m": best["rho"], "p_12m": best["p"],
            "n_12m": best["n"],
            #: 五分位差与"是哪个持有期"都取自**同一行** `best`。
            #: 2026-09-17 M7：正文原来写死 `by_field[field][252]`，而 `best` 是
            #: "优先 12 个月、没有就取最长的" —— 两者不是同一行时，正文会出现
            #: "12 个月 rho +0.99、五分位差算不出来"这种自相矛盾的组合。
            "spread": best.get("quintile_spread_pct"),
            "best_horizon": best["horizon"],
            "verdict_12m": best["verdict"],
            "any_significant_consistent": bool(strong),
            "significant_horizons": [r["horizon"] for r in strong],
            "label": (("可用（样本内，在 " + "、".join(r["horizon"] for r in strong)
                       + " 上显著且两半一致）") if strong
                      else "仅描述现状，不作预测依据")})
    families = {}
    for s in summary:
        f = families.setdefault(s["family"], {"n_fields": 0, "usable": False})
        f["n_fields"] += 1
        f["usable"] = f["usable"] or s["any_significant_consistent"]
    #: 取**实际有结果的最长持有期**（不是写死的 12 个月）—— 录像短的时候 12 个月那一档
    #: 可能一行都没有，写死就会印出「各指标落在 个位数」这种半截话。
    _present = sorted({r["horizon_days"] for r in rows}, reverse=True)
    longest = _present[0] if _present else max(k for k, _ in VALIDITY_HORIZONS)
    longest_cn = dict(VALIDITY_HORIZONS).get(longest, f"{longest} 个交易日")
    at_longest = [r for r in rows if r["horizon_days"] == longest]
    eff = [r["n_eff"] for r in at_longest if r["n_eff"] is not None]
    months_n = [r["n"] for r in at_longest if r["n"] is not None]
    thinnest = min(at_longest, key=lambda r: r["n"], default=None)
    widest = max(at_longest, key=lambda r: r["n"], default=None)
    usable_fams = sorted(f for f, v in families.items() if v["usable"])
    _eff_sentence = (
        f"**{longest_cn}持有期的有效独立样本，各指标落在 {min(eff)} ~ {max(eff)} 份**"
        if eff else
        f"**{longest_cn}持有期在本机数据上还凑不出有效样本**（录像不够长）")
    return {"ok": True, "n_months": n_months,
            "start": mon.index[0].date().isoformat(),
            "end": mon.index[-1].date().isoformat(),
            "rows": rows, "summary": summary,
            "n_tests": n_tests, "n_fields": len(summary),
            "n_families": len(families), "n_families_usable": len(usable_fams),
            "usable_families": usable_fams, "families": families,
            "limitations": [
                "**幸存者偏差是满格的**（实测 2026-09-17：本地日线缓存 5211 只股票里，"
                "最后交易日早于 2026-08-01 的有 **0 只**）—— 缓存里一只退市股都没有。"
                "而这些票当年通常是弱票，所以历史宽度序列被**系统性高估**，"
                "用宽度类指标算出来的相关性都建立在这条被污染的序列上。",
                f"**月频采样得 {n_months} 个月（{mon.index[0].date()} ~ "
                f"{mon.index[-1].date()}），但各指标历史长短不同**："
                + (f"能用的月份数从 **{min(months_n)} 个月**（{thinnest['name']}）"
                   f"到 **{max(months_n)} 个月**（{widest['name']}）不等"
                   if months_n and thinnest and widest else "各指标可用月份数不等")
                + "（这是各列自己的可用起点不同：十年百分位要满 3 年预热才给数，"
                  "宽度/新高低/成交额几乎从录像开头就有）。",
                _eff_sentence
                + "（见下方「有效独立样本」列）—— 所以 p 值只能当参考，不能当结论；"
                  "持有期越长，重叠越少、但样本也越少。",
                f"**共检验 {n_tests} 个组合**（{len(summary)} 个指标 × "
                f"{len(VALIDITY_HORIZONS)} 个持有期），"
                f"按纪律 3「数族不数因子」归到 {len(families)} 个族 —— "
                f"其中 **{len(usable_fams)} 个族**（{'、'.join(usable_fams) or '无'}）"
                f"**至少在一个持有期上**找到了可用证据；"
                "判断依据是各指标自己那一行写明的持有期，"
                "**不等于它在 12 个月上也显著**。"
                "从这么多次比较里挑出显著的那几个，本身就是过拟合风险；"
                "本项目**不做**「多重检验校正」（那是为了对付「试很多次、挑出最好的那个」这类"
                "偏差的统计处理；2026-07-26 用户拍板不做），"
                "所以这里如实披露检验次数，由读者自己打折。",
                "**本体检只说明样本内相关性，不改任何仓位**（业务铁律 1）；"
                "没通过的一律标「仅描述现状，不作预测依据」，"
                "**不引入权重、不合成总分**（与「四指数并行不合成」的既定拍板一致）。"]}

