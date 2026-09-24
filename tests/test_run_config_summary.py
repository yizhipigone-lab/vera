"""回测口径摘要 + 止损摘要优先级行 (2026-08-20).

用户要求: 历史回测卡片除止损参数外, 必须写清边界条件
(初始资金 / K线周期 / 买入价口径 / 选股公式 / 股票池 / 复权口径 / 优先级)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.stop_config import get_stop_config_summary
from utils.config_loader import get_run_config_summary


def _sample_config():
    """构造一个形如 _config_to_yaml_dict 输出的完整配置。"""
    return {
        "backtest": {
            "initial_capital": 1000000.0,
            "period": "1d",
            "entry_price_mode": "close_t",
        },
        "selection": {
            "formula_name": "QUANTQQ",
            "formula_arg": "3",
            "period": "1d",
            "dividend_type": 1,
            "universe": {"type": "50", "exclude_st": True},
        },
        "time_range": {"start": "20240101", "end": "20250630"},
    }


def test_run_config_summary_has_all_boundary_fields():
    summary = get_run_config_summary(_sample_config())
    # 每个边界条件都必须在摘要里出现
    assert "初始资金" in summary
    assert "1,000,000 元" in summary
    assert "K线周期" in summary
    assert "日线" in summary
    assert "买入价口径" in summary
    assert "信号日收盘价" in summary
    assert "选股公式: QUANTQQ" in summary
    assert "股票池" in summary
    assert "沪深A股" in summary
    assert "复权口径" in summary
    assert "前复权" in summary
    assert "20240101 ~ 20250630" in summary


def test_run_config_summary_period_mismatch_noted():
    cfg = _sample_config()
    cfg["backtest"]["period"] = "5m"  # 选股 1d / 回测 5m 组合
    summary = get_run_config_summary(cfg)
    assert "5分线" in summary
    assert "选股用日线" in summary


def test_run_config_summary_open_t1_entry():
    cfg = _sample_config()
    cfg["backtest"]["entry_price_mode"] = "open_t1"
    summary = get_run_config_summary(cfg)
    assert "次日开盘价" in summary


def test_run_config_summary_unknown_universe_no_crash():
    cfg = _sample_config()
    cfg["selection"]["universe"]["type"] = "999"
    summary = get_run_config_summary(cfg)
    assert "999" in summary  # 未知类型回退显示原始代码


def test_stop_config_summary_includes_priority():
    summary = get_stop_config_summary({
        "priority": "trailing_first",
        "cost_stop": {"enabled": False},
        "ladder_tp": {"enabled": False},
        "trailing_stop": {"enabled": False},
        "time_stop": {"enabled": False},
    })
    assert "优先级" in summary
    assert "移动止盈优先" in summary
