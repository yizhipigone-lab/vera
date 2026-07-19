# -*- coding: utf-8 -*-
"""避雷阈值信号源锦标赛 — 信号逻辑 + 因果性 + 退化守卫 + 判定 测试

计划书: docs/plan/2026-07-19_避雷阈值信号锦标赛_计划书.md §3步骤2 / §4
因果性测试: 截断 t 日后数据, t 日及之前的状态必须逐值不变(防前视)。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import filter_signal_tournament as fst  # noqa: E402


def _daily_index(n, end="2026-07-17"):
    return pd.bdate_range(end=end, periods=n)


# ── 信号逻辑 ─────────────────────────────────────────────────

def test_ma200_below_triggers_strict():
    idx = _daily_index(300)
    close = pd.Series(np.linspace(100, 100, 250).tolist() + np.linspace(99, 60, 50).tolist(), index=idx)
    strict = fst.signal_ma200(close)
    assert strict.iloc[-1]          # 暴跌末端远在 MA200 之下
    assert not strict.iloc[200]     # 平稳期不严格


def test_ma200_above_not_strict():
    idx = _daily_index(250)
    close = pd.Series(np.linspace(50, 100, 250), index=idx)
    assert not fst.signal_ma200(close).iloc[-1]


def test_drawdown_triggers_after_15pct_drop():
    idx = _daily_index(300)
    close = pd.Series([100.0] * 250 + [84.0] * 50, index=idx)  # 瞬间 -16%
    strict = fst.signal_drawdown(close)
    assert strict.iloc[250]
    assert not strict.iloc[249]


def test_drawdown_ignores_small_drop():
    idx = _daily_index(300)
    close = pd.Series([100.0] * 250 + [90.0] * 50, index=idx)  # -10% < 15%
    assert not fst.signal_drawdown(close).iloc[-1]


def test_volatility_calm_then_spike():
    idx = _daily_index(400)
    rng = np.random.default_rng(7)
    calm = rng.normal(0, 0.005, 300)          # 低波
    storm = rng.normal(0, 0.05, 100)          # 高波
    close = pd.Series(100 * np.exp(np.cumsum(np.concatenate([calm, storm]))), index=idx)
    strict = fst.signal_volatility(close)
    assert strict.iloc[-30:].any()            # 高波段触发
    # calm 期误报率应远低于高波段触发率(分位数信号平稳期天然 ~20% 误报,不断言单点)
    calm_rate = strict.iloc[60:290].mean()
    storm_rate = strict.iloc[310:400].mean()
    assert storm_rate > calm_rate * 2


def test_breadth_low_participation_strict():
    idx = _daily_index(40)
    # 10 只股票: 8 只持续下跌(MA20 下), 2 只上涨 → 站上 MA20 占比 20%... 构造 8/10 在 MA20 下
    data = {}
    for i in range(8):
        data[f"S{i}"] = np.linspace(100, 80, 40)      # 收盘 < MA20
    for i in range(8, 10):
        data[f"S{i}"] = np.linspace(80, 100, 40)      # 收盘 > MA20
    closes = pd.DataFrame(data, index=idx)
    strict = fst.signal_breadth(closes, ma=20, thresh=0.2)
    # 末端: 8 只在 MA20 下, 2 只刚好接近 MA20 → 占比 ≤20% → 严格
    assert strict.iloc[-1] == bool((closes.iloc[-1] > closes.rolling(20).mean().iloc[-1]).mean() < 0.2)


def test_breadth_broad_rally_not_strict():
    idx = _daily_index(40)
    data = {f"S{i}": np.linspace(80, 100, 40) for i in range(10)}
    closes = pd.DataFrame(data, index=idx)
    assert not fst.signal_breadth(closes).iloc[-1]


# ── 因果性(计划书强制): 截断未来, 过去逐值不变 ──────────────

@pytest.mark.parametrize("gen", ["ma200", "drawdown", "volatility"])
def test_index_signals_causal(gen):
    idx = _daily_index(400)
    rng = np.random.default_rng(11)
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 400))), index=idx)
    fn = {"ma200": fst.signal_ma200, "drawdown": fst.signal_drawdown,
          "volatility": fst.signal_volatility}[gen]
    full = fn(close)
    for cut in [300, 350]:
        trunc = fn(close.iloc[:cut])
        pd.testing.assert_series_equal(full.iloc[:cut], trunc, check_names=False)


def test_breadth_causal():
    idx = _daily_index(60)
    rng = np.random.default_rng(3)
    closes = pd.DataFrame({f"S{i}": 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 60)))
                           for i in range(5)}, index=idx)
    full = fst.signal_breadth(closes)
    trunc = fst.signal_breadth(closes.iloc[:50])
    pd.testing.assert_series_equal(full.iloc[:50], trunc, check_names=False)


# ── 退化守卫(§4) ─────────────────────────────────────────────

def test_diagnostics_always_strict_is_degenerate():
    idx = _daily_index(240)
    strict = pd.Series(True, index=idx)
    d = fst.arm_diagnostics(strict, idx[::5])
    assert d["strict_ratio"] == 1.0 and d["degenerate"]


def test_diagnostics_normal_arm_not_degenerate():
    idx = _daily_index(240)
    strict = pd.Series(False, index=idx)
    strict.iloc[50:90] = True                     # 一段严格
    d = fst.arm_diagnostics(strict, idx[::5])
    assert not d["degenerate"] and d["switches"] == 2
    assert 0.05 <= d["strict_ratio"] <= 0.60


def test_diagnostics_chatter_is_degenerate():
    idx = _daily_index(240)
    strict = pd.Series([i % 2 == 0 for i in range(240)], index=idx)  # 隔日切换
    d = fst.arm_diagnostics(strict, idx[::5])
    assert d["switches"] > fst.MAX_SWITCHES and d["degenerate"]


# ── 判定(§4 预注册) ─────────────────────────────────────────

def _r(annret, maxdd, calmar, degenerate=False):
    return {"annret": annret, "maxdd": maxdd, "calmar": calmar, "degenerate": degenerate}


def test_judge_pass():
    base = _r(0.38, -0.121, 3.15)
    assert fst.judge(_r(0.375, -0.098, 3.42), base) == "PASS"


def test_judge_fail_on_calmar():
    base = _r(0.38, -0.121, 3.15)
    assert fst.judge(_r(0.30, -0.098, 2.9), base) == "FAIL"   # Calmar 差 >0.1


def test_judge_fail_on_dd():
    base = _r(0.38, -0.121, 3.15)
    assert fst.judge(_r(0.38, -0.115, 3.2), base) == "FAIL"   # 回撤改善 <2pp


def test_judge_fail_on_annret_loss():
    base = _r(0.38, -0.121, 3.15)
    assert fst.judge(_r(0.36, -0.09, 3.3), base) == "FAIL"    # 年化损失 >1pp


def test_judge_degenerate_shortcircuits():
    assert fst.judge(_r(0.50, -0.05, 5.0, degenerate=True), _r(0.38, -0.121, 3.15)) == "DEGENERATE"
