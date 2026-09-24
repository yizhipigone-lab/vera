"""批次 3a · A5 回测 parity 快照基线 (2026-08-01)。

背景: `_simulate_core_v3_legacy` 删除后, tests/test_loop_parity.py 退化为
形状断言 (只断言维度/类型, 不断言数值), 数值级回归网缺失
(见 docs/plan/2026-08-01_架构优化计划书.md 批次 3 A5 行)。
本测试按 docs/architecture/loop.md §7.7 重建数值级保护网:
固定策略配置 × 固定种子合成数据 → equity/trades 快照入仓 (JSON),
逐字段**精确**对比 (浮点经 repr 精度 JSON 往返, 位级一致)。

快照只读不写。需要重建基线时 (确认行为变更合法后) 运行:
    python -X utf8 tools/gen_snapshot_parity.py

覆盖矩阵:
  loop 层 (build_backtest_loop — run/run_cached 两入口的共同核心):
    stop_first / ladder_tp_first / trailing_first 三 priority 主分支
    + 退市+formula_sell 组合分支 + crafted 双触发场景
  engine 层 (run_cached, filter_limit_up=False):
    stop_first / trailing_first, 锁 stop_config dict → loop 参数准备段
  另含 run vs run_cached 同参一致性守卫 (test_run_vs_run_cached_consistency)。
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.engine import ENGINE_VERSION, BacktestEngine
from backtest.loop import build_backtest_loop
from backtest.prepared import PreparedMatrix

# 复用既有固定种子合成数据生成器 (不依赖真实行情缓存 — 真实数据会漂移)
from tests.test_loop_parity import make_crafted_dual_trigger, make_synthetic

SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")

# ─────────────────────────────────────────────────────────────
# 快照参数 (2026-08-01 固化; 改这里 = 改基线, 必须重新生成快照)
# 显式非零 slippage/stamp_tax, 连成本路径一起锁。
# ─────────────────────────────────────────────────────────────
SNAP_PARAMS = dict(
    initial_capital=1_000_000.0, commission=0.0003,
    min_buy_amount=1000.0, max_buy_amount=200_000.0, lot_size=100, min_lots=1,
    cost_stop_enabled=True, cost_stop_threshold=-0.05,
    trailing_enabled=True, trailing_activation=0.05, trailing_drawdown=0.10,
    ladder_enabled=True,
    ladder_profits=(0.06, 0.15), ladder_ratios=(0.5, 0.5), n_ladder=2,
    time_enabled=True, max_hold_days=10,
    cond_time_enabled=True, cond_time_days=3, cond_time_profit=0.08,
    first_day_enabled=True, first_day_target=0.03,
    bpday=1, slippage=0.001, stamp_tax=0.0005,
    max_position_pct=1.0,
)


def _run_loop(price, high, low, open_, entry, *,
              ladder_tp_first=False, trailing_first=False,
              formula_exit_np=None, formula_exit_ratio=1.0,
              tradable_np=None, last_tradable_idx=None,
              degraded_np=None,
              **param_overrides):
    """按 SNAP_PARAMS 构造 BacktestLoop 并运行, 返回 (equity_arr, raw_trades)。

    param_overrides: 场景级参数覆盖 (如 crafted 场景关 first_day/cond_time
    防抢先平仓, 让目标分支真正走到)。覆盖值同样固化进快照。
    degraded_np (2026-08-06): 5m 降级 bar 标记, 仅降级场景传。
    """
    kw = dict(SNAP_PARAMS)
    kw.update(param_overrides)
    loop = build_backtest_loop(
        kw["initial_capital"], kw["commission"],
        kw["min_buy_amount"], kw["max_buy_amount"], kw["lot_size"], kw["min_lots"],
        kw["cost_stop_enabled"], kw["cost_stop_threshold"],
        kw["trailing_enabled"], kw["trailing_activation"], kw["trailing_drawdown"],
        kw["ladder_enabled"],
        np.array(kw["ladder_profits"], dtype=np.float64),
        np.array(kw["ladder_ratios"], dtype=np.float64),
        kw["n_ladder"],
        kw["time_enabled"], kw["max_hold_days"],
        kw["cond_time_enabled"], kw["cond_time_days"], kw["cond_time_profit"],
        kw["first_day_enabled"], kw["first_day_target"],
        bpday=kw["bpday"], slippage=kw["slippage"], stamp_tax=kw["stamp_tax"],
        max_position_pct=kw["max_position_pct"],
        ladder_tp_first=ladder_tp_first, trailing_first=trailing_first,
        formula_exit_np=formula_exit_np, formula_exit_ratio=formula_exit_ratio,
        trailing_confirm=kw.get("trailing_confirm", "intraday"),
    )
    return loop.run(price, entry, high, low, open_,
                    tradable_np, last_tradable_idx, formula_exit_np,
                    degraded_np=degraded_np)


# ─────────────────────────────────────────────────────────────
# 场景定义 (单一事实源: 测试与 tools/gen_snapshot_parity.py 共用)
# 每个场景函数返回 (equity_arr, raw_trades, params_summary dict)
# ─────────────────────────────────────────────────────────────
def _sc_loop_stop_first_s42():
    price, high, low, open_, entry = make_synthetic(seed=42)
    eq, tr = _run_loop(price, high, low, open_, entry)
    return eq, tr, {"level": "loop", "priority": "stop_first", "seed": 42}


def _sc_loop_ladder_tp_first_s7():
    price, high, low, open_, entry = make_synthetic(seed=7)
    eq, tr = _run_loop(price, high, low, open_, entry, ladder_tp_first=True)
    return eq, tr, {"level": "loop", "priority": "ladder_tp_first", "seed": 7}


def _sc_loop_trailing_first_s123():
    price, high, low, open_, entry = make_synthetic(seed=123)
    eq, tr = _run_loop(price, high, low, open_, entry, trailing_first=True)
    return eq, tr, {"level": "loop", "priority": "trailing_first", "seed": 123}


def _sc_loop_stop_first_delist_formula_s71():
    """退市 (reason=11) + formula_sell (reason=12) 组合分支。

    关 first_day/cond_time: 否则首日未达标 (reason=10) 会在 bar1 抢先平仓,
    持仓活不到 bar15 的退市/formula 触发点 (2026-08-01 建基线时实测)。
    """
    price, high, low, open_, entry = make_synthetic(seed=71, n_stocks=3)
    n, k = price.shape
    tradable = np.ones((n, k), dtype=bool)
    tradable[15:, 0] = False  # stock0 bar15 起退市
    last_tradable = np.full(k, -1, dtype=np.int64)
    last_tradable[0] = 14
    fsig = np.zeros((n, k), dtype=bool)
    fsig[15, 0] = True  # 同 bar 发 formula_sell 信号
    eq, tr = _run_loop(price, high, low, open_, entry,
                       tradable_np=tradable, last_tradable_idx=last_tradable,
                       formula_exit_np=fsig,
                       first_day_enabled=False, cond_time_enabled=False)
    return eq, tr, {"level": "loop", "priority": "stop_first", "seed": 71,
                    "extras": ["delisting", "formula_sell"],
                    "param_overrides": {"first_day_enabled": False,
                                        "cond_time_enabled": False}}


def _sc_loop_trailing_first_dual_trigger():
    """crafted 双触发: ladder 部分卖 + trailing 全卖剩余 (无随机, 数据固定)。

    关 first_day/cond_time: 否则首日未达标/条件时间止损会抢在 bar1 平仓,
    双触发分支走不到。ladder 第二档抬到 0.25: crafted 数据 bar2 涨幅恰为
    0.15, 与默认第二档 0.15 相等会两档同触发一次卖光 (2026-08-01 实测),
    抬高后只触发第一档 (部分卖), 剩余留给 bar3 trailing 全卖。
    """
    price, high, low, open_, entry = make_crafted_dual_trigger()
    eq, tr = _run_loop(price, high, low, open_, entry, trailing_first=True,
                       first_day_enabled=False, cond_time_enabled=False,
                       ladder_profits=(0.06, 0.25))
    return eq, tr, {"level": "loop", "priority": "trailing_first",
                    "data": "crafted_dual_trigger",
                    "param_overrides": {"first_day_enabled": False,
                                        "cond_time_enabled": False,
                                        "ladder_profits": [0.06, 0.25]}}


# ── engine 层 (run_cached) ──
def _make_engine():
    return BacktestEngine({
        "initial_capital": SNAP_PARAMS["initial_capital"],
        "commission": SNAP_PARAMS["commission"],
        "slippage": SNAP_PARAMS["slippage"],
        "stamp_tax": SNAP_PARAMS["stamp_tax"],
        "enable_realistic_costs": True,
        "period": "1d",
        "position_sizing": {
            "min_buy_amount": SNAP_PARAMS["min_buy_amount"],
            "max_buy_amount": SNAP_PARAMS["max_buy_amount"],
            "lot_size": SNAP_PARAMS["lot_size"],
            "min_lots": SNAP_PARAMS["min_lots"],
        },
    })


def _stop_config_snapshot(priority="stop_first"):
    """快照用 stop_config (2026-08-01 固化; 参数与 SNAP_PARAMS 对齐)。"""
    return {
        "priority": priority,
        "cost_stop": {"enabled": True, "threshold": SNAP_PARAMS["cost_stop_threshold"]},
        "trailing_stop": {"enabled": True,
                          "activation": SNAP_PARAMS["trailing_activation"],
                          "drawdown": SNAP_PARAMS["trailing_drawdown"]},
        "ladder_tp": {"enabled": True, "levels": [
            {"profit": p, "sell_ratio": r}
            for p, r in zip(SNAP_PARAMS["ladder_profits"], SNAP_PARAMS["ladder_ratios"])
        ]},
        "time_stop": {"enabled": True, "max_hold_days": SNAP_PARAMS["max_hold_days"]},
        "cond_time_stop": {"enabled": True, "days": SNAP_PARAMS["cond_time_days"],
                           "profit": SNAP_PARAMS["cond_time_profit"]},
        "first_day": {"enabled": True, "target": SNAP_PARAMS["first_day_target"]},
        "formula_sell": {"enabled": False, "formula_name": "", "formula_arg": "",
                         "sell_ratio": 1.0, "priority": 0},
        "capabilities": {"formula_exit": True, "gap_protection": True, "delisting": True},
    }


def _ladder_triplet(stop_config):
    lv = sorted(stop_config["ladder_tp"]["levels"], key=lambda x: x["profit"])
    lp = np.array([x["profit"] for x in lv], dtype=np.float64)
    lr = np.array([x["sell_ratio"] for x in lv], dtype=np.float64)
    return lp, lr, len(lv)


def _sc_engine_run_cached(priority, seed):
    price, high, low, open_, entry = make_synthetic(seed=seed, n_stocks=3)
    n, k = price.shape
    dates = pd.bdate_range("2024-01-02", periods=n)
    cols = [f"{600000 + i}.SH" for i in range(k)]
    close = pd.DataFrame(price, index=dates, columns=cols)
    entries = pd.DataFrame(entry, index=dates, columns=cols)
    # 能力数据: 跳空保护 open + 退市 tradable + formula_sell 信号
    tradable = np.ones((n, k), dtype=bool)
    tradable[30:, 2] = False  # stock2 bar30 起退市
    last_tradable = np.full(k, n - 1, dtype=np.int64)
    last_tradable[2] = 29
    fsig = np.zeros((n, k), dtype=bool)
    fsig[20, 0] = True
    eng = _make_engine()
    sc = _stop_config_snapshot(priority)
    lp, lr, nl = _ladder_triplet(sc)
    prepared = PreparedMatrix(
        close=close, entries=entries,
        high_np=high.astype(np.float64), low_np=low.astype(np.float64),
        open_np=open_.astype(np.float64),
        tradable_np=tradable, last_tradable_idx=last_tradable)
    result = eng.run_cached(
        prepared, sc, lp, lr, nl,
        filter_limit_up=False,  # 合成数据无涨跌停语义, 且避免 ST 信息外部依赖
        formula_exit_np=fsig, formula_exit_ratio=1.0,
        return_raw=True,
    )
    return (result["raw_equity"], result["raw_trades"],
            {"level": "engine_run_cached", "priority": priority, "seed": seed,
             "extras": ["open_np", "delisting", "formula_sell"]})


def _sc_engine_run_cached_stop_first_s42():
    return _sc_engine_run_cached("stop_first", 42)


def _sc_engine_run_cached_trailing_first_s7():
    return _sc_engine_run_cached("trailing_first", 7)


# ── confirm 模式覆盖 (2026-08-06 审计 P2: 5 种确认方式纳入快照防漂移) ──
def _make_confirm_crafted():
    """精巧 6 bar (bpday=2, 3 天): 峰顶长上影 + 次日回落, 让 5 种 confirm
    模式走出 5 个不同结果 (seed 合成数据实测 5 模式全同, 无区分度)。

    day0: 10 买入, high 11.0 创峰 (+10% ≥ 激活 5%), 线 = 9.9
    day1 bar0: high 12.0 创峰 (新线 10.8), low 10.5 破新线, close 11.5
    day1 bar1: low 11.0 不碰线, close 11.2
    day2 bar0: low 10.6 碰线, close 10.7
    day2 bar1: close 10.5 破线
    预期: intraday@10.8 线价(d1b0) / simple@11.5 bar收盘(d1b0) /
          low@11.2 日收盘(d1b1) / real@10.8 线价(d2b0, 创峰bar跳过) /
          close@10.5 日收盘(d2b1) —— 五模态全部分叉。
    """
    close = np.array([10.0, 10.6, 11.5, 11.2, 10.7, 10.5]).reshape(-1, 1)
    high = np.array([10.2, 11.0, 12.0, 11.6, 11.1, 10.6]).reshape(-1, 1)
    low = np.array([9.95, 10.3, 10.5, 11.0, 10.6, 10.4]).reshape(-1, 1)
    open_ = np.array([10.0, 10.4, 10.8, 11.4, 11.0, 10.6]).reshape(-1, 1)
    entry = np.zeros((6, 1), dtype=bool)
    entry[0, 0] = True
    return close, high, low, open_, entry


def _make_loop_confirm_scenario(confirm):
    """同一精巧数据 × 同一 SNAP_PARAMS, 只换 trailing_confirm; 关其他
    策略防抢先平仓 (ladder 首档 6% / first_day 3% 都会被 +10% 触发)。"""
    def _sc():
        close, high, low, open_, entry = _make_confirm_crafted()
        eq, tr = _run_loop(close, high, low, open_, entry,
                           bpday=2, trailing_confirm=confirm,
                           cost_stop_enabled=False, ladder_enabled=False,
                           time_enabled=False, cond_time_enabled=False,
                           first_day_enabled=False)
        return eq, tr, {"level": "loop", "data": "crafted_confirm_6bar",
                        "param_overrides": {"bpday": 2,
                                            "trailing_confirm": confirm,
                                            "cost_stop_enabled": False,
                                            "ladder_enabled": False,
                                            "time_enabled": False,
                                            "cond_time_enabled": False,
                                            "first_day_enabled": False}}
    return _sc


def _sc_loop_real_degraded_defer():
    """5m 降级天推迟峰值 (2026-08-06 审计 HIGH#2) 数值锁。

    6 根 bar (bpday=2) 精巧场景, 与 tests/test_degrade_peak_defer.py
    场景一同源 (两处手工同步): day1 两根降级 bar 广播 1d OHLC
    (高 12.0), 旧峰值 11 的线 9.9 继续值班 → bar2 按线价 9.9 卖出
    (旧逻辑峰值被刷成 12 → 全天 peak==high 跳过 → 次日才卖)。
    """
    close = np.array([10.0, 10.5, 10.5, 10.5, 10.3, 10.2]).reshape(-1, 1)
    high = np.array([10.2, 11.0, 12.0, 12.0, 10.6, 10.5]).reshape(-1, 1)
    low = np.array([9.9, 10.2, 9.8, 9.8, 10.1, 10.0]).reshape(-1, 1)
    open_ = np.array([10.0, 10.3, 11.5, 11.5, 10.4, 10.3]).reshape(-1, 1)
    entry = np.zeros((6, 1), dtype=bool)
    entry[0, 0] = True
    degraded = np.array([False, False, True, True, False, False]).reshape(-1, 1)
    eq, tr = _run_loop(close, high, low, open_, entry,
                       degraded_np=degraded, bpday=2, trailing_confirm="real",
                       cost_stop_enabled=False, ladder_enabled=False,
                       time_enabled=False, cond_time_enabled=False,
                       first_day_enabled=False)
    return eq, tr, {"level": "loop", "data": "crafted_degraded_6bar",
                    "extras": ["degraded_np", "confirm_real"],
                    "param_overrides": {"bpday": 2, "trailing_confirm": "real",
                                        "cost_stop_enabled": False,
                                        "ladder_enabled": False,
                                        "time_enabled": False,
                                        "cond_time_enabled": False,
                                        "first_day_enabled": False}}


SCENARIOS = {
    "loop_stop_first_s42": _sc_loop_stop_first_s42,
    "loop_ladder_tp_first_s7": _sc_loop_ladder_tp_first_s7,
    "loop_trailing_first_s123": _sc_loop_trailing_first_s123,
    "loop_stop_first_delist_formula_s71": _sc_loop_stop_first_delist_formula_s71,
    "loop_trailing_first_dual_trigger": _sc_loop_trailing_first_dual_trigger,
    "engine_run_cached_stop_first_s42": _sc_engine_run_cached_stop_first_s42,
    "engine_run_cached_trailing_first_s7": _sc_engine_run_cached_trailing_first_s7,
    "loop_real_degraded_defer": _sc_loop_real_degraded_defer,
}

# 5 种 confirm 模式逐一锁定 (2026-08-06 审计 P2; 精巧数据上五模态结果各异)
for _confirm in ("intraday", "low", "close", "simple", "real"):
    SCENARIOS[f"loop_confirm_{_confirm}_crafted"] = _make_loop_confirm_scenario(_confirm)
del _confirm


# ─────────────────────────────────────────────────────────────
# 序列化 (generator 与测试共用, 保证格式一致)
# raw_trades 列: [ci, entry_idx, exit_idx, entry_price, exit_price,
#                 shares, pnl, return, reason] (见 engine._build_trades)
# ─────────────────────────────────────────────────────────────
def equity_to_json(eq):
    """numpy float64 → 原生 float 列表 (json 按 repr 最短精度序列化, 往返位级一致)。"""
    return [float(v) for v in np.asarray(eq, dtype=np.float64)]


def trades_to_json(tr):
    tr = np.atleast_2d(np.asarray(tr, dtype=np.float64))
    if tr.size == 0:
        return []
    rows = []
    for r in tr:
        rows.append([
            int(r[0]), int(r[1]), int(r[2]),
            float(r[3]), float(r[4]),
            int(r[5]),
            float(r[6]), float(r[7]),
            int(r[8]),
        ])
    return rows


def build_snapshot_doc(name, generated_at):
    """跑场景并组装快照文档 (generator 用)。"""
    eq, tr, summary = SCENARIOS[name]()
    meta = {
        "name": name,
        "generator": "tools/gen_snapshot_parity.py",
        "generated_at": generated_at,
        "engine_version": ENGINE_VERSION,
        "note": "批次3a A5 快照基线; 修改策略逻辑会致测试失败, "
                "确认变更合法后运行生成器重建基线",
    }
    meta.update(summary)
    meta["params"] = {
        k: (list(v) if isinstance(v, tuple) else v)
        for k, v in SNAP_PARAMS.items()
    }
    return {"meta": meta,
            "equity": equity_to_json(eq),
            "trades": trades_to_json(tr)}


# ─────────────────────────────────────────────────────────────
# 对比 (精确相等; 失败时给出首个不一致位置)
# ─────────────────────────────────────────────────────────────
def _assert_list_exact(actual, expected, what):
    assert len(actual) == len(expected), (
        f"{what} 长度不一致: actual={len(actual)} snapshot={len(expected)}"
    )
    for i, (a, e) in enumerate(zip(actual, expected)):
        assert a == e, (
            f"{what} 第 {i} 项不一致: actual={a!r} snapshot={e!r}\n"
            f"  若为合法行为变更, 运行 python -X utf8 tools/gen_snapshot_parity.py 重建基线"
        )


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_snapshot_parity(name):
    """快照逐字段对比: equity 全数组 + trades 全行全列, 精确相等。"""
    path = os.path.join(SNAPSHOT_DIR, f"{name}.json")
    if not os.path.exists(path):
        pytest.fail(
            f"快照缺失: {path}\n运行 python -X utf8 tools/gen_snapshot_parity.py 生成")
    with open(path, "r", encoding="utf-8") as f:
        snap = json.load(f)
    assert "meta" in snap and "equity" in snap and "trades" in snap, (
        f"快照 {name} 结构不完整 (缺 meta/equity/trades)")

    eq, tr, _ = SCENARIOS[name]()
    _assert_list_exact(equity_to_json(eq), snap["equity"], f"{name}.equity")
    _assert_list_exact(trades_to_json(tr), snap["trades"], f"{name}.trades")


# ─────────────────────────────────────────────────────────────
# run vs run_cached 同参一致性守卫 (计划书批次3: 先查因再固化, 防双入口漂移)
# ─────────────────────────────────────────────────────────────
def test_run_vs_run_cached_consistency(monkeypatch):
    """同一份数据 + 同一 stop_config, engine.run() 与 run_cached() 输出必须一致。

    run() 的取数段 (DataFetcher.get_kline) 与涨停过滤 (_filter_limit_up) mock 掉,
    两入口喂入**完全相同**的矩阵 (经 spy 捕获 run() 的 _prepare_run_matrices 产物),
    锁定的是参数准备段 + 核心循环 + 后处理的一致性。
    """
    import backtest.engine as engine_module

    n, k = 40, 3
    price, high, low, open_, _ = make_synthetic(seed=2026, n_dates=n, n_stocks=k)
    dates = pd.bdate_range("2024-01-02", periods=n)
    cols = ["600000.SH", "600001.SH", "000001.SZ"]
    close_df = pd.DataFrame(price, index=dates, columns=cols)
    kline = {
        "Close": close_df,
        "High": pd.DataFrame(high, index=dates, columns=cols),
        "Low": pd.DataFrame(low, index=dates, columns=cols),
        "Open": pd.DataFrame(open_, index=dates, columns=cols),
    }
    selections = pd.DataFrame([
        {"select_date": dates[5], "stock_code": cols[0]},
        {"select_date": dates[8], "stock_code": cols[1]},
        {"select_date": dates[12], "stock_code": cols[2]},
    ])
    sc = _stop_config_snapshot("trailing_first")

    eng = _make_engine()
    monkeypatch.setattr(
        engine_module.DataFetcher, "get_kline", lambda *a, **kw: kline)
    monkeypatch.setattr(
        BacktestEngine, "_filter_limit_up", lambda self, e, c: e)

    captured = {}
    orig_prep = BacktestEngine._prepare_run_matrices

    def _spy_prep(self, sel, start, end, win_td):
        prep = orig_prep(self, sel, start, end, win_td)
        captured.update(prep or {})
        return prep

    monkeypatch.setattr(BacktestEngine, "_prepare_run_matrices", _spy_prep)

    res_run = eng.run(selections=selections,
                      start_time="20240102", end_time="20240227",
                      stop_config=sc)
    assert captured, "_prepare_run_matrices 未被调用, run() 路径异常"

    lp, lr, nl = _ladder_triplet(sc)
    prepared = PreparedMatrix(
        close=captured["close"], entries=captured["entries"],
        high_np=captured["high"], low_np=captured["low"],
        open_np=captured["open"],
        tradable_np=captured["tradable"],
        last_tradable_idx=captured["last_tradable_idx"])
    res_cached = eng.run_cached(
        prepared, sc, lp, lr, nl,
        filter_limit_up=False,
        return_raw=True,
    )

    # equity: run 的 equity_curve vs run_cached 的 raw_equity, 必须逐点一致
    eq_run = res_run["equity_curve"]["equity"].to_numpy(dtype=np.float64)
    eq_cached = np.asarray(res_cached["raw_equity"], dtype=np.float64)
    assert np.array_equal(eq_run, eq_cached), (
        "run vs run_cached equity 不一致 (疑似双入口漂移, 先查因再固化):\n"
        f"  run[-3:]={eq_run[-3:]}\n  cached[-3:]={eq_cached[-3:]}"
    )
    # trades: 后处理产物逐字段一致
    pd.testing.assert_frame_equal(res_run["trades"], res_cached["trades"])


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
