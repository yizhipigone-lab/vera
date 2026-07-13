"""
候选 A 阶段 1 — stop_config capabilities 开关测试

验证:
  - load_stop_config() 返回 capabilities 三键 (default.yaml 加了)
  - load_stop_config_or_default() 兜底含 capabilities + priority (修了原兜底漏 priority)
  - yaml/stop_config 缺 capabilities 时 .get 回退全 True (安全, 40 调用方零感知)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.stop_config import load_stop_config, load_stop_config_or_default


def test_load_stop_config_has_capabilities():
    """default.yaml 加了 capabilities → load_stop_config 返回三键默认 True."""
    cfg = load_stop_config()
    caps = cfg.get("capabilities", {})
    assert caps.get("formula_exit") is True, f"formula_exit 应 True, 实际 {caps.get('formula_exit')}"
    assert caps.get("gap_protection") is True, f"gap_protection 应 True, 实际 {caps.get('gap_protection')}"
    assert caps.get("delisting") is True, f"delisting 应 True, 实际 {caps.get('delisting')}"


def test_load_stop_config_or_default_has_capabilities_and_priority():
    """兜底 dict 加了 capabilities + priority (原兜底漏 priority, 已修)."""
    cfg = load_stop_config_or_default()
    assert cfg.get("priority") == "trailing_first", (
        f"兜底 priority 应 trailing_first (与 default.yaml 对齐), 实际 {cfg.get('priority')}"
    )
    caps = cfg.get("capabilities", {})
    assert caps.get("formula_exit") is True
    assert caps.get("gap_protection") is True
    assert caps.get("delisting") is True


def test_missing_capabilities_defaults_all_true():
    """老格式 stop_config (无 capabilities) → run_cached 用 .get(caps, {}) 回退全 True.

    这是 40 调用方零感知的关键: 即使 stop_config 没 capabilities 键,
    run_cached 的 caps.get('formula_exit', True) 等也回退 True → 全开 → 数据 None → 能力 off = 旧行为。
    """
    sc = {
        "priority": "stop_first",
        "cost_stop": {"enabled": True, "threshold": -0.12},
        "trailing_stop": {"enabled": True, "activation": 0.08, "drawdown": 0.05},
        "ladder_tp": {"enabled": True, "levels": []},
        "time_stop": {"enabled": True, "max_hold_days": 20},
        # 故意没有 capabilities 键 (老格式)
    }
    caps = sc.get("capabilities", {})
    assert caps.get("formula_exit", True) is True
    assert caps.get("gap_protection", True) is True
    assert caps.get("delisting", True) is True


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])
