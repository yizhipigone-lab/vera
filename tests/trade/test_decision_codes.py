"""决策台账原因码契约测试 (2026-09-18)。

锁住 `trade/decision_codes.py` 这份「唯一真相源」的三件事:

1. `CODES` 表本身自洽 —— 每个码都有白话标签/合法色调/合法动作, 且动作都能在
   `ACTION_RANK` 里找到排名 (否则「动作强弱合并」会静默把某条压成 0);
2. 未知码**不报错**, 返回灰字兜底 —— 页面上少一点描述可以, 整张卡打不开不行;
3. `classify_rotation` / `classify_auto_buy` 的**判定优先级** —— 这两个纯函数
   实时写入与历史回填共用, 顺序错了会导致「同一天实时说换了腿、回填说没动」。

为什么这些用例值得写: 这两个函数的判定顺序必须与 `trade/rotation.py` 里 `_execute`
的分支顺序保持一致。那边改了分支顺序而忘了回看这里, 回填出来的历史台账就会与
实时台账口径不一致 —— 而且不会报错, 只会静默不一致。用例是给未来的自己设的路障。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.decision_codes import (  # noqa: E402
    ACTION_BUY, ACTION_FAIL, ACTION_HOLD, ACTION_INFO, ACTION_RANK, ACTION_SELL,
    CODES, TONE_DOWN, TONE_INFO, TONE_MUTED, TONE_UP, TONE_WARN,
    action_of, classify_auto_buy, classify_rotation, classify_rotation_skip,
    get, label_of, tone_of,
)

_VALID_TONES = {TONE_UP, TONE_DOWN, TONE_MUTED, TONE_WARN, TONE_INFO}
_VALID_ACTIONS = {ACTION_BUY, ACTION_SELL, ACTION_HOLD, ACTION_FAIL, ACTION_INFO}


# ── CODES 表自洽 ────────────────────────────────────────────────

def test_codes_count_is_24():
    """原因码总数锁定。加码时这条会红 —— 那是提醒: 前端的 EVIDENCE_LABELS、
    计划书 §3.6 的映射表也要同步。"""
    assert len(CODES) == 24


def test_every_code_has_complete_fields():
    """每个码都必须有白话标签/色调/动作三件套, 且都不为空。"""
    for code, meta in CODES.items():
        assert set(meta) == {"label", "tone", "action"}, code
        assert meta["label"].strip(), code
        assert meta["tone"] in _VALID_TONES, code
        assert meta["action"] in _VALID_ACTIONS, code


def test_every_code_action_is_ranked():
    """动作必须都在 ACTION_RANK 里 —— 漏一个, 合并规则会把它当权重 0 处理,
    表现为「这条决策莫名其妙被更弱的动作盖掉」, 且不报错。"""
    for code, meta in CODES.items():
        assert meta["action"] in ACTION_RANK, code


def test_codes_prefixed_by_strategy():
    """命名规则: 前缀 = 哪条策略。前端按前缀分组的假设依赖这条。"""
    allowed = ("ROT_", "PICK_", "EXIT_", "LADDER_", "NO_RUN")
    for code in CODES:
        assert code.startswith(allowed), code


def test_action_rank_order_is_strict():
    """动作强弱必须严格递减: 有成交的压得住没成交的。"""
    assert (ACTION_RANK[ACTION_SELL] > ACTION_RANK[ACTION_BUY]
            > ACTION_RANK[ACTION_FAIL] > ACTION_RANK[ACTION_INFO]
            > ACTION_RANK[ACTION_HOLD])


# ── 未知码兜底 ──────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["", None, "NOT_A_CODE", "rot_switch"])
def test_unknown_code_returns_fallback_without_raising(bad):
    """未知码 (含大小写写错) 不能抛异常, 必须退回灰字。"""
    meta = get(bad)
    assert meta["tone"] == TONE_MUTED
    assert meta["action"] == ACTION_HOLD
    assert meta["label"]  # 至少有个能显示的东西


def test_unknown_code_fallback_label_is_the_code_itself():
    """兜底标签用码本身 —— 页面上能看到一个不认识的码, 便于排查, 不用翻日志。"""
    assert label_of("BRAND_NEW_CODE") == "BRAND_NEW_CODE"


def test_accessors_agree_with_codes_table():
    """label_of / tone_of / action_of 三个取数口必须与 CODES 表逐条一致。"""
    for code, meta in CODES.items():
        assert label_of(code) == meta["label"]
        assert tone_of(code) == meta["tone"]
        assert action_of(code) == meta["action"]


# ── 买红卖绿: 中国股市习惯, 不能反 ──────────────────────────────

def test_buy_is_red_sell_is_green():
    """色调铁律 (AGENTS.md / 项目约定): 买=红(--up), 卖=绿(--down)。"""
    assert tone_of("ROT_SWITCH") == TONE_UP
    assert tone_of("PICK_BUY") == TONE_UP
    assert tone_of("ROT_TRAILING_STOP") == TONE_DOWN
    assert tone_of("EXIT_TRIGGERED") == TONE_DOWN
    # 没动作一律灰, 不能借灰色偷渡成红/绿
    assert tone_of("ROT_HOLD_KEEP") == TONE_MUTED
    assert tone_of("EXIT_NO_TRIGGER") == TONE_MUTED


def test_no_run_is_hold_not_fail():
    """NO_RUN 是「没运行」不是「运行失败」—— 必须是 HOLD 灰字。
    写成 FAIL 会让页面上整屏黄色, 用户以为系统天天出故障。"""
    assert action_of("NO_RUN") == ACTION_HOLD
    assert tone_of("NO_RUN") == TONE_MUTED


# ── classify_rotation ──────────────────────────────────────────

def test_rotation_skip_kinds_win_over_everything():
    """跳过类原因优先级最高: 连 detail 里写了 stop_off 也不算 —— 那一轮根本没跑。"""
    detail = {"stop_off": True, "target": "513100.SH", "momentum": {"a": -0.1}}
    assert classify_rotation(detail, skip_kind="busy") == "ROT_BUSY"
    assert classify_rotation(detail, skip_kind="first_sig") == "ROT_HOLD_FIRST_SIG"
    assert classify_rotation(detail, skip_kind="wait_signal") == "ROT_NOT_SIGNAL_DAY"


def test_rotation_busy_beats_first_sig():
    """同时报两个跳过原因时, busy 优先 —— 与 rotation.py 的调用顺序一致。"""
    assert classify_rotation({}, skip_kind="busy") == "ROT_BUSY"


def test_rotation_trailing_stop_wins_over_switch():
    """日频移动止损触发时, 即使当天算出了新目标腿, 也记「回撤超线卖风险腿」。
    这条最容易错: 止损是主动减风险, 不能被「换腿」这种中性描述盖过去。"""
    detail = {"stop_off": True, "target": "513100.SH", "momentum": {"a": 0.05}}
    assert classify_rotation(detail) == "ROT_TRAILING_STOP"


def test_rotation_insufficient_data_beats_target():
    """行情不够时维持现状 (fail-safe), 不能因为 detail 里有旧 target 就记成换腿。"""
    detail = {"target": "513100.SH", "signal": {"insufficient": True},
              "momentum": {"a": 0.05}}
    assert classify_rotation(detail) == "ROT_DATA_MISSING"


def test_rotation_both_legs_negative_goes_hedge():
    """目标为空 + 两腿动量都 ≤0 = 两条腿都跌, 主动切黄金避险 (买入)。"""
    detail = {"target": None, "momentum": {"159949.SZ": -0.03, "513100.SH": -0.01}}
    assert classify_rotation(detail) == "ROT_HEDGE_BOTH_NEG"
    assert action_of("ROT_HEDGE_BOTH_NEG") == ACTION_BUY


def test_rotation_one_leg_positive_keeps_holding():
    """只要有一条腿 ≥0, 就不是「两条腿都跌」, 记维持持仓。"""
    detail = {"target": None, "momentum": {"159949.SZ": 0.02, "513100.SH": -0.01}}
    assert classify_rotation(detail) == "ROT_HOLD_KEEP"


def test_rotation_zero_momentum_is_both_negative():
    """边界: 动量恰好 0 算「不涨」→ 归入两条腿都跌 (≤0 而非 <0)。
    这条边界改错会让「横盘不动」被记成「维持持仓」, 而实际规则是切避险。"""
    detail = {"target": None, "momentum": {"159949.SZ": 0.0, "513100.SH": 0.0}}
    assert classify_rotation(detail) == "ROT_HEDGE_BOTH_NEG"


def test_rotation_target_with_momentum_is_switch():
    """有目标腿 + 有动量 = 今天真的重算过 → 换腿买入。"""
    detail = {"target": "513100.SH", "momentum": {"159949.SZ": 0.03, "513100.SH": 0.01}}
    assert classify_rotation(detail) == "ROT_SWITCH"
    assert action_of("ROT_SWITCH") == ACTION_BUY


def test_rotation_target_without_momentum_is_keep():
    """有目标腿但没动量 = 在执行既有目标, 不是今天新算的 → 维持持仓。
    与上一条配对: 全靠 momentum 有没有值来区分「重算」与「执行」。"""
    detail = {"target": "513100.SH"}
    assert classify_rotation(detail) == "ROT_HOLD_KEEP"


@pytest.mark.parametrize("empty_target", [None, "", "skip"])
def test_rotation_empty_target_variants(empty_target):
    """目标的「空」有三种写法 (None / 空串 / 'skip'), 都要走避险篮子分支。"""
    detail = {"target": empty_target, "momentum": {"159949.SZ": 0.02}}
    assert classify_rotation(detail) == "ROT_HOLD_KEEP"


def test_rotation_momentum_values_broken_are_treated_as_missing():
    """动量值坏了 (字符串/None) 一律当「没有」, 不猜 —— 猜出来的原因码是假证据。"""
    detail = {"target": None, "momentum": {"a": "坏数据", "b": None}}
    assert classify_rotation(detail) == "ROT_HOLD_KEEP"


def test_rotation_empty_detail_does_not_raise():
    """空 detail (审计里的老数据可能就长这样) 不能炸, 退回维持持仓。"""
    assert classify_rotation({}) == "ROT_HOLD_KEEP"


# ── 老格式兼容 (2026-08-17 ~ 08-21 的历史明细) ──────────────────

def test_old_single_basket_format_uses_changed_flag():
    """单篮子时代 (08-17~08-20): 明细里没有 target, 只有 state/changed。
    `changed=True` 表示篮子变了 → 换腿; False → 维持。

    这条是回填准确性的关键: 不认这个格式的话, 那 4 个交易日会被全记成
    "维持持仓", 而原文白纸黑字写着「换档 True」—— 直接自相矛盾。
    """
    assert classify_rotation({"state": "full_cyb", "changed": True}) == "ROT_SWITCH"
    assert classify_rotation({"state": "full_cyb", "changed": False}) == "ROT_HOLD_KEEP"


def test_old_single_basket_first_evaluation_is_a_switch():
    """首次判定 (derived=None) 也算换档 —— 那一刻确实建立了仓位。"""
    assert classify_rotation(
        {"state": "full_cyb", "changed": True, "derived": None}) == "ROT_SWITCH"


def test_old_single_basket_gold_state_is_still_a_switch():
    """切到黄金篮子同样是"换腿"(避险方向), 不是"维持持仓"。"""
    assert classify_rotation(
        {"state": "full_gold", "changed": True, "derived": "full_cyb"}) == "ROT_SWITCH"


def test_stop_off_wins_over_old_changed_flag():
    """老格式里若出现 stop_off, 它优先于 changed —— 止损是更强的信号。"""
    assert classify_rotation(
        {"state": "full_cyb", "changed": True, "stop_off": True}) == "ROT_TRAILING_STOP"


def test_momentum_nested_in_signal_is_recognized():
    """2026-08-21 那种格式: 有 target, 但动量嵌在 `signal.momentum` 里而不是顶层。
    不回落取它的话, 那天明明算出了新目标腿, 却会被记成"维持持仓"。"""
    detail = {"target": "513100.SH",
              "signal": {"target": "513100.SH",
                         "momentum": {"159949.SZ": 0.0048, "513100.SH": 0.0433}}}
    assert classify_rotation(detail) == "ROT_SWITCH"


def test_top_level_momentum_takes_precedence():
    """顶层 momentum 优先 —— 实时路径写的就是这种, 回落逻辑不该干扰它:
    顶层 momentum=None (执行既有目标) 时即使 signal 里有动量也不算换腿。"""
    detail = {"target": "513100.SH", "momentum": None,
              "signal": {"momentum": {"a": 0.05}}}
    assert classify_rotation(detail) == "ROT_HOLD_KEEP"


def test_signal_insufficient_still_wins_over_nested_momentum():
    """数据不足优先于嵌套动量 —— fail-safe 不能因为 signal 里有别的字段就失效。"""
    detail = {"target": "513100.SH",
              "signal": {"insufficient": True, "momentum": {"a": 0.05}}}
    assert classify_rotation(detail) == "ROT_DATA_MISSING"


# ── classify_rotation_skip: 从原文字认跳过类型 ─────────────────

@pytest.mark.parametrize("message,want", [
    ("非连续竞价时段, 跳过调仓", "ROT_SKIP_SESSION"),
    ("跳过调仓", "ROT_SKIP_SESSION"),
    ("首个周频信号未算, 等信号日", "ROT_HOLD_FIRST_SIG"),
    ("首个信号未算", "ROT_HOLD_FIRST_SIG"),
    ("轮动信号数据不足: 需要 250 根, 实际 0 根", "ROT_DATA_MISSING"),
    ("上一轮还在跑, 本次忽略", "ROT_BUSY"),
    ("等信号日", "ROT_NOT_SIGNAL_DAY"),
])
def test_rotation_skip_returns_reason_codes(message, want):
    """``classify_rotation_skip`` 必须返回**可以用作原因码**的字符串。

    这条盯的是一个真踩过的坑: 它最初返回的是内部中间量 ``"skip_session"``,
    调用方顺手当原因码写进了台账 —— 页面上就会冒出一个灰字兜底的怪码,
    而且不报错、不崩, 只有人眼能发现。
    """
    got = classify_rotation_skip(message)
    assert got == want
    assert got in CODES, f"{got} 不是一个合法原因码"


def test_rotation_skip_unknown_wording_is_conservative():
    """认不出来的文案 → 最中性的那个答案 (「今天不轮到这份重算」)。

    宁可含糊, 不能编一个"上一轮还在跑"出来 —— 那会让用户去查一个不存在的并发问题。
    """
    assert classify_rotation_skip("一句没见过的话") == "ROT_NOT_SIGNAL_DAY"
    assert classify_rotation_skip("") == "ROT_NOT_SIGNAL_DAY"
    assert classify_rotation_skip(None) == "ROT_NOT_SIGNAL_DAY"


def test_rotation_skip_more_specific_rule_wins():
    """文案里同时出现两个关键词时, 更具体的排在前面 (顺序敏感)。"""
    # "非连续竞价" 与 "等信号日" 同时出现 → 前者更具体
    assert classify_rotation_skip(
        "非连续竞价时段, 等信号日再算") == "ROT_SKIP_SESSION"


def test_rotation_skip_agrees_with_structured_skip_kind():
    """两条路 (读句子 / 拿结构化 skip_kind) 对同一个原因必须给同一个码 ——
    它们是同一件事的两个入口, 分叉了就会"实时一套、回填一套"。"""
    cases = [
        ("非连续竞价时段, 跳过调仓", "skip_session"),
        ("首个周频信号未算, 等信号日", "first_sig"),
        ("上一轮还在跑", "busy"),
        ("等信号日", "wait_signal"),
        ("数据不足: 需要 250 根", "data_missing"),
    ]
    for message, kind in cases:
        assert classify_rotation_skip(message) == classify_rotation(
            {}, skip_kind=kind), message


# ── classify_auto_buy ──────────────────────────────────────────

def test_auto_buy_regime_block():
    """弱市闸门拦下 → 大盘没站上年线, 今天不买新股。"""
    assert classify_auto_buy("auto_buy_skip_regime", {}) == "PICK_REGIME_BLOCK"
    assert action_of("PICK_REGIME_BLOCK") == ACTION_FAIL


def test_auto_buy_error():
    """流程报错 → FAIL (想动没动成), 不是 HOLD。"""
    assert classify_auto_buy("auto_buy_error", {"msg": "取数超时"}) == "PICK_ERROR"
    assert action_of("PICK_ERROR") == ACTION_FAIL


def test_auto_buy_summary_bought():
    assert classify_auto_buy("auto_buy_summary", {"selected": 5, "bought": 2}) == "PICK_BUY"


def test_auto_buy_summary_selected_nothing():
    """一只都没选出来 → 公式没信号 (与「选出来了但下不了单」是两件事)。"""
    assert classify_auto_buy(
        "auto_buy_summary", {"selected": 0, "bought": 0}) == "PICK_NO_SIGNAL"


def test_auto_buy_summary_all_filtered():
    """选出来了但一张单没下成 → 全被过滤。"""
    assert classify_auto_buy(
        "auto_buy_summary", {"selected": 3, "bought": 0}) == "PICK_ALL_FILTERED"


def test_auto_buy_unknown_kind_falls_back_to_no_signal():
    """不认识的审计类型 → 保守记「公式没选出票」, 不抛异常、不编瞎话。"""
    assert classify_auto_buy("some_future_kind", {}) == "PICK_NO_SIGNAL"


def test_auto_buy_string_numbers_from_old_audit():
    """审计里的老数据可能是字符串数字, 必须宽松解析 —— 否则「买到了」会被
    误判成「全被过滤」, 历史台账凭空多出一堆假失败。"""
    assert classify_auto_buy(
        "auto_buy_summary", {"selected": "5", "bought": "2"}) == "PICK_BUY"
    assert classify_auto_buy(
        "auto_buy_summary", {"selected": "3", "bought": "0"}) == "PICK_ALL_FILTERED"
    assert classify_auto_buy(
        "auto_buy_summary", {"selected": "0", "bought": "0"}) == "PICK_NO_SIGNAL"


def test_auto_buy_missing_fields_are_zero():
    """字段缺失当 0. selected 缺 → 「没选出票」; selected 有但 bought 缺 → 「全被过滤」。"""
    assert classify_auto_buy("auto_buy_summary", {}) == "PICK_NO_SIGNAL"
    assert classify_auto_buy(
        "auto_buy_summary", {"selected": 2}) == "PICK_ALL_FILTERED"
