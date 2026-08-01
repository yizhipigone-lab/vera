"""TradeConfig / load_trade_config 单元测试 (2026-07-26 裁决①②后重写).

锁住: 缺字段走默认值 (与回测 stop_loss 同形同值)、嵌套结构
类型/值域 fail-fast、未知字段拒绝、旧扁平字段已删除 (做减法)。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.config import TradeConfig, load_trade_config


def _write(tmp_path, text):
    p = tmp_path / "c.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_defaults_align_backtest_stop_loss(tmp_path):
    """默认值锁死: 与 config/default.yaml stop_loss 同形同值 (裁决①)。"""
    cfg = load_trade_config(_write(tmp_path, ""))
    assert cfg.stop.priority == "trailing_first"
    assert cfg.stop.cost_stop.enabled and cfg.stop.cost_stop.threshold == -0.12
    assert cfg.stop.trailing_stop.activation == 0.035
    assert cfg.stop.trailing_stop.drawdown == 0.01
    assert cfg.stop.ladder_tp.levels == ((0.06, 0.30), (0.15, 0.30))
    assert cfg.stop.time_stop.max_hold_days == 20
    assert not cfg.stop.cond_time_stop.enabled
    assert not cfg.stop.first_day.enabled
    # sizing 默认对齐 default.yaml backtest.position_sizing
    assert cfg.position_sizing.min_buy_amount == 2000.0
    assert cfg.position_sizing.max_buy_amount == 20000.0
    assert cfg.position_sizing.lot_size == 100
    assert cfg.daily_loss_limit == 0.05
    assert cfg.reconcile_times == ("09:35", "11:30", "14:55", "15:05")


def test_removed_flat_fields_rejected(tmp_path):
    """裁决①②做减法: 旧扁平字段一律未知字段拒绝, 不留兼容层。"""
    for old in ("ladder_tiers", "trailing_drawdown", "hard_stop_ratio",
                "time_stop_days", "time_stop_min_gain", "concentration_cap"):
        with pytest.raises(ValueError, match="未知字段"):
            load_trade_config(_write(tmp_path, f"{old}: 0.1\n"))


def test_nested_stop_override(tmp_path):
    """嵌套覆写: 只写关心的规则, 其余走默认; levels 按 profit 升序归一化。"""
    cfg = load_trade_config(_write(tmp_path, """
stop:
  priority: stop_first
  trailing_stop:
    drawdown: 0.02
  ladder_tp:
    levels:
      - {profit: 0.15, sell_ratio: 1.0}
      - {profit: 0.05, sell_ratio: 0.33}
"""))
    assert cfg.stop.priority == "stop_first"
    assert cfg.stop.trailing_stop.drawdown == 0.02
    assert cfg.stop.trailing_stop.activation == 0.035      # 未写走默认
    assert cfg.stop.ladder_tp.levels == ((0.05, 0.33), (0.15, 1.0))


def test_bad_priority_rejected(tmp_path):
    with pytest.raises(ValueError, match="priority"):
        load_trade_config(_write(tmp_path, "stop:\n  priority: yolo\n"))


def test_cost_stop_threshold_sign_enforced(tmp_path):
    """threshold 负值口径 (回测同), 正值 fail-fast。"""
    with pytest.raises(ValueError, match="threshold"):
        load_trade_config(_write(
            tmp_path, "stop:\n  cost_stop:\n    threshold: 0.12\n"))


def test_ladder_levels_must_be_backtest_shape(tmp_path):
    """levels 元素必须是 {profit, sell_ratio} 映射 (回测同形)。"""
    with pytest.raises(ValueError, match="levels"):
        load_trade_config(_write(
            tmp_path, "stop:\n  ladder_tp:\n    levels:\n      - [0.05, 0.33]\n"))


def test_unknown_rule_and_param_rejected(tmp_path):
    with pytest.raises(ValueError, match="未知规则"):
        load_trade_config(_write(tmp_path, "stop:\n  magic_stop:\n    x: 1\n"))
    with pytest.raises(ValueError, match="未知参数"):
        load_trade_config(_write(
            tmp_path, "stop:\n  cost_stop:\n    threshld: -0.1\n"))


def test_sizing_validation(tmp_path):
    with pytest.raises(ValueError, match="max_buy_amount"):
        load_trade_config(_write(
            tmp_path, "position_sizing:\n  min_buy_amount: 30000\n"
                      "  max_buy_amount: 20000\n"))
    with pytest.raises(ValueError, match="未知字段"):
        load_trade_config(_write(
            tmp_path, "position_sizing:\n  leverage: 2\n"))


def test_trade_section_compatible(tmp_path):
    cfg = load_trade_config(_write(
        tmp_path, "trade:\n  daily_loss_limit: 0.03\n"))
    assert cfg.daily_loss_limit == 0.03


def test_bool_not_accepted_as_number(tmp_path):
    with pytest.raises(TypeError):
        load_trade_config(_write(tmp_path, "daily_loss_limit: true\n"))


def test_config_is_frozen():
    with pytest.raises(Exception):
        TradeConfig().account_id = "x"
    with pytest.raises(Exception):
        TradeConfig().stop.priority = "stop_first"
