# -*- coding: utf-8 -*-
"""因子 IC 筛选流水线 — IC 计算正确性 + 面板因子因果性 + 板块热度管线 测试

关键保障:
- IC 自校验: 因子=未来收益本身 → IC≈1; 取负 → IC≈-1; 噪声 → |IC| 小
- 因果性: 截断 t 日后数据, 因子在 t 日及之前的取值逐值不变(防前视)
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import factor_ic_screen as fis  # noqa: E402


def _panel(n_days=120, n_stocks=30, seed=1):
    idx = pd.bdate_range(end="2026-07-17", periods=n_days)
    rng = np.random.default_rng(seed)
    ret = rng.normal(0.0005, 0.02, (n_days, n_stocks))
    close = pd.DataFrame(100 * np.exp(np.cumsum(ret, axis=0)),
                         index=idx, columns=[f"S{i:03d}.SZ" for i in range(n_stocks)])
    vol = pd.DataFrame(rng.uniform(1e6, 5e6, (n_days, n_stocks)), index=idx, columns=close.columns)
    amt = close * vol
    spread = rng.uniform(0.005, 0.02, (n_days, n_stocks))
    high = close * (1 + spread)
    low = close * (1 - spread)
    open_ = close * (1 + rng.normal(0, 0.003, (n_days, n_stocks)))
    return {"close": close, "high": high, "low": low, "open": open_, "volume": vol, "amount": amt}


def _selections_from_panel(close, n_per_day=8, seed=2):
    """从面板随机抽信号 (select_date, stock_code)。"""
    rng = np.random.default_rng(seed)
    rows = []
    for d in close.index[70:-25]:
        codes = rng.choice(close.columns, size=n_per_day, replace=False)
        rows += [(c, d) for c in codes]
    return pd.DataFrame(rows, columns=["stock_code", "select_date"])


def test_ic_perfect_factor_near_one():
    p = _panel()
    sel = _selections_from_panel(p["close"])
    sel["fwd10"] = fis.lookup(fis.forward_returns(p["close"], 10), sel["select_date"], sel["stock_code"])
    sel["perfect"] = sel["fwd10"]                      # 因子=未来收益
    s = fis.ic_stats(sel, "perfect", "fwd10")
    assert s["ic_mean"] == pytest.approx(1.0, abs=1e-9)
    assert s["ic_pos_rate"] == 1.0


def test_ic_inverted_factor_near_minus_one():
    p = _panel()
    sel = _selections_from_panel(p["close"])
    sel["fwd10"] = fis.lookup(fis.forward_returns(p["close"], 10), sel["select_date"], sel["stock_code"])
    sel["inv"] = -sel["fwd10"]
    assert fis.ic_stats(sel, "inv", "fwd10")["ic_mean"] == pytest.approx(-1.0, abs=1e-9)


def test_ic_noise_small():
    p = _panel(n_days=200, n_stocks=40)
    sel = _selections_from_panel(p["close"], n_per_day=20)
    sel["fwd10"] = fis.lookup(fis.forward_returns(p["close"], 10), sel["select_date"], sel["stock_code"])
    rng = np.random.default_rng(5)
    sel["noise"] = rng.normal(0, 1, len(sel))
    assert abs(fis.ic_stats(sel, "noise", "fwd10")["ic_mean"]) < 0.1


def test_lookup_correctness():
    p = _panel()
    d, c = p["close"].index[50], p["close"].columns[3]
    v = fis.lookup(p["close"], pd.Series([d]), pd.Series([c]))
    assert v[0] == pytest.approx(p["close"].loc[d, c])


def test_lookup_out_of_bounds_nan():
    p = _panel()
    v = fis.lookup(p["close"], pd.Series([pd.Timestamp("1999-01-01")]), pd.Series(["XXX.SZ"]))
    assert np.isnan(v[0])


@pytest.mark.parametrize("fn", [fis.f_mom20, fis.f_vol20, fis.f_dist_ma20, fis.f_dist_high20,
                                fis.f_volr5_20, fis.f_amt20, fis.f_turnover5,
                                fis.f_ret1, fis.f_maxret20, fis.f_rsi14,
                                fis.f_macd_hist, fis.f_boll_pct, fis.f_kdj_k,
                                fis.f_dist_high250, fis.f_price, fis.f_amihud20,
                                fis.f_ret_skew20, fis.f_volvol20, fis.f_pv_corr20,
                                fis.f_overnight20, fis.f_intraday20])
def test_panel_factors_causal(fn):
    p = _panel()
    full = fn(p)
    cut = 100
    trunc = fn({k: v.iloc[:cut] for k, v in p.items()})
    pd.testing.assert_frame_equal(full.iloc[:cut], trunc, check_names=False)


def test_sector_heat_same_sector_same_value_and_causal():
    p = _panel(n_stocks=10)
    cols = p["close"].columns
    sector_map = {c: ("AAA" if i < 5 else "BBB") for i, c in enumerate(cols)}
    ctx = {"sector_map": sector_map}
    heat = fis.f_sector_heat20(p, ctx)
    row = heat.iloc[-1]
    # 同板块成员热度相同, 且取值在 [0,1](截面分位)
    assert row[cols[:5]].nunique() == 1 and row[cols[5:]].nunique() == 1
    assert row.dropna().between(0, 1).all()
    # 因果性
    trunc = fis.f_sector_heat20({k: v.iloc[:100] for k, v in p.items()}, ctx)
    pd.testing.assert_frame_equal(heat.iloc[:100], trunc, check_names=False)


def test_quintile_spread_monotonic():
    n = 2000
    rng = np.random.default_rng(9)
    f = rng.normal(0, 1, n)
    df = pd.DataFrame({"f": f, "y": 0.01 * f + rng.normal(0, 0.05, n)})
    assert fis.quintile_spread(df, "f", "y") > 0


# ── 指数关联因子(corr_index20 / beta60) ─────────────────────

def _ctx_for(close: pd.DataFrame) -> dict:
    """合成 index_ret = 全市场等权日收益(与个股同窗口)。"""
    return {"index_ret": close.pct_change().mean(axis=1)}


def test_corr_index_perfect_when_identical():
    p = _panel()
    ctx = _ctx_for(p["close"])
    # 构造一只"指数本身"股票塞进去: 与指数收益完全一致 → corr 应 ≈1
    p2 = dict(p)
    c = p["close"].copy()
    c["IDX"] = 100 * (1 + ctx["index_ret"].fillna(0)).cumprod()
    p2["close"] = c
    ctx = _ctx_for(c)
    corr = fis.f_corr_index20(p2, ctx)
    assert corr["IDX"].iloc[-1] == pytest.approx(1.0, abs=1e-6)


def test_beta_one_when_identical():
    p = _panel()
    c = p["close"].copy()
    ctx0 = _ctx_for(c)
    c["IDX"] = 100 * (1 + ctx0["index_ret"].fillna(0)).cumprod()
    ctx = _ctx_for(c)
    beta = fis.f_beta60({"close": c}, ctx)
    assert beta["IDX"].iloc[-1] == pytest.approx(1.0, abs=1e-6)


def test_index_factors_causal():
    p = _panel()
    ctx = _ctx_for(p["close"])
    corr_full = fis.f_corr_index20(p, ctx)
    beta_full = fis.f_beta60(p, ctx)
    cut = 100
    p_tr = {k: v.iloc[:cut] for k, v in p.items()}
    ctx_tr = _ctx_for(p_tr["close"])
    pd.testing.assert_frame_equal(corr_full.iloc[:cut], fis.f_corr_index20(p_tr, ctx_tr), check_names=False)
    pd.testing.assert_frame_equal(beta_full.iloc[:cut], fis.f_beta60(p_tr, ctx_tr), check_names=False)
