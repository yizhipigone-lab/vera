"""公式农场达标口径 (core/farm_rules) 测试。

2026-09-11 用户拍板: 达标线统一定为 **年化≥15% 且 |最大回撤|≤15% 且 笔数≥20**;
笔数<20 记「样本不足」(不判达标、不参与最优评选)。此前 gs 系三份报告各写
一份 0.30/1000, 与 09-09 批实际在用的 15% 冲突 —— 本模块是唯一真相源。

2026-09-22 用户拍板: 年化下限 15% → **10%** (回撤/笔数两条腿不动)。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import farm_rules  # noqa: E402


def test_thresholds_pinned_to_user_decision():
    assert farm_rules.TARGET_ANN == 0.10        # 2026-09-22 拍板: 15% → 10%
    assert farm_rules.TARGET_MAXDD == 0.15
    assert farm_rules.MIN_TRADES == 20


def test_pass_at_exact_threshold():
    """边界含等号: 年化正好 10%、回撤正好 15%、笔数正好 20 → 达标。"""
    v = farm_rules.verdict(0.10, -0.15, 20)
    assert v["code"] == "pass" and v["label"] == "达标"


def test_fail_just_below_ann():
    v = farm_rules.verdict(0.0999, -0.10, 500)
    assert v["code"] == "fail"
    assert "年化" in v["reason"] and "10%" in v["reason"]
    assert v["reason"].count("10%") >= 1


def test_fail_just_over_maxdd():
    v = farm_rules.verdict(0.40, -0.1501, 500)
    assert v["code"] == "fail"
    assert "回撤" in v["reason"]


def test_insufficient_trades_beats_ann():
    """样本不足优先于"数字好看": 3 笔 100% 胜率不是达标。"""
    v = farm_rules.verdict(0.90, -0.01, 3)
    assert v["code"] == "insufficient"
    assert v["label"] == "样本不足"
    assert "20" in v["reason"]
    assert farm_rules.verdict(0.90, -0.01, 19)["code"] == "insufficient"
    assert farm_rules.verdict(0.90, -0.01, 20)["code"] == "pass"


@pytest.mark.parametrize("ann,dd,trades", [
    (None, -0.01, 100), (0.2, None, 100), (0.2, -0.01, None),
    (float("nan"), -0.01, 100), (0.2, -0.01, 0),
])
def test_invalid_inputs(ann, dd, trades):
    assert farm_rules.verdict(ann, dd, trades)["code"] == "invalid"


def test_labels_are_the_four_known_states():
    assert farm_rules.verdict(0.2, -0.05, 100)["label"] == "达标"
    assert farm_rules.verdict(0.05, -0.05, 100)["label"] == "未达标"
    assert farm_rules.verdict(0.05, -0.05, 5)["label"] == "样本不足"
    assert farm_rules.verdict(None, None, None)["label"] == "无效"


def test_describe_is_human_readable_and_complete():
    s = farm_rules.describe()
    assert "10%" in s and "15%" in s and "20" in s and "回撤" in s and "年化" in s


def test_is_pass_helper_matches_verdict():
    assert farm_rules.is_pass({"annret": 0.2, "maxdd": -0.05, "trades": 100}) is True
    assert farm_rules.is_pass({"annret": 0.2, "maxdd": -0.05, "trades": 5}) is False
    assert farm_rules.is_pass({"annret": 0.02, "maxdd": -0.05, "trades": 100}) is False


def test_pick_best_prefers_eligible_sample():
    """最优组合只在笔数≥20 的行里选; 若全不足则退回最优行并标样本不足。"""
    rows = [{"annret": 0.30, "maxdd": -0.01, "trades": 3},      # 数字最好但没样本
            {"annret": 0.05, "maxdd": -0.02, "trades": 200}]
    best = farm_rules.pick_best(rows)
    assert best["trades"] == 200

    thin = [{"annret": 0.30, "maxdd": -0.01, "trades": 3},
            {"annret": 0.10, "maxdd": -0.01, "trades": 5}]
    best2 = farm_rules.pick_best(thin)
    assert best2["trades"] == 3          # 只能给最优行, 但调用方会判「样本不足」
    assert farm_rules.pick_best([]) is None
