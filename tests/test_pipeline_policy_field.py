"""test_pipeline_policy_field - PipelineResult.policy_priority 字段 + serialize(计划书 §7)。

覆盖 plan-audit F-2 三件套("policy_priority" in / [] / .get())+ 默认 None +
serialize 有才加 key(无则响应形状不变)+ 现有 5 字段回归不破坏。
"""
import pandas as pd

from pipeline.result_writer import PipelineResult, ResultWriter


def _make_result(policy_priority=None):
    """构造最小 PipelineResult(backtest=None, 不依赖 engine)。"""
    return PipelineResult(
        selections=pd.DataFrame(),
        backtest=None,
        benchmark={},
        reports={},
        policy_priority=policy_priority,
    )


def test_in_contains_policy_priority():
    """F-2: 'policy_priority' in result == True(_FIELDS 元组同步)。"""
    r = _make_result()
    assert "policy_priority" in r


def test_getitem_policy_priority_no_keyerror():
    """F-2: result['policy_priority'] 不抛 KeyError(幽灵 bug 守卫)。"""
    r = _make_result(policy_priority={"600519.SH": "P1"})
    assert r["policy_priority"] == {"600519.SH": "P1"}


def test_get_policy_priority():
    """F-2: result.get('policy_priority') 返回正确;默认 None。"""
    r = _make_result(policy_priority={"600519.SH": "P1"})
    assert r.get("policy_priority") == {"600519.SH": "P1"}
    assert _make_result().get("policy_priority") is None


def test_default_none():
    """默认 None(建议字段,不执行)。"""
    assert _make_result().policy_priority is None


def test_serialize_omits_when_none():
    """serialize: policy_priority=None → resp 不含该 key(响应形状不变,backward compat)。"""
    resp = ResultWriter().serialize(_make_result(policy_priority=None))
    assert "policy_priority" not in resp


def test_serialize_includes_when_present():
    """serialize: 有值 → resp['policy_priority'] 含。"""
    pp = {"600519.SH": "P1", "000001.SZ": "AVOID"}
    resp = ResultWriter().serialize(_make_result(policy_priority=pp))
    assert resp["policy_priority"] == pp


def test_old_five_fields_still_work():
    """回归: 加字段不破坏现有字段的 in / [] 访问。

    A 层(2026-07-26)加 policy_priority → 6 字段; P1.5(2026-07-28)加 policy_enriched → 7 字段。
    老字段 (selections/backtest/benchmark/reports/error) 访问不变。
    """
    r = _make_result()
    for k in ("selections", "backtest", "benchmark", "reports", "error"):
        assert k in r
        _ = r[k]  # 不抛 KeyError
    # _FIELDS 含 7 个 (A 层 policy_priority + P1.5 policy_enriched)
    assert len(PipelineResult._FIELDS) == 7
    assert "policy_priority" in PipelineResult._FIELDS
    assert "policy_enriched" in PipelineResult._FIELDS  # P1.5 新增 (H-3 _FIELDS 同步)
