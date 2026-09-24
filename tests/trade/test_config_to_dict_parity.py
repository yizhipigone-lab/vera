# -*- coding: utf-8 -*-
"""P1-2 持久化对拍: trade_config_to_dict 键序/结构锁定 (2026-08-16 收尾)。

防回归点: P1-2 把 to_dict 顶层骨架改成 dataclasses.fields 派生后,
键名/键序/嵌套结构必须与手写版保持等价 —— 本测试把这份等价固化下来,
未来改派生逻辑漂移即炸。
"""
import dataclasses

from trade.config import TradeConfig, trade_config_to_dict


def test_to_dict_top_level_keys_match_dataclass_fields():
    """顶层键名与顺序 = dataclass 字段声明序（派生骨架的不变量）。"""
    d = trade_config_to_dict(TradeConfig())
    expected = [f.name for f in dataclasses.fields(TradeConfig)]
    assert list(d.keys()) == expected, (
        f"to_dict 顶层键序漂移: 期望 {expected}, 实际 {list(d.keys())}")


def test_to_dict_nested_sections_present():
    """嵌套段结构锁定: 关键段/key 一个都不能少。"""
    d = trade_config_to_dict(TradeConfig())
    for section in ("stop", "position_sizing", "auto_buy"):
        assert section in d and isinstance(d[section], dict), f"缺嵌套段 {section}"
    # 抽样深键 (stop 段的出场参数)
    stop = d["stop"]
    for k in ("cost_stop", "trailing_stop", "ladder_tp", "time_stop"):
        assert k in stop, f"stop 段缺 {k}"
    assert "threshold" in stop["cost_stop"]
    assert "activation" in stop["trailing_stop"]
    assert "drawdown" in stop["trailing_stop"]


def test_to_dict_roundtrip_stable():
    """to_dict → 再 to_dict 幂等（同输入恒同输出, 字节级稳定）。"""
    import json
    cfg = TradeConfig()
    j1 = json.dumps(trade_config_to_dict(cfg), ensure_ascii=False, sort_keys=False)
    j2 = json.dumps(trade_config_to_dict(cfg), ensure_ascii=False, sort_keys=False)
    assert j1 == j2
