"""benchmark 模块单元测试 (P2-7, 2026-07-15).

覆盖:
- PERIODS_PER_YEAR 各周期映射正确 (尤其 1w=52 不被 *252 高估 4.8 倍)
- BenchmarkComparator 构造时默认值正确
- _align 在共同交易日 < 2 时返回空 DataFrame (防御性返回)
- P2-6: benchmark 模块不再触发 BacktestEngine 完整模块加载链
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest._constants import PERIODS_PER_YEAR, BARS_PER_DAY
from backtest.benchmark import BenchmarkComparator


# ---------- P1-4 / F-H6 关键回归保护 ----------

def test_periods_per_year_weekly_not_overestimated():
    """P1-4 (2026-07-13 修复): 1w 必须用 52 周/年, 不能错套 252."""
    assert PERIODS_PER_YEAR["1w"] == 52
    assert PERIODS_PER_YEAR["1d"] == 252
    assert PERIODS_PER_YEAR["5m"] == 48 * 252


def test_periods_per_year_no_hardcoded_252_for_week():
    """防回归: 1w 不能等于 252 (会被 4.8 倍高估)."""
    assert PERIODS_PER_YEAR["1w"] != 252, (
        "1w 用 252 会把 5 年周线当成 1 年, 必须用 52"
    )


def test_bars_per_day_values():
    """BARS_PER_DAY 必须与 PERIODS_PER_YEAR 配套 (5m×252 = PERIODS_PER_YEAR['5m'])."""
    assert BARS_PER_DAY["1d"] == 1
    assert BARS_PER_DAY["5m"] == 48
    assert BARS_PER_DAY["1w"] == 1
    # 自洽性: 5m 的年化 = bars_per_day × 252
    assert PERIODS_PER_YEAR["5m"] == BARS_PER_DAY["5m"] * 252


# ---------- BenchmarkComparator 构造 ----------

def test_benchmark_default_indices():
    """无 config 时默认对比沪深指数."""
    cmp = BenchmarkComparator()
    assert cmp.index_names == ["shanghai"]
    assert cmp.normalize_start is True
    assert cmp.period == "1d"


def test_benchmark_custom_indices():
    """config 传入多指数."""
    cfg = {"indices": ["hs300", "csi500"], "period": "1w"}
    cmp = BenchmarkComparator(cfg)
    assert cmp.index_names == ["hs300", "csi500"]
    assert cmp.period == "1w"


def test_benchmark_uses_correct_periods_per_year():
    """不同 period 必须用对应 PERIODS_PER_YEAR, 防 1w 仍用 252."""
    # 2026-07-18 审计 M2 修复: 旧版断言 `bm.PERIODS_PER_YEAR is PERIODS_PER_YEAR`
    # 是恒真 tautology (同一模块对象两次 import)。改为行为断言: 查表值正确。
    import backtest.benchmark as bm
    assert bm.PERIODS_PER_YEAR["1d"] == 252
    assert bm.PERIODS_PER_YEAR["1w"] == 52
    # 5m 年化 = 48 bar/日 × 252 日
    assert bm.PERIODS_PER_YEAR["5m"] == 48 * 252


# ---------- _align 防御性返回 ----------

def test_align_returns_empty_when_common_dates_lt_2():
    """_align 在共同交易日 < 2 时必须返回空 DataFrame (不抛, 不静默返回错误数据)."""
    cmp = BenchmarkComparator()

    eq = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=3), "equity": [1.0, 1.05, 1.10]})
    idx = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=3), "close": [3000, 3100, 3200]})

    result = cmp._align(eq, idx, "shanghai", periods_per_year=252)
    assert isinstance(result, pd.DataFrame)
    assert result.empty


def test_align_returns_valid_comparison_with_enough_overlap():
    """_align 在共同日期足够时返回含 strategy_equity / index_close / excess_return 的 DataFrame."""
    cmp = BenchmarkComparator()
    dates = pd.date_range("2024-01-01", periods=10)

    eq = pd.DataFrame({"date": dates, "equity": [1.0 + i * 0.01 for i in range(10)]})
    idx = pd.DataFrame({"date": dates, "close": [3000 + i * 10 for i in range(10)]})

    result = cmp._align(eq, idx, "shanghai", periods_per_year=252)
    assert not result.empty
    assert "strategy_equity" in result.columns
    assert "index_close" in result.columns
    assert "strategy_return" in result.columns
    assert "index_return" in result.columns
    assert "excess_return" in result.columns
    assert len(result) == 10


# ---------- P2-6 验证: benchmark 不再 import engine ----------

def test_benchmark_does_not_import_engine(monkeypatch):
    """P2-6: benchmark 模块应不依赖 BacktestEngine (避免拖入 connector / data_fetcher 等).

    验证方式: 监控 backtest.engine 的 import 信号, benchmark 加载时不应触发.
    """
    # 通过 sys.modules 监控
    import sys
    triggered = []

    real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    def spy_import(name, *args, **kwargs):
        if name == "backtest.engine" or name.startswith("backtest.engine."):
            triggered.append(name)
        return real_import(name, *args, **kwargs)

    import builtins
    monkeypatch.setattr(builtins, '__import__', spy_import)

    # 先确保 benchmark 已加载 (test session 中其他测试可能已触发)
    import backtest.benchmark  # noqa: F401
    triggered.clear()

    # 重新触发 import (通过 importlib.reload)
    import importlib
    importlib.reload(sys.modules["backtest.benchmark"])

    # reload 后的 import 链路里, 不应有 backtest.engine
    engine_triggers = [t for t in triggered if t == "backtest.engine"]
    assert not engine_triggers, (
        f"benchmark 不应依赖 BacktestEngine, 但 import 时触发了: {engine_triggers}"
    )


# ---------- H-2: MappingProxyType 写保护 ----------

def test_periods_per_year_is_readonly():
    """PERIODS_PER_YEAR 为 MappingProxyType, 写操作→TypeError."""
    with pytest.raises(TypeError):
        PERIODS_PER_YEAR["1w"] = 999


def test_bars_per_day_is_readonly():
    """BARS_PER_DAY 为 MappingProxyType, 写操作→TypeError."""
    with pytest.raises(TypeError):
        BARS_PER_DAY["1d"] = 999


# ---------- 覆盖率靶向: BenchmarkComparator.fetch_and_compare ----------

def test_benchmark_fetch_and_compare_normal(monkeypatch):
    """fetch_and_compare() 正常流程: 有 equity_curve + mock index."""
    import pandas as pd
    cmp = BenchmarkComparator()

    equity = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=5),
        "equity": [1.0, 1.01, 1.02, 1.03, 1.04],
    })
    idx_data = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=5),
        "close": [3000, 3010, 3020, 3030, 3040],
    })

    def fake_fetch_index(self, index_name, start_time="", end_time=""):
        return idx_data
    monkeypatch.setattr(cmp, "_fetch_index", fake_fetch_index)

    result = cmp.fetch_and_compare(equity)
    assert isinstance(result, dict)
    assert "shanghai" in result


def test_benchmark_fetch_and_compare_empty_equity(monkeypatch):
    """equity_curve 为空 → _align 返回空, 最终结果相应."""
    import pandas as pd
    cmp = BenchmarkComparator()

    empty_idx = pd.DataFrame()
    monkeypatch.setattr(cmp, "_fetch_index",
                        lambda self, name, start="", end="": empty_idx)

    result = cmp.fetch_and_compare(pd.DataFrame(columns=["date", "equity"]))
    # index 为空时被 skip, 结果为 {}
    assert result == {}


def test_periods_per_year_still_readable():
    """MappingProxyType 读操作与 dict 完全一致."""
    assert PERIODS_PER_YEAR["1w"] == 52
    assert PERIODS_PER_YEAR.get("1d") == 252
    assert len(PERIODS_PER_YEAR) == 3


# ---------- 2026-07-21: step3_benchmark 基准拉取区间回归 ----------

def _make_pipeline_stub(config):
    """不跑 Pipeline.__init__ (避免读 yaml/建目录), 只注入 config."""
    from pipeline.pipeline import Pipeline
    pipe = Pipeline.__new__(Pipeline)
    pipe.config = config
    return pipe


def test_step3_benchmark_uses_equity_actual_range(monkeypatch):
    """5m 稀疏窗口下 equity 超出请求区间: 基准拉取必须用 equity 实际首尾,
    否则 equity 尾部基准整段缺失 (请求 end=20250101 时基准只到 2024-12-31)."""
    pipe = _make_pipeline_stub({
        "time_range": {"start": "20240101", "end": "20250101"},
        "backtest": {"period": "5m"},
    })
    # equity 实际区间: 2024-06-27 ~ 2025-04-25 (起点被 5m 深度截断, 终点含窗口缓冲)
    equity = pd.DataFrame({
        "date": pd.to_datetime(["2024-06-27 09:35", "2024-12-31 15:00",
                                "2025-04-25 15:00"]),
        "equity": [1.0, 1.2, 1.27],
    })

    captured = {}
    _patch_comparator(monkeypatch, captured)

    pipe.step3_benchmark({"equity_curve": equity})
    assert captured["start_time"] == "20240627"
    assert captured["end_time"] == "20250425"
    # 回测 period 注入基准 config, 保证 5m 对齐
    assert captured["config"]["period"] == "5m"


def test_step3_benchmark_empty_equity_no_fetch(monkeypatch):
    """equity 为空 → 直接返回 {}, 不触发基准拉取."""
    pipe = _make_pipeline_stub({"backtest": {"period": "5m"}})

    captured = {}
    _patch_comparator(monkeypatch, captured)

    assert pipe.step3_benchmark({"equity_curve": pd.DataFrame()}) == {}
    assert "start_time" not in captured


def _patch_comparator(monkeypatch, captured):
    """把 pipeline 模块内可见的 BenchmarkComparator 换成探针假类。

    必须 patch pipeline.pipeline 的模块属性, 不能 patch backtest.benchmark
    里的类 — 本文件 test_benchmark_does_not_import_engine 会 importlib.reload
    backtest.benchmark, reload 后 pipeline 绑定的是新类对象, patch 旧类不生效。
    """
    import pipeline.pipeline as pl

    class FakeComparator:
        def __init__(self, config=None):
            captured["config"] = config

        def fetch_and_compare(self, equity_curve, start_time="", end_time=""):
            captured["start_time"] = start_time
            captured["end_time"] = end_time
            return {}

    monkeypatch.setattr(pl, "BenchmarkComparator", FakeComparator)

# ---------- 2026-07-21: 基准日粒度回退 (用户决策: 基准也降级为日线) ----------

def _bars_5m(day):
    return [pd.Timestamp(f"{day} 09:35"), pd.Timestamp(f"{day} 15:00")]


def test_benchmark_daily_fallback_when_index5m_starts_late(monkeypatch):
    """分钟级回测, 权益起点早于指数 5m 可得起点 (TDX 深度限制) →
    基准对比整体回退日粒度, 覆盖完整请求区间; granularity 标记 1d。"""
    cmp = BenchmarkComparator({"period": "5m"})
    # 权益: 5m 网格 2024-01-02 ~ 2024-06-28 (起点早于指数 5m 深度)
    eq_days = ["2024-01-02", "2024-01-03", "2024-06-27", "2024-06-28"]
    eq_dates = [t for d in eq_days for t in _bars_5m(d)]
    equity = pd.DataFrame({
        "date": pd.to_datetime(eq_dates),
        "equity": [1.0, 1.01, 1.02, 1.03, 1.04, 1.05, 1.06, 1.07],
    })
    # 指数 5m: 仅从 2024-06-27 起 (模拟 TDX 5m 深度上限)
    idx5m_dates = [t for d in ["2024-06-27", "2024-06-28"] for t in _bars_5m(d)]
    idx_5m = pd.DataFrame({
        "date": pd.to_datetime(idx5m_dates), "close": [3000, 3010, 3020, 3030]})
    # 指数 1d: 全区间
    idx_1d = pd.DataFrame({
        "date": pd.to_datetime(eq_days), "close": [2900, 2910, 3000, 3030]})

    monkeypatch.setattr(cmp, "_fetch_index", lambda self, name, s="", e="": idx_5m)

    from core.data_fetcher import DataFetcher
    calls = {}

    def fake_index_data(index_name, start_time="", end_time="",
                        dividend_type="none", period="1d"):
        calls["period"] = period
        return idx_1d
    monkeypatch.setattr(DataFetcher, "get_index_data",
                        classmethod(lambda cls, *a, **kw: fake_index_data(*a, **kw)))

    result = cmp.fetch_and_compare(equity, "20240101", "20250101")
    assert calls.get("period") == "1d", "应回退拉 1d 指数数据"
    comp = result["shanghai"]
    assert not comp.empty
    # 覆盖完整请求区间 (日粒度), 不再从 2024-06-27 才开始
    assert comp.index.min() == pd.Timestamp("2024-01-02")
    assert comp.index.max() == pd.Timestamp("2024-06-28")
    assert len(comp) == len(eq_days)
    assert comp.attrs["stats"]["granularity"] == "1d"


def test_benchmark_keeps_5m_when_index_covers_equity_start(monkeypatch):
    """权益起点 ≥ 指数 5m 起点 → 维持 5m 路径, 不拉 1d。"""
    cmp = BenchmarkComparator({"period": "5m"})
    eq_dates = [t for d in ["2024-06-27", "2024-06-28"] for t in _bars_5m(d)]
    equity = pd.DataFrame({
        "date": pd.to_datetime(eq_dates), "equity": [1.0, 1.01, 1.02, 1.03]})
    idx_5m = pd.DataFrame({
        "date": pd.to_datetime(eq_dates), "close": [3000, 3010, 3020, 3030]})

    monkeypatch.setattr(cmp, "_fetch_index", lambda self, name, s="", e="": idx_5m)

    from core.data_fetcher import DataFetcher

    def _boom(*a, **kw):
        raise AssertionError("5m 覆盖权益起点时不应回退拉 1d")
    monkeypatch.setattr(DataFetcher, "get_index_data",
                        classmethod(lambda cls, *a, **kw: _boom()))

    result = cmp.fetch_and_compare(equity, "20240627", "20250101")
    comp = result["shanghai"]
    assert len(comp) == len(eq_dates)  # 5m 粒度
    assert "granularity" not in comp.attrs.get("stats", {})
