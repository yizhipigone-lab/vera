"""
engine.run() / engine.run_cached() 参数契约回归测试

锁定问题 (2026-06-26 A2 先生先生先生先生发现):
  engine.run() 路径错把 eff_slippage/eff_stamp_tax 两个成本参数塞进位置参数,
  但 _simulate_core_v3 函数签名只接受 commission, 后续 first_day_enabled 关键字
  与位置参数重复, 触发 'got multiple values for argument' 错误, 前端先生先生先生根本跑不通.

修复: 引擎调用方全部用 keyword 传 _simulate_core_v3 的所有"非必需"参数.

本测试锁死:
  1. _simulate_core_v3 函数签名不被误改 (位置参数 ≤ 22 个)
  2. _simulate_core_v3 直接调用 (不走 engine) 必须能正确接受所有 keyword 参数
  3. engine.py 源码扫描: engine.run() 和 run_cached() 调用 _simulate_core_v3 时
     slippage/stamp_tax 必须用 keyword 形式 (防 'multiple values' bug 回归)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inspect
import numpy as np
import pandas as pd
import pytest

from backtest.engine import _simulate_core_v3
from backtest.stop_config import load_stop_config


# === 1. 函数签名契约测试 ===
class TestSimulateCoreV3Signature:
    """_simulate_core_v3 函数签名契约 — 防止后续有人误改位置参数顺序"""

    def test_function_signature_unchanged(self):
        """核心契约: 函数位置参数个数不超过 22 个 (锁定 2026-06-26 baseline)"""
        sig = inspect.signature(_simulate_core_v3)
        positional_count = sum(
            1 for p in sig.parameters.values()
            if p.default == inspect.Parameter.empty
        )
        # 当前定义: 22 个位置参数
        # 注意: 如果加新位置参数, 同步更新 engine.run() 和 run_cached() 两个调用点.
        # 上限 22 留缓冲 (允许小幅扩展, 但大幅扩张要审查)
        assert positional_count <= 22, (
            f"_simulate_core_v3 位置参数个数 {positional_count} > 22. "
            f"如需新增位置参数, 请同步更新 engine.run() 和 run_cached() 两个调用点."
        )

    def test_keyword_only_params_present(self):
        """契约: 下列参数必须存在且为 keyword-only 或带默认值"""
        sig = inspect.signature(_simulate_core_v3)
        required_keywords = [
            'first_day_enabled', 'first_day_target', 'first_day_n_bars',
            'slippage', 'stamp_tax', 'high_np', 'low_np', 'bpday',
        ]
        for kw in required_keywords:
            assert kw in sig.parameters, (
                f"_simulate_core_v3 缺少关键字参数 [{kw}]. "
                f"如需重命名, 请同步更新两个调用点."
            )

    def test_slippage_and_stamp_tax_have_defaults(self):
        """slippage 和 stamp_tax 必须有默认值 (不能是必需位置参数)"""
        sig = inspect.signature(_simulate_core_v3)
        for kw in ['slippage', 'stamp_tax']:
            p = sig.parameters[kw]
            assert p.default != inspect.Parameter.empty, (
                f"{kw} 必须有默认值, 否则 engine 调用方会被强制要求传."
            )


# === 2. _simulate_core_v3 直接调用契约测试 ===
class TestSimulateCoreV3DirectCall:
    """直接调 _simulate_core_v3 — 验证所有 keyword 参数可正常接收"""

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
        """核心测试: 用全 keyword 形式调用 _simulate_core_v3 必须成功"""
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
        equity_arr, raw_trades = _simulate_core_v3(
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

    def test_call_without_slippage_stamp_tax_uses_defaults(self):
        """slippage/stamp_tax 走默认值 (不传) 必须能跑, 默认 slippage=0/stamp_tax=0"""
        close, high, low, entries = self._make_synthetic_data()
        # 最小化调用, 走默认值
        equity_arr, raw_trades = _simulate_core_v3(
            close.values.astype(np.float64),
            entries.values,
            100_000.0, 0.0003,
            1000.0, 5000.0, 100, 1,
            True, -0.08, True, 0.05, 0.03,
            True, np.array([0.06, 0.15]), np.array([0.30, 0.30]), 2,
            True, 20, False, 7, 0.01,
        )
        assert equity_arr.shape == (close.shape[0],)


# === 3. engine 源码扫描 — 防有人未来再加新参数忘了改两处调用 ===
class TestNoNewPositionalParamsAdded:
    """源码级守卫: 检查 _simulate_core_v3 调用点是否还混用错位位置参数"""

    def _read_engine_src(self):
        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'backtest', 'engine.py')
        with open(src_path, 'r', encoding='utf-8') as f:
            return f.read()

    def test_engine_run_uses_keyword_for_slippage_stamp_tax(self):
        """engine.run() 调用 _simulate_core_v3 时, slippage/stamp_tax 必须用 keyword"""
        src = self._read_engine_src()
        run_start = src.find('def run(self, selections')
        run_end = src.find('def run_cached(self, close')
        run_body = src[run_start:run_end]

        assert 'slippage=' in run_body, (
            "engine.run() 调用 _simulate_core_v3 时缺少 slippage= keyword. "
            "这是 2026-06-26 A2 发现的真实 bug, 修复方式是用 keyword 传."
        )
        assert 'stamp_tax=' in run_body, (
            "engine.run() 调用 _simulate_core_v3 时缺少 stamp_tax= keyword."
        )

    def test_run_cached_uses_keyword_for_slippage_stamp_tax(self):
        """engine.run_cached() 调用 _simulate_core_v3 时, slippage/stamp_tax 必须用 keyword"""
        src = self._read_engine_src()
        cached_start = src.find('def run_cached(self, close')
        cached_end = src.find('def _build_trades')
        cached_body = src[cached_start:cached_end]

        assert 'slippage=' in cached_body, (
            "engine.run_cached() 调用 _simulate_core_v3 时缺少 slippage= keyword. "
            "先生先生先生发现: run_cached 路径先生先生先生先生先生先生先生先生先生先生没接 enable_realistic_costs, "
            "用 keyword 传 slippage/stamp_tax 是修复方式."
        )
        assert 'stamp_tax=' in cached_body, (
            "engine.run_cached() 调用 _simulate_core_v3 时缺少 stamp_tax= keyword."
        )

    def test_no_multiple_values_error_pattern(self):
        """源码扫描: 不应该出现 'got multiple values' 的位置/关键字混传模式"""
        src = self._read_engine_src()
        # 检查所有 _simulate_core_v3 调用点, 看 first_day_enabled 是否同时是位置和 keyword
        import re
        calls = re.findall(r'_simulate_core_v3\((.*?)\)', src, re.DOTALL)
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
                            f"_simulate_core_v3 调用 #{i+1} 中 slippage 同时是位置参数和 keyword — "
                            f"会触发 'multiple values' 错"
                        )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])