# -*- coding: utf-8 -*-
"""core/market_mirror.py — 照镜子 (历史相似日 + 之后实际怎么走)。

2026-09-19 架构修订批次 5.1 第四刀: 自 core/market_position_runner.py 端出。
**只报告不预测**: 报"历史上跟今天像的日子之后实际怎么走", 并强制带三条警告
(相似≠预测 / 有效独立样本 N_eff / 年份集中度)。依赖共享底座 + core/market_position。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core import market_position_io as mpio
from core.market_position_io import (  # 共享底座原语
    _f,
    _features_frame,
    _index_series,
    _rat,
    _year_breakdown,
    history,
)
from core.market_position import (
    RECENT_EXCLUDE_BARS,
    SIMILAR_FEATURES,
    forward_return,
    similar_days,
)
from utils.logger import get_logger

_logger = get_logger(__name__)


#: 照镜子必须原样带出的警告 (写成常量, 不靠各处自觉)
MIRROR_WARNING = (
    "「历史相似」不是预测 —— 上表说的是「历史上跟今天像的那些日子, 之后实际怎么走」, "
    "不等于这次也会那样走。命中 {n} 个交易日看着不少, 但它们挨得很近、涨跌高度重叠, "
    "**真正独立的信息只有大约 {n_eff} 份**; 而 A 股几十年也只经历过屈指可数的几轮周期。")

#: 结论被单一年份主导时的点名警告 (§14.3; 有测试锁住"必须出现")
YEAR_DOMINANCE_WARNING = (
    "⚠ **本结论由 {year} 年主导** —— 这一年在 {n} 个命中日里占了 {share}。"
    "换句话说, 上面的\"历史上像今天的时候之后怎么走\", 主要是\"{year} 年那一次怎么走\", "
    "不是很多次独立经验的平均。")

#: 照镜子的结论依据 = 「距离最近的一档」(前 5%), 不再拿 top-5 的中位数当结论 (§14.2)。
#: 原因: 5 个样本的中位数不是统计量, 报它等于虚报精度。
MIRROR_BAND_QUANTILE = 0.05

#: 分位带内样本数上限 (防"最近的一档"大到几百天)
MIRROR_BAND_MAX = 300

#: 单个年份占分位带比例 > 该值 → 必须点名"本结论由该年主导" (§14.3)
YEAR_DOMINANCE = 0.5

def mirror(top_n: int = 5) -> dict:
    """历史照镜子: 今天最像历史上哪几天 + 那几天之后实际怎么走。

    诚实边界写在 MIRROR_WARNING 里, 调用方 (页面/体温表) 必须原样展示。
    """
    recs = history(limit=0)
    if len(recs) < 120:
        return {"ok": False,
                "reason": f"连续录像只有 {len(recs)} 条, 至少需要 120 条; "
                          "先跑 tools/market_position_collect.py --backfill"}
    hist = _features_frame(recs)
    target = {k: hist[k].dropna().iloc[-1] if hist[k].notna().any() else None
              for k in SIMILAR_FEATURES}
    # 纪律①(排除最近 252 天)与纪律②(命中日之间至少隔 20 天)在 pure 层;
    # 结论依据 = 距离最近的一档(前 5%), top_n 明细只作"最像的几天"展示 (§14.2)。
    res = similar_days(target, hist, top_n=top_n, quantile=MIRROR_BAND_QUANTILE,
                       max_band=MIRROR_BAND_MAX)
    picks, band = res["picks"], res["band"]
    hs = _index_series("000300.SH")
    sh = _index_series("000001.SH")

    def _add_fwd(items: list[dict]) -> list[dict]:
        for p in items:
            p["fwd_20_hs300_pct"] = forward_return(hs, p["date"], 20) if hs is not None else None
            p["fwd_60_hs300_pct"] = forward_return(hs, p["date"], 60) if hs is not None else None
            p["fwd_20_sh_pct"] = forward_return(sh, p["date"], 20) if sh is not None else None
        return items

    _add_fwd(picks)
    _add_fwd(band)
    if not band and not picks:
        used = len(hist.dropna()) if len(hist) else 0
        return {"ok": False,
                "reason": f"六个相似度特征齐全的历史交易日只有 {used} 条, 而"
                          f"「排除最近 {RECENT_EXCLUDE_BARS} 天」这条纪律要求至少 "
                          f"{RECENT_EXCLUDE_BARS + 1} 条; 先跑全量回填把录像补长。"
                          "（十年百分位要满 3 年才有数, 所以录像的头几年特征不全。）"}

    def _vals(items, key):
        return [float(p[key]) for p in items if p.get(key) is not None]

    f20, f60 = _vals(band, "fwd_20_hs300_pct"), _vals(band, "fwd_60_hs300_pct")

    def _n_eff(k: int) -> float | None:
        """有效独立样本 = 档内命中日数 ÷ 持有期天数 (重叠窗口会严重高估信息量)。

        实测 110 个命中日 / 20 日 ≈ **5.5 份**、/ 60 日 ≈ 1.8 份 —— 看着一百多个样本,
        真正独立的信息不到 6 份, 这就是为什么必须把它写在结论旁边。
        """
        return _f(len(band) / float(k), 1) if k > 0 else None

    s20, s60 = pd.Series(f20), pd.Series(f60)
    summary = {
        "n": len(band),
        "n_picks": len(picks),
        "fwd_20_median": _f(s20.median(), 2) if f20 else None,
        "fwd_20_mean": _f(s20.mean(), 2) if f20 else None,
        "fwd_20_q25": _f(s20.quantile(0.25), 2) if f20 else None,
        "fwd_20_q75": _f(s20.quantile(0.75), 2) if f20 else None,
        "fwd_20_up_ratio": _f(sum(1 for x in f20 if x > 0) / len(f20) * 100, 0) if f20 else None,
        "fwd_60_median": _f(s60.median(), 2) if f60 else None,
        "fwd_60_up_ratio": _f(sum(1 for x in f60 if x > 0) / len(f60) * 100, 0) if f60 else None,
        "n_eff_20": _n_eff(20),
        "n_eff_60": _n_eff(60),
    }
    years = _year_breakdown(band, "fwd_20_hs300_pct")
    total = sum(y["n"] for y in years)
    dom = None
    warnings = [MIRROR_WARNING.format(n=len(band), n_eff=summary["n_eff_20"])]
    if total:
        top = max(years, key=lambda y: y["n"])
        if top["n"] / total > YEAR_DOMINANCE:
            dom = top["year"]
            warnings.append(YEAR_DOMINANCE_WARNING.format(
                year=dom, n=f"{total} 个里的 {top['n']} 个",
                share=_rat(top["n"] / total * 100)))
    return {"ok": True, "asof": recs[-1]["date"], "target": target,
            "matches": picks, "band": band, "summary": summary,
            "years": years, "dominance": dom,
            "eligible": res["eligible"], "exclude_recent": res["exclude_recent"],
            "min_gap": res["min_gap"], "band_quantile": res["quantile"],
            "warning": " ".join(warnings)}

