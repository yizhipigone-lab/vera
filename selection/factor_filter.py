# -*- coding: utf-8 -*-
"""因子过滤(生产接缝) — 把实验室终审通过的过滤规则应用到选股结果上。

设计(2026-07-19, 计划书 docs/plan/2026-07-19_公式因子体检实验室_计划书.md):
- 规则来自 formula_lab 的 {formula}_filter_rules.json, 前端按公式勾选, 存 current.yaml;
- 排名口径与终审完全一致: 每个 select_date 内横截面 rank(pct), 缺失值保留;
- 因果: 面板因子 trailing-only(复用 factor_ic_screen 注册函数);
  daily_basic 因子用 T 日快照(T 收盘决策点已知, 与 T 收盘买入铁律一致)。

注意: 过滤语义与 tools/overheat_ab_test.py::apply_filter 一致(终审同款),
此处是生产版(多规则顺序应用), 改动时需两侧同步。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
KEEP_COLS = ["stock_code", "select_date", "formula_name"]

# daily_basic 来源的因子(其余视为 kline 面板因子)
DB_FACTORS = {"turnover_rate", "volume_ratio", "pe_ttm", "pb", "ps_ttm",
              "dv_ratio", "total_mv", "circ_mv"}


def apply_rules(df: pd.DataFrame, rules: list[str]) -> pd.DataFrame:
    """按日截面 rank 顺序应用多条规则("turnover_rate:top10" 等)。缺失因子值的行保留。"""
    if not rules:
        return df
    out = df
    for item in rules:
        factor, rule = item.split(":")
        if factor not in out.columns:
            raise KeyError(f"因子值缺失: {factor}(compute_factor_values 未提供)")
        rank = out.groupby("select_date")[factor].rank(pct=True)
        if rule == "top10":
            keep = rank.isna() | (rank <= 0.90)
        elif rule == "top20":
            keep = rank.isna() | (rank <= 0.80)
        elif rule == "bottom10":
            keep = rank.isna() | (rank > 0.10)
        elif rule == "bottom20":
            keep = rank.isna() | (rank > 0.20)
        else:
            raise ValueError(f"未知规则: {rule}")
        out = out[keep]
    return out


def _load_closes(codes: list[str], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """只读选股池涉及个股的 1d 收盘(kline_cache 本地 parquet, 无 TDX 依赖)。"""
    kdir = ROOT / "data" / "kline_cache" / "1d"
    warmup = start - pd.Timedelta(days=90)   # dist_ma20/RSI 等最多 ~60 交易日暖机, 留余量
    series = {}
    for c in dict.fromkeys(codes):
        f = kdir / f"{c}.parquet"
        if not f.exists():
            continue
        try:
            d = pd.read_parquet(f, columns=["date", "close"])
        except Exception:
            continue
        d = d[(d["date"] >= warmup) & (d["date"] <= end)]
        if len(d):
            series[c] = pd.Series(d["close"].values, index=pd.to_datetime(d["date"]))
    if not series:
        return pd.DataFrame()
    return pd.concat(series, axis=1).sort_index()


def _panel_factor_fn(name: str):
    """从 factor_ic_screen 注册表取面板因子函数(单一数据源)。"""
    sys.path.insert(0, str(ROOT / "tools"))
    from factor_ic_screen import PANEL_FACTORS
    for n, fn, _need, _fam in PANEL_FACTORS:
        if n == name:
            return fn
    raise KeyError(f"未知面板因子: {name}(不在 factor_ic_screen 注册表)")


def compute_factor_values(selections: pd.DataFrame, factors: list[str]) -> pd.DataFrame:
    """为 selections 计算所需因子值(返回带因子列的副本)。

    面板因子: 本地 kline 面板 + 注册函数(需要 ctx 的板块/指数因子此处不支持, 抛错);
    daily_basic 因子: tushare 按日快照(缓存 data/factors/daily_basic_*.parquet)。
    """
    sel = selections.copy()
    sel["select_date"] = pd.to_datetime(sel["select_date"])
    dates = pd.DatetimeIndex(sel["select_date"].unique())
    start, end = dates.min(), dates.max()

    need_panel = [f for f in factors if f not in DB_FACTORS]
    need_db = [f for f in factors if f in DB_FACTORS]

    if need_panel:
        closes = _load_closes(sel["stock_code"].tolist(), start, end)
        if closes.empty:
            raise RuntimeError("kline_cache 无选股池个股数据, 无法计算面板因子")
        sys.path.insert(0, str(ROOT / "tools"))
        from factor_ic_screen import lookup
        for f in need_panel:
            fn = _panel_factor_fn(f)
            if fn.__code__.co_argcount == 2:
                raise NotImplementedError(f"因子 {f} 需要 ctx(板块/指数), 生产过滤暂不支持")
            panel = fn({"close": closes})
            sel[f] = lookup(panel, sel["select_date"], sel["stock_code"])

    if need_db:
        sys.path.insert(0, str(ROOT / "tools"))
        from factor_ic_screen import load_daily_basic
        tag = f"{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}"
        db = load_daily_basic(dates, tag)
        if len(db):
            sel["_td"] = sel["select_date"].dt.strftime("%Y%m%d")
            sel = sel.merge(db.rename(columns={"ts_code": "stock_code"}),
                            left_on=["stock_code", "_td"], right_on=["stock_code", "trade_date"],
                            how="left").drop(columns=["_td", "trade_date"])
            for f in need_db:
                sel[f] = pd.to_numeric(sel[f], errors="coerce")

    return sel


def filter_selections(selections: pd.DataFrame, rules: list[str]) -> tuple[pd.DataFrame, dict]:
    """生产入口: 算因子值 → 应用规则 → (过滤后 selections, 审计信息)。"""
    t0 = time.time()
    factors = sorted({r.split(":")[0] for r in rules})
    valued = compute_factor_values(selections, factors)
    before = len(selections)
    filtered = apply_rules(valued, rules)[KEEP_COLS].copy()
    info = {"rules": rules, "before": before, "after": len(filtered),
            "removed": before - len(filtered), "elapsed_s": round(time.time() - t0, 1)}
    return filtered, info
