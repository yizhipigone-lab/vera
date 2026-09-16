"""
engine.run() / engine.run_cached() 参数契约回归测试

锁定问题 (2026-06-26 A2 先生先生先生先生发现):
  engine.run() 路径错把 eff_slippage/eff_stamp_tax 两个成本参数塞进位置参数,
  但核心函数签名只接受 commission, 后续 first_day_enabled 关键字
  与位置参数重复, 触发 'got multiple values for argument' 错误, 前端先生先生先生根本跑不通.

修复: 引擎调用方全部用 keyword 传核心循环的所有"非必需"参数.

2026-08-01 批次 3b C2: `_simulate_core_v3` 测试兼容壳退役 — 契约对象改为
`backtest.loop.build_backtest_loop` (生产直调入口); run/run_cached 的 build+run
已并入共享段 `_resolve_stop_and_build_loop`, 源码扫描同步改扫共享段。

本测试锁死:
  1. build_backtest_loop 函数签名不被误改 (位置参数 ≤ 20 个)
  2. 直调核心循环 (tests/loop_direct.py, 原壳等价展开) 必须能正确接受所有 keyword 参数
  3. engine.py 源码扫描: 共享段调 build_backtest_loop 时
     slippage/stamp_tax 必须用 keyword 形式 (防 'multiple values' bug 回归)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inspect

import numpy as np
import pandas as pd
import pytest

import backtest.engine as engine_module
import backtest.stop_config as stop_config_module
from backtest.engine import BacktestEngine
from backtest.loop import build_backtest_loop
from backtest.prepared import PreparedMatrix
from backtest.stop_config import load_stop_config
from tests.loop_direct import run_loop_direct


# === 1. 函数签名契约测试 ===
class TestBuildBacktestLoopSignature:
    """build_backtest_loop 函数签名契约 — 防止后续有人误改位置参数顺序

    2026-08-01 批次 3b C2: 原 _simulate_core_v3 壳 (22 位置参数 = 2 矩阵 + 20 核心)
    退役, 契约改锁 build_backtest_loop (20 个必需位置参数, 矩阵参数归 loop.run)。
    """

    def test_function_signature_unchanged(self):
        """核心契约: 函数位置参数个数不超过 20 个 (锁定 2026-08-01 baseline)"""
        sig = inspect.signature(build_backtest_loop)
        positional_count = sum(
            1 for p in sig.parameters.values()
            if p.default == inspect.Parameter.empty
        )
        # 当前定义: 20 个位置参数
        # 注意: 如果加新位置参数, 同步更新 _resolve_stop_and_build_loop 调用点.
        # 上限 20 即当前值 (新增必需参数要审查)
        assert positional_count <= 20, (
            f"build_backtest_loop 位置参数个数 {positional_count} > 20. "
            f"如需新增位置参数, 请同步更新 _resolve_stop_and_build_loop 调用点."
        )
        positional_count = sum(
            1 for p in sig.parameters.values()
            if p.default == inspect.Parameter.empty
        )

    def test_keyword_only_params_present(self):
        """契约: 下列参数必须存在且为 keyword-only 或带默认值"""
        sig = inspect.signature(build_backtest_loop)
        required_keywords = [
            'first_day_enabled', 'first_day_target', 'bpday',
            'slippage', 'stamp_tax',
            # P-v3.4: 公式卖出 (formula_sell) 的 3 个 keyword
            'formula_exit_np', 'formula_exit_ratio', 'formula_exit_lag_bars',
        ]
        for kw in required_keywords:
            assert kw in sig.parameters, (
                f"build_backtest_loop 缺少关键字参数 [{kw}]. "
                f"如需重命名, 请同步更新调用点."
            )
            p = sig.parameters[kw]
            assert p.default != inspect.Parameter.empty, (
                f"{kw} 必须有默认值 (默认 None/1.0/1), 否则调用方被强制要求传."
            )

    def test_keyword_only_params_whitelist(self):
        """契约: keyword 参数白名单 — 新增 keyword 参数必须显式登记 (2026-09-15 审计)。

        位置参数 ≤20 有守卫, 但 keyword 增长对守卫静默 (40→42 就是这样发生的:
        buy_price_np 2026-08-20 / max_turnover_pct 2026-08-28, 均有记录但守卫
        无感)。白名单不是禁止新增 —— 把新参数名加进来即视为"已审查"。
        """
        sig = inspect.signature(build_backtest_loop)
        optional = {name for name, p in sig.parameters.items()
                    if p.default != inspect.Parameter.empty}
        expected = {
            'first_day_enabled', 'first_day_target', 'bpday',
            'slippage', 'stamp_tax', 'max_position_pct',
            'ladder_tp_first', 'trailing_first',
            'formula_exit_np', 'formula_exit_ratio', 'formula_exit_lag_bars',
            'atr_enabled', 'atr_matrix', 'atr_multiplier',
            'trailing_gap_protection', 'trailing_confirm',
            'sell_cooldown_bars', 'max_total_exposure',
            'loss_streak_halt_n', 'loss_streak_halt_bars',
            'buy_price_np', 'max_turnover_pct',
        }
        assert optional == expected, (
            f"keyword 参数集漂移: 多出 {sorted(optional - expected)}, "
            f"缺少 {sorted(expected - optional)}. "
            f"新增 keyword 参数请显式登记到本白名单 (视为已审查)."
        )

    def test_slippage_and_stamp_tax_have_defaults(self):
        """slippage 和 stamp_tax 必须有默认值 (不能是必需位置参数)"""
        sig = inspect.signature(build_backtest_loop)
        for kw in ['slippage', 'stamp_tax']:
            p = sig.parameters[kw]
            assert p.default != inspect.Parameter.empty, (
                f"{kw} 必须有默认值, 否则 engine 调用方会被强制要求传."
            )

    def test_formula_exit_new_params_have_safe_defaults(self):
        """P-v3.4: formula_exit_* 三个新参数的默认值必须保证"不启用"语义"""
        sig = inspect.signature(build_backtest_loop)
        assert sig.parameters['formula_exit_np'].default is None, "formula_exit_np 默认应 None (不启用)"
        assert sig.parameters['formula_exit_ratio'].default == 1.0, "formula_exit_ratio 默认 1.0"
        assert sig.parameters['formula_exit_lag_bars'].default == 1, "formula_exit_lag_bars 默认 1 (T+1)"


# === 2. 核心循环直接调用契约测试 ===
class TestCoreLoopDirectCall:
    """直接调 run_loop_direct (原壳等价展开) — 验证所有 keyword 参数可正常接收"""

    def _make_synthetic_data(self, n_dates=20, n_stocks=3, seed=42):
        """构造一份小合成 K 线数据"""
        np.random.seed(seed)
        dates = pd.bdate_range('2024-01-02', periods=n_dates)
        close = pd.DataFrame(
            np.random.uniform(95, 105, size=(n_dates, n_stocks)).cumsum(axis=0) + 100,
            index=dates,
            columns=[f'{i:06d}.SZ' for i in range(1, n_stocks + 1)],
        )
        high = close * 1.02
        low = close * 0.98
        entries = pd.DataFrame(False, index=dates, columns=close.columns)
        entries.iloc[5, 0] = True
        return close, high, low, entries

    def test_call_with_all_keyword_args(self):
        """核心测试: 用全 keyword 形式调用 run_loop_direct 必须成功"""
        close, high, low, entries = self._make_synthetic_data()
        stop_cfg = load_stop_config()
        cost = stop_cfg.get("cost_stop", {})
        trail = stop_cfg.get("trailing_stop", {})
        time_s = stop_cfg.get("time_stop", {})
        cond_t = stop_cfg.get("cond_time_stop", {})
        first_day = stop_cfg.get("first_day", {})
        ladder = stop_cfg.get("ladder_tp", {})
        levels = ladder.get("levels", [])
        lv = sorted(levels, key=lambda x: x.get("profit", 0))
        ladder_profits = np.array([lv[i]["profit"] for i in range(len(lv))], dtype=np.float64)
        ladder_ratios = np.array([lv[i]["sell_ratio"] for i in range(len(lv))], dtype=np.float64)

        mhd_scaled = 20
        ctd_scaled = 7

        # 关键: 用全 keyword 形式调用, slippage/stamp_tax 必须用 keyword
        equity_arr, raw_trades = run_loop_direct(
            price_np=close.values.astype(np.float64),
            entry_np=entries.values,
            initial_capital=100_000.0,
            commission=0.0003,
            min_buy_amount=1000.0,
            max_buy_amount=5000.0,
            lot_size=100,
            min_lots=1,
            cost_stop_enabled=cost.get("enabled", True),
            cost_stop_threshold=float(cost.get("threshold", -0.08)),
            trailing_enabled=trail.get("enabled", True),
            trailing_activation=float(trail.get("activation", 0.05)),
            trailing_drawdown=float(trail.get("drawdown", 0.03)),
            ladder_enabled=ladder.get("enabled", True),
            ladder_profits=ladder_profits,
            ladder_ratios=ladder_ratios,
            n_ladder=len(lv),
            time_enabled=time_s.get("enabled", True),
            max_hold_days=mhd_scaled,
            cond_time_enabled=cond_t.get("enabled", False),
            cond_time_days=ctd_scaled,
            cond_time_profit=float(cond_t.get("profit", 0.01)),
            first_day_enabled=first_day.get("enabled", False),
            first_day_target=float(first_day.get("target", 0.03)),
            first_day_n_bars=1,
            high_np=high.values.astype(np.float64),
            low_np=low.values.astype(np.float64),
            bpday=1,
            slippage=0.001,
            stamp_tax=0.0005,
        )
        # 验证返回结构
        assert isinstance(equity_arr, np.ndarray)
        assert equity_arr.shape == (close.shape[0],)
        assert equity_arr[-1] > 0  # 净值是正数
        # 验证 raw_trades 是 numpy array (空数组也算合法)
        assert isinstance(raw_trades, np.ndarray)

    def test_entry_uses_signal_day_close_not_next_day_open(self):
        """【HIGH-T1 回归锁】 信号日收盘价买入: entry_bar == 信号日,
        不允许未来又改回 T+1 次日开盘买入."""
        # 固定低价 (5 元/股), max_buy=5000 能买 1000 股, 开 time_stop=3 强制平仓拿到 1 笔 trade
        n_dates, n_stocks = 20, 1
        dates = pd.bdate_range('2024-01-02', periods=n_dates)
        columns = ['600519.SH']
        close = pd.DataFrame(np.full((n_dates, n_stocks), 5.0), index=dates, columns=columns)
        high = close * 1.02; low = close * 0.98
        entries = pd.DataFrame(False, index=dates, columns=close.columns)
        entries.iloc[5, 0] = True  # 信号日 = bar 5

        _, raw_trades = run_loop_direct(
            price_np=close.values.astype(np.float64),
            entry_np=entries.values,
            initial_capital=100_000.0,
            commission=0.0003,
            min_buy_amount=1000.0,
            max_buy_amount=5000.0,
            lot_size=100,
            min_lots=1,
            cost_stop_enabled=False, cost_stop_threshold=-0.30,
            trailing_enabled=False, trailing_activation=0.05, trailing_drawdown=0.03,
            ladder_enabled=False, ladder_profits=np.array([]), ladder_ratios=np.array([]), n_ladder=0,
            # time_stop 强制 3 天后平仓 → 保证 raw_trades 至少 1 笔
            time_enabled=True, max_hold_days=3,
            cond_time_enabled=False, cond_time_days=9999, cond_time_profit=0.99,
            first_day_enabled=False, first_day_target=0.99, first_day_n_bars=1,
            high_np=high.values.astype(np.float64),
            low_np=low.values.astype(np.float64),
            bpday=1,
            slippage=0.0, stamp_tax=0.0,
        )
        assert len(raw_trades) >= 1, "应至少 1 笔交易 (time_stop 强制平仓)"
        entry_bar = int(raw_trades[0][1])
        assert entry_bar == 5, (
            f"违反 F1 原则: 买入 bar 必须=信号日 (5). 实际 {entry_bar}. "
            f"如非 5, 说明有人把代码改回了 T+1 次日开盘买入."
        )

    def test_signal_day_suspended_skip_buy(self):
        """【F1 + T1】信号日停牌 → 必须 skip, 不能用 ffill 假价成交."""
        n_dates, n_stocks = 20, 1
        dates = pd.bdate_range('2024-01-02', periods=n_dates)
        columns = ['600519.SH']
        close = pd.DataFrame(np.full((n_dates, n_stocks), 5.0), index=dates, columns=columns)
        high = close * 1.02; low = close * 0.98
        entries = pd.DataFrame(False, index=dates, columns=close.columns)
        entries.iloc[5, 0] = True
        # tradable_np: bar 5 这只股票停牌
        tradable = np.ones((n_dates, n_stocks), dtype=bool)
        tradable[5, 0] = False

        _, raw_trades = run_loop_direct(
            price_np=close.values.astype(np.float64),
            entry_np=entries.values,
            initial_capital=100_000.0, commission=0.0003,
            min_buy_amount=1000.0, max_buy_amount=5000.0,
            lot_size=100, min_lots=1,
            cost_stop_enabled=False, cost_stop_threshold=-0.30,
            trailing_enabled=False, trailing_activation=0.05, trailing_drawdown=0.03,
            ladder_enabled=False, ladder_profits=np.array([]), ladder_ratios=np.array([]), n_ladder=0,
            time_enabled=True, max_hold_days=3,  # 同上, 让正常情况有 trade; 停牌时无
            cond_time_enabled=False, cond_time_days=9999, cond_time_profit=0.99,
            first_day_enabled=False, first_day_target=0.99, first_day_n_bars=1,
            high_np=high.values.astype(np.float64),
            low_np=low.values.astype(np.float64),
            tradable_np=tradable,
            bpday=1, slippage=0.0, stamp_tax=0.0,
        )
        assert len(raw_trades) == 0, (
            f"信号日停牌必须 skip, 不能用 ffill 假价成交. 实际有 {len(raw_trades)} 笔"
        )

    def test_call_without_slippage_stamp_tax_uses_defaults(self):
        """slippage/stamp_tax 走默认值 (不传) 必须能跑, 默认 slippage=0/stamp_tax=0"""
        close, high, low, entries = self._make_synthetic_data()
        # 最小化调用, 走默认值
        equity_arr, raw_trades = run_loop_direct(
            close.values.astype(np.float64),
            entries.values,
            100_000.0, 0.0003,
            1000.0, 5000.0, 100, 1,
            True, -0.08, True, 0.05, 0.03,
            True, np.array([0.06, 0.15]), np.array([0.30, 0.30]), 2,
            True, 20, False, 7, 0.01,
        )
        assert equity_arr.shape == (close.shape[0],)


# === 3. engine 源码扫描 — 防有人未来再加新参数忘了改调用点 ===
class TestNoNewPositionalParamsAdded:
    """源码级守卫: 检查 build_backtest_loop 调用点是否还混用错位位置参数

    2026-08-01 批次 3b C2: build+run 已并入共享段 _resolve_stop_and_build_loop,
    扫描目标从 run/run_cached body 改为共享段 body。
    """

    def _read_engine_src(self):
        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'backtest', 'engine.py')
        with open(src_path, 'r', encoding='utf-8') as f:
            return f.read()

    def _shared_segment_body(self, src):
        start = src.find('def _resolve_stop_and_build_loop(')
        end = src.find('def run(self, selections')
        assert start != -1 and end != -1 and start < end, "共享段 _resolve_stop_and_build_loop 定位失败"
        return src[start:end]

    def test_shared_segment_uses_keyword_for_slippage_stamp_tax(self):
        """共享段调 build_backtest_loop 时, slippage/stamp_tax 必须用 keyword"""
        body = self._shared_segment_body(self._read_engine_src())

        assert 'slippage=' in body, (
            "_resolve_stop_and_build_loop 调 build_backtest_loop 时缺少 slippage= keyword. "
            "这是 2026-06-26 A2 发现的真实 bug, 修复方式是用 keyword 传."
        )
        assert 'stamp_tax=' in body, (
            "_resolve_stop_and_build_loop 调 build_backtest_loop 时缺少 stamp_tax= keyword."
        )

    def test_both_entries_call_shared_segment(self):
        """2026-08-01 批次 3b C2: run()/run_cached() 都必须经共享段, 不得各自 build。"""
        src = self._read_engine_src()
        run_start = src.find('def run(self, selections')
        run_end = src.find('def run_cached(self, prepared')
        cached_end = src.find('def _build_trades')
        run_body = src[run_start:run_end]
        cached_body = src[run_end:cached_end]
        assert 'self._resolve_stop_and_build_loop(' in run_body, (
            "run() 未走共享段 _resolve_stop_and_build_loop")
        assert 'self._resolve_stop_and_build_loop(' in cached_body, (
            "run_cached() 未走共享段 _resolve_stop_and_build_loop")
        # 两入口不得再各自直调 build_backtest_loop (防重复段死灰复燃)
        assert 'build_backtest_loop(' not in run_body, (
            "run() 绕过共享段直调 build_backtest_loop")
        assert 'build_backtest_loop(' not in cached_body, (
            "run_cached() 绕过共享段直调 build_backtest_loop")

    def test_no_multiple_values_error_pattern(self):
        """源码扫描: 不应该出现 'got multiple values' 的位置/关键字混传模式"""
        src = self._read_engine_src()
        # 检查所有 build_backtest_loop 调用点, 看 first_day_enabled 是否同时是位置和 keyword
        import re
        calls = re.findall(r'build_backtest_loop\((.*?)\)', src, re.DOTALL)
        for i, call in enumerate(calls):
            # 简化: 检查 first_day_enabled 是否在 keyword 形式出现
            # 如果同时是位置传 (在 first_day_enabled= 之前) 和 keyword, 就报错
            # 但这是粗略检查, 主要看两种参数 (slippage, stamp_tax) 是否混传
            if 'first_day_enabled=' in call and 'slippage' in call:
                # 确保 slippage 是 keyword 形式, 不在 first_day_enabled 之前的位置
                slippage_pos = call.find('slippage')
                first_day_kw_pos = call.find('first_day_enabled=')
                # 如果 slippage 在 first_day_enabled 之前 (位置), 且后面又有 slippage=, 说明重复
                if slippage_pos < first_day_kw_pos and 'slippage=' in call:
                    # 这是合法的 — 前面可能是 docstring 或注释
                    # 真正要看的是 slippage 位置参数 + slippage= keyword 重复
                    if re.search(r'\bslippage\b(?!\s*=)', call[:first_day_kw_pos]) and 'slippage=' in call:
                        pytest.fail(
                            f"build_backtest_loop 调用 #{i+1} 中 slippage 同时是位置参数和 keyword — "
                            f"会触发 'multiple values' 错"
                        )


# === 4. run_cached 签名契约测试 (候选 A 阶段 1: 加厚前门) ===
class TestRunCachedSignature:
    """run_cached 前门收敛后 (P2-1) 的签名契约 — 5 位置参数 (prepared + stop_config
    + ladder_profits + ladder_ratios + n_ladder) + 6 keyword-only 能力参数。

    close/entries/high_np/low_np/open_np/tradable_np/last_tradable_idx 已收进
    PreparedMatrix, 死参数 selections 已删。
    """

    def test_run_cached_positional_params_unchanged(self):
        """核心契约: 必需位置参数 = 6 (含 self; 即 prepared + stop_config +
        ladder_profits + ladder_ratios + n_ladder)。"""
        sig = inspect.signature(BacktestEngine.run_cached)
        positional_count = sum(
            1 for p in sig.parameters.values()
            if p.default == inspect.Parameter.empty
            and p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                           inspect.Parameter.POSITIONAL_OR_KEYWORD)
        )
        assert positional_count == 6, (
            f"run_cached 必需位置参数个数 {positional_count} != 6 (self+5). "
            f"P2-1 收敛后应为 prepared/stop_config/ladder_profits/ladder_ratios/n_ladder。"
        )

    def test_run_cached_capability_kwargs_keyword_only(self):
        """6 个能力参数必须 keyword-only + 有默认值 (None=能力 off)。"""
        sig = inspect.signature(BacktestEngine.run_cached)
        required_kw = {
            'filter_limit_up', 'formula_exit_np', 'formula_exit_ratio',
            'formula_exit_lag_bars', 'close_raw', 'return_raw',
        }
        for kw in required_kw:
            assert kw in sig.parameters, f"run_cached 缺少 keyword 参数 [{kw}]"
            p = sig.parameters[kw]
            assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
                f"{kw} 必须 keyword-only (防位置传参错位)"
            )
            assert p.default != inspect.Parameter.empty, (
                f"{kw} 必须有默认值 (不传 = 能力 off)"
            )

    def test_run_cached_capability_kwargs_default_values(self):
        """6 keyword 的精确默认值 (filter_limit_up=True 是命脉, 防被改 False)。"""
        sig = inspect.signature(BacktestEngine.run_cached)
        expected_defaults = {
            'filter_limit_up': True,
            'formula_exit_np': None,
            'formula_exit_ratio': None,
            'formula_exit_lag_bars': 1,
            'close_raw': None,
            'return_raw': False,
        }
        for kw, expected in expected_defaults.items():
            actual = sig.parameters[kw].default
            assert actual == expected, (
                f"run_cached.{kw} 默认值应为 {expected!r}, 实际 {actual!r} — "
                f"改默认值会破坏行为 (filter_limit_up=True 是命脉)"
            )

    def test_run_cached_forwards_capability_keywords(self):
        """源码守卫: run_cached 读 capabilities + 共享段 loop.run 必须透传能力 keyword.

        2026-08-01 批次 3b C2: build+run 并入 _resolve_stop_and_build_loop,
        能力透传 keyword 的扫描目标从 run_cached body 改为共享段 body;
        run_cached body 仍须读 capabilities/return_raw 并调共享段。
        """
        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'backtest', 'engine.py')
        with open(src_path, 'r', encoding='utf-8') as f:
            src = f.read()
        cached_start = src.find('def run_cached(self, prepared')
        cached_end = src.find('def _build_trades')
        cached_body = src[cached_start:cached_end]
        seg_start = src.find('def _resolve_stop_and_build_loop(')
        seg_end = src.find('def run(self, selections')
        seg_body = src[seg_start:seg_end]
        for kw in ['tradable_np=', 'last_tradable_idx=', 'open_np=',
                   'formula_exit_np=', 'formula_exit_ratio=']:
            assert kw in seg_body, (
                f"共享段调 loop.run 时缺少 {kw} keyword (三类能力透传)"
            )
        assert 'capabilities' in cached_body, "run_cached body 必须读 capabilities 开关"
        assert 'return_raw' in cached_body, "run_cached body 必须支持 return_raw"
        assert '_resolve_stop_and_build_loop(' in cached_body, (
            "run_cached 必须经共享段 _resolve_stop_and_build_loop (2026-08-01 C2)")


# === 5. 任务 3: run() / run_cached() 移动止损止盈默认值契约 ===
# 锁定:
#   1. 缺字段 / None → 默认值 0.035 / 0.01 (与 config/default.yaml 对齐)
#   2. 显式 0.08/0.05 与 0.0/0.0 必须保留 (0.0 是合法显式值, 不能用 or 兜底)
#   3. build_backtest_loop 的位置参数索引 9/10 必须保持 trailing_activation / trailing_drawdown


def _default_contract_engine():
    return BacktestEngine({
        "initial_capital": 100_000.0,
        "commission": 0.0003,
        "enable_realistic_costs": False,
        "period": "1d",
        "position_sizing": {
            "min_buy_amount": 1000.0,
            "max_buy_amount": 5000.0,
            "lot_size": 100,
            "min_lots": 1,
        },
    })


def _default_contract_market():
    dates = pd.bdate_range("2024-01-02", periods=3)
    close = pd.DataFrame(
        {"000001.SZ": [10.0, 10.1, 10.2]},
        index=dates,
    )
    entries = pd.DataFrame(False, index=dates, columns=close.columns)
    entries.iloc[0, 0] = True
    return close, entries


def _capture_core_trailing(monkeypatch, n_bars):
    captured = {}
    parameter_names = list(
        inspect.signature(engine_module.build_backtest_loop).parameters
    )
    activation_index = parameter_names.index("trailing_activation")
    drawdown_index = parameter_names.index("trailing_drawdown")
    assert activation_index == 9, f"activation at {activation_index}, not 9"
    assert drawdown_index == 10, f"drawdown at {drawdown_index}, not 10"

    class _MockLoop:
        def run(self, *a, **kw):
            return (np.full(n_bars, 100_000.0, dtype=np.float64),
                    np.empty((0, 9), dtype=np.float64))
    def fake_builder(*args, **kwargs):
        # 捕获 build_backtest_loop 的 positional args
        if len(args) > drawdown_index:
            captured["activation"] = args[activation_index]
            captured["drawdown"] = args[drawdown_index]
        return _MockLoop()

    monkeypatch.setattr(engine_module, "build_backtest_loop", fake_builder)
    return captured


@pytest.mark.parametrize(
    "trailing_config",
    [
        {"enabled": True},
        {"enabled": True, "activation": None, "drawdown": None},
    ],
)
def test_run_cached_uses_yaml_defaults_when_trailing_values_missing(
    monkeypatch, trailing_config
):
    engine = _default_contract_engine()
    close, entries = _default_contract_market()
    captured = _capture_core_trailing(monkeypatch, len(close))

    prepared = PreparedMatrix(close=close, entries=entries,
                              high_np=None, low_np=None)
    engine.run_cached(
        prepared,
        {"trailing_stop": trailing_config},
        np.array([], dtype=np.float64),
        np.array([], dtype=np.float64),
        0,
        filter_limit_up=False,
    )

    assert captured == {"activation": 0.035, "drawdown": 0.01}


@pytest.mark.parametrize(
    ("activation", "drawdown"),
    [(0.08, 0.05), (0.0, 0.0)],
)
def test_run_cached_preserves_explicit_trailing_values(
    monkeypatch, activation, drawdown
):
    engine = _default_contract_engine()
    close, entries = _default_contract_market()
    captured = _capture_core_trailing(monkeypatch, len(close))

    prepared = PreparedMatrix(close=close, entries=entries,
                              high_np=None, low_np=None)
    engine.run_cached(
        prepared,
        {
            "trailing_stop": {
                "enabled": True,
                "activation": activation,
                "drawdown": drawdown,
            },
        },
        np.array([], dtype=np.float64),
        np.array([], dtype=np.float64),
        0,
        filter_limit_up=False,
    )

    assert captured == {"activation": activation, "drawdown": drawdown}


def _run_with_captured_trailing(monkeypatch, trailing_config):
    engine = _default_contract_engine()
    close, _ = _default_contract_market()
    kline = {
        "Close": close,
        "High": close * 1.01,
        "Low": close * 0.99,
        "Open": close.copy(),
    }
    selections = pd.DataFrame([
        {
            "select_date": close.index[0],
            "stock_code": close.columns[0],
        },
    ])
    captured = _capture_core_trailing(monkeypatch, len(close))
    monkeypatch.setattr(
        engine_module.DataFetcher,
        "get_kline",
        lambda *args, **kwargs: kline,
    )
    monkeypatch.setattr(
        BacktestEngine,
        "_filter_limit_up",
        lambda self, entries, prices: entries,
    )
    # 本测试只验证引擎传给核心循环的参数；摘要 None 语义在 Task 5 独立验证。
    monkeypatch.setattr(
        stop_config_module,
        "get_stop_config_summary",
        lambda config: "",
    )

    engine.run(
        selections=selections,
        start_time="20240102",
        end_time="20240104",
        stop_config={"trailing_stop": trailing_config},
    )
    return captured


@pytest.mark.parametrize(
    "trailing_config",
    [
        {"enabled": True},
        {"enabled": True, "activation": None, "drawdown": None},
    ],
)
def test_run_uses_yaml_defaults_when_trailing_values_missing(
    monkeypatch, trailing_config
):
    captured = _run_with_captured_trailing(monkeypatch, trailing_config)
    assert captured == {"activation": 0.035, "drawdown": 0.01}


@pytest.mark.parametrize(
    ("activation", "drawdown"),
    [(0.08, 0.05), (0.0, 0.0)],
)
def test_run_preserves_explicit_trailing_values(
    monkeypatch, activation, drawdown
):
    captured = _run_with_captured_trailing(
        monkeypatch,
        {
            "enabled": True,
            "activation": activation,
            "drawdown": drawdown,
        },
    )
    assert captured == {"activation": activation, "drawdown": drawdown}


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
