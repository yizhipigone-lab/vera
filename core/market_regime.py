# -*- coding: utf-8 -*-
"""core/market_regime.py — 牛熊区间与时长

2026-09-19 架构修订批次 5.1 第二刀: 自 core/market_position_runner.py 端出。
依赖只有共享底座 core/market_position_io (路径/原语)。
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from core import market_position_io as mpio
from core.market_position_io import (  # 共享底座原语 (批次 5.1)
    INDEX_SPECS,
    _f,
    _index_series,
    _num,
)
from core.market_position import index_position_series
from utils.logger import get_logger

_logger = get_logger(__name__)


def _regime_episodes(labels: pd.Series) -> list[dict]:
    """把逐日的牛/熊/震荡标签切成**连续的区间** → 每段的起止/交易日数/涨跌幅。

    计划书 §14.5 的缺口: `index_regime.classify` 只给**单点状态**(今天牛还是熊),
    答不了"**这轮牛走了多久、超出历史中位多少**" —— 而"走了多久"正是"位置"的一部分。
    """
    out: list[dict] = []
    cur, start = None, None
    idx = list(labels.index)
    for i, v in enumerate(labels.to_numpy()):
        v = None if v is None or v != v else str(v)
        if v != cur:
            if cur is not None and start is not None and i - 1 >= start:
                out.append({"state": cur, "start_i": start, "end_i": i - 1})
            cur, start = v, i
    if cur is not None and start is not None:
        out.append({"state": cur, "start_i": start, "end_i": len(idx) - 1})
    return [e for e in out if e["state"]]

def _regime_summary(labels: pd.Series, closes: pd.Series, *,
                    caliber: str) -> dict | None:
    """一条牛熊口径的**区间统计** + 当前这一段走到哪了。

    两条口径都在体温表里并列给 (§14.6): 年线斜率口径与 20% 法则口径。
    返回 None = 标签全空 (样本不足)。

    **实测发现 (2026-09-17, 必须如实带出)**: 年线(MA250)斜率口径在**日频上会频繁翻状态** ——
    沪深300 历史 71 段、中位只有 0.3 个月, 于是"本轮已走 0.9 个月 / 历史中位 0.3 个月"
    这类对比基本是噪声。处置**不是**偷偷给它加去抖(那等于发明第三种口径), 而是:
    ①照实给; ②当某口径的中位区间长度 < 1 个月时, 输出里**明写"这个口径的『走了多久』不可用"**。
    """
    eps = _regime_episodes(labels)
    if not eps:
        return None
    px = closes.reindex(labels.index)
    idx = list(labels.index)
    rows = []
    for e in eps:
        a, b = e["start_i"], e["end_i"]
        p0, p1 = float(px.iloc[a]), float(px.iloc[b])
        rows.append({
            "state": e["state"],
            "start": idx[a].date().isoformat(),
            "end": idx[b].date().isoformat(),
            "days": b - a + 1,
            "months": _f((idx[b] - idx[a]).days / 30.44, 1),
            "ret_pct": _f((p1 / p0 - 1) * 100, 1) if p0 > 0 else None,
        })
    for i, r in enumerate(rows):
        r["ongoing"] = (i == len(rows) - 1)
    cur = rows[-1]
    hist = [r for r in rows[:-1] if r["state"] == cur["state"]]
    allm = pd.Series([r["months"] for r in rows])
    same = pd.Series([r["months"] for r in hist]) if hist else pd.Series(dtype=float)
    years = max((idx[-1] - idx[0]).days / 365.25, 1e-9)
    flicker = bool(len(hist) >= 5 and float(same.median()) < 1.0)
    return {"caliber": caliber, "state": cur["state"], "since": cur["start"],
            "months": cur["months"], "ret_pct": cur["ret_pct"],
            "n_episodes": len(rows), "n_same_state": len(hist),
            "median_months": _f(same.median(), 1) if hist else None,
            "median_ret_pct": _f(pd.Series([r["ret_pct"] for r in hist]).median(), 1)
            if hist else None,
            "months_percentile": (_f(float((same <= cur["months"]).mean()) * 100, 0)
                                  if hist else None),
            "all_median_months": _f(allm.median(), 1),
            "flips_per_year": _f(len(rows) / years, 1),
            "too_flickery": flicker,
            "flicker_note": (
                f"该口径在日频上翻状态很勤（历史 {len(rows)} 段、{len(rows) / years:.1f} 段/年、"
                f"中位只有 {_num(allm.median(), 1)} 个月），所以它的「本轮已走多久」"
                "参考价值有限 —— 这正是需要第二条口径的原因" if flicker else ""),
            "same_state_rows": hist,
            # 明细表只列"历史上最长的 8 段": 抖动的口径会产出几十段 0.0 个月的碎片,
            # 全列出来只会淹掉真正有意义的那几轮周期 (选最长是描述, 不是阈值)
            "longest_rows": sorted(hist, key=lambda r: -r["months"])[:8]}

def _regime_all() -> dict:
    """三大指数 × 两条口径的区间统计 (体温表「这轮走了多久」一节用)。"""
    out = {}
    for key, name, code in INDEX_SPECS:
        s = _index_series(code)
        if s is None or len(s) < 30:
            out[key] = None
            continue
        s = s.dropna()
        s = s[s > 0]
        df = index_position_series(s)
        out[key] = {
            "name": name, "code": code,
            "ma250": _regime_summary(df["regime"], s, caliber="年线斜率口径"),
            "pct20": _regime_summary(df["regime_20"], s, caliber="20% 法则口径"),
        }
    return out

