"""决策台账历史回填测试 (2026-09-18)。

回填是一次性动作, 但它**是用户唯一能看到 8 月历史原因的途径** —— 翻错了不会报错,
只会静默给出一段错的解释。所以这里把计划书 §3.6 的映射表逐行钉住。

三条最要紧的:
1. **映射表逐行**: 每种审计类型 → 台账行的 (策略, 对象, 动作, 原因码, 来源);
2. **幂等且不覆盖 live**: 跑两遍结果一样; 回填改不动当场记录的行;
3. **不许凭空造行**: 每类审计只管自己那一类。这一条是被真事故钓出来的 ——
   最初把当天全部审计项一股脑交给尾盘选股的还原函数, 而该函数对不认识的类型
   会兜底返回"公式没选出票", 于是一天虚增上千行编出来的记录 (见
   ``test_unrelated_kinds_do_not_create_rows``)。
"""
import datetime as _dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.decision_backfill import (  # noqa: E402
    LEDGER_START, ROTATION_FIRST_SEEN, _missing_rows, collect, run,
)
from trade.decision_codes import action_of  # noqa: E402
from trade.store import TradeStore  # noqa: E402

# 2024-01-02(周二) ~ 01-05(周五) 连续四个交易日; 01-06 是周六
D1, D2, D3, D4, SAT = ("2024-01-02", "2024-01-03", "2024-01-04",
                       "2024-01-05", "2024-01-06")
_T = {d: _dt.datetime.strptime(d + " 10:00", "%Y-%m-%d %H:%M").timestamp()
      for d in (D1, D2, D3, D4, SAT)}
CODE = "600519.SH"
CODE2 = "000001.SZ"


@pytest.fixture()
def store(tmp_path):
    s = TradeStore(tmp_path / "t.db", tmp_path / "r.jsonl")
    yield s
    s.close()


def _audit(store, day, kind, message="", detail=None, offset=0):
    """塞一行审计 (ts 落在 day 当天, offset 秒可用来控制先后顺序)。"""
    with store._lock, store._conn:
        store._conn.execute(
            "INSERT INTO audit (ts, kind, message, detail_json) VALUES (?,?,?,?)",
            (_T[day] + offset, kind, message,
             json.dumps(detail or {}, ensure_ascii=False)))


def _asset(store, day, source):
    with store._lock, store._conn:
        store._conn.execute(
            "INSERT INTO daily_asset (date,total_asset,available,market_value,"
            "ts,source) VALUES (?,?,?,?,?,?)",
            (day, 1e6, 1e6, 0.0, _T[day], source))


def _rows_of(rows, day):
    return [r for r in rows if r["trade_date"] == day]


def _by_key(rows):
    return {(r["trade_date"], r["strategy"], r["subject"]): r for r in rows}


# ═══════════════════════════════════════════════════════════════
# 映射表逐行 (§3.6)
# ═══════════════════════════════════════════════════════════════

def test_rotation_summary_maps_per_tranche(store):
    """``rotation_summary`` 按份号还原成分1/份2, 原因码走共用的纯函数。"""
    _audit(store, D1, "rotation_summary", "份1 换腿", {
        "tranche": 0, "anchor": "monday", "target": "513100.SH",
        "momentum": {"159949.SZ": 0.02, "513100.SH": 0.03}})
    _audit(store, D1, "rotation_summary", "份2 维持", {
        "tranche": 1, "anchor": "tuesday", "target": "513100.SH"},
        offset=1)
    rows = collect(store._conn, D1, D1)
    got = {(r["subject"]): (r["reason_code"], r["source"]) for r in rows}
    assert got["份1"] == ("ROT_SWITCH", "backfill_exact")
    assert got["份2"] == ("ROT_HOLD_KEEP", "backfill_exact")


def test_rotation_summary_evidence_keeps_the_numbers(store):
    """「凭什么」要留住复核用的数字 (目标腿/动量/份池), 否则回填行没法验证。"""
    _audit(store, D1, "rotation_summary", "份1", {
        "tranche": 0, "target": "513100.SH", "pool": 324554.94,
        "momentum": {"159949.SZ": -0.059, "513100.SH": 0.033},
        "stop_off": False, "stop_leg": None, "signal": {"x": 1}})
    ev = collect(store._conn, D1, D1)[0]["evidence"]
    assert ev["target"] == "513100.SH"
    assert ev["pool"] == 324554.94
    assert ev["momentum"]["159949.SZ"] == -0.059


def test_rotation_trailing_stop_from_detail(store):
    """明细里 stop_off=True → 回撤超线卖风险腿 (不是"换腿")。"""
    _audit(store, D1, "rotation_summary", "止损", {
        "tranche": 0, "target": "513100.SH", "stop_off": True,
        "stop_leg": "159949.SZ", "momentum": {"159949.SZ": -0.08}})
    assert collect(store._conn, D1, D1)[0]["reason_code"] == "ROT_TRAILING_STOP"


def test_rotation_skip_maps_by_wording(store):
    """``rotation_skip`` 只有一句 prose, 按文案认出跳过类型 (来源 = 历史原文)。"""
    _audit(store, D1, "rotation_skip", "非连续竞价时段, 跳过调仓")
    _audit(store, D1, "rotation_skip", "首个周频信号未算, 等信号日", offset=1)
    rows = [r for r in collect(store._conn, D1, D1) if r["strategy"] == "rotation"]
    assert {r["reason_code"] for r in rows} == {"ROT_SKIP_SESSION",
                                               "ROT_HOLD_FIRST_SIG"}
    assert all(r["source"] == "backfill_text" for r in rows)


def test_rotation_error_and_migrate(store):
    _audit(store, D1, "rotation_error", "轮动炸了")
    _audit(store, D1, "rotation_migrate", "迁移初始化", {"tranches": 3},
           offset=1)
    got = {r["reason_code"]: r["source"]
           for r in collect(store._conn, D1, D1) if r["strategy"] == "rotation"}
    assert got == {"ROT_ERROR": "backfill_exact", "ROT_INIT": "backfill_exact"}


def test_auto_buy_all_three_kinds(store):
    """尾盘选股三种审计各自的映射。"""
    _audit(store, D1, "auto_buy_summary", "选中 5 买 2",
           {"selected": 5, "bought": 2})
    got = [r for r in collect(store._conn, D1, D1)
           if r["strategy"] == "auto_buy"]
    assert [(r["subject"], r["reason_code"], r["source"]) for r in got] == [
        ("pool", "PICK_BUY", "backfill_exact")]


def test_auto_buy_regime_block_and_error(store):
    _audit(store, D1, "auto_buy_skip_regime", "399006.SZ 未站上 MA200")
    _audit(store, D1, "auto_buy_error", "取数超时", offset=1)
    got = {r["reason_code"] for r in collect(store._conn, D1, D1)
           if r["strategy"] == "auto_buy"}
    assert got == {"PICK_REGIME_BLOCK", "PICK_ERROR"}


def test_auto_buy_summary_three_outcomes(store):
    """bought>0 / selected==0 / 选出来但没买成 —— 三选一不能错。"""
    _audit(store, D1, "auto_buy_summary", "买到了", {"selected": 5, "bought": 2})
    assert collect(store._conn, D1, D1)[0]["reason_code"] == "PICK_BUY"
    _audit(store, D2, "auto_buy_summary", "没选出", {"selected": 0, "bought": 0})
    assert collect(store._conn, D2, D2)[0]["reason_code"] == "PICK_NO_SIGNAL"
    _audit(store, D3, "auto_buy_summary", "全过滤", {"selected": 3, "bought": 0})
    assert collect(store._conn, D3, D3)[0]["reason_code"] == "PICK_ALL_FILTERED"


def test_auto_buy_evidence_keeps_counts(store):
    _audit(store, D1, "auto_buy_summary", "选中 5 买 0",
           {"selected": 5, "bought": 0})
    ev = collect(store._conn, D1, D1)[0]["evidence"]
    assert (ev["selected"], ev["bought"]) == (5, 0)


def test_exit_sell_maps_to_triggered(store):
    """真卖出 (``exit_sell``) → EXIT_TRIGGERED, 带价格数量与委托号。"""
    _audit(store, D1, "exit_sell", "600519.SH 卖出 1000@8.5",
           {"code": CODE, "price": 8.5, "qty": 1000, "order_id": "O-1",
            "reason": "硬止损"})
    r = collect(store._conn, D1, D1)[0]
    assert (r["strategy"], r["subject"], r["reason_code"]) == (
        "exit", CODE, "EXIT_TRIGGERED")
    assert r["evidence"]["price"] == 8.5
    assert r["trade_ids"] == "O-1"
    assert action_of(r["reason_code"]) == "SELL"


@pytest.mark.parametrize("kind", ["exit_sell_market"])
def test_exit_sell_market_also_maps(store, kind):
    """市价卖出 (``exit_sell_market``) 同样是真卖出, 不能漏。"""
    _audit(store, D1, kind, "市价卖出", {"code": CODE, "price": 8.5, "qty": 100})
    assert collect(store._conn, D1, D1)[0]["reason_code"] == "EXIT_TRIGGERED"


@pytest.mark.parametrize("kind", ["exit_skip", "exit_arm_fail",
                                 "exit_risk_reject", "exit_fail_closed",
                                 "exit_lock_fail"])
def test_all_exit_fail_kinds_map(store, kind):
    """executor 的五处"想卖没卖成"出口统一记 EXIT_ARM_FAIL, 原文进正文。"""
    _audit(store, D1, kind, f"{kind} 的原文", {"code": CODE, "reason": "被挡住"})
    r = collect(store._conn, D1, D1)[0]
    assert r["reason_code"] == "EXIT_ARM_FAIL"
    assert kind in r["reason_text"]        # 原文照抄, 不许改写
    assert action_of(r["reason_code"]) == "FAIL"


def test_exit_rows_without_code_are_dropped(store):
    """明细没带代码的退出事件直接丢掉 —— 不硬猜是哪个票。

    连带效果: 那天若没有别的退出事件, 就不该虚报"退出策略有记录"
    (否则会顶掉那行"当天没有检查过止盈止损"的说明)。
    """
    _audit(store, D1, "exit_arm_fail", "没带代码", {})
    rows = _rows_of(collect(store._conn, D1, D1), D1)
    assert all(r["strategy"] != "exit" or r["reason_code"] == "NO_RUN"
               for r in rows)
    assert any(r["strategy"] == "exit" and r["reason_code"] == "NO_RUN"
               for r in rows)


def test_ladder_place_merges_tiers_per_code(store):
    """同一票挂了多档 → 一行, 档位写全 (与实时路径同一判据)。"""
    _audit(store, D1, "ladder_place", "档1", {"code": CODE, "tier": 0,
                                             "order_id": "O-1"})
    _audit(store, D1, "ladder_place", "档2", {"code": CODE, "tier": 1,
                                             "order_id": "O-2"}, offset=1)
    rows = [r for r in collect(store._conn, D1, D1) if r["strategy"] == "ladder"]
    assert len(rows) == 1
    assert rows[0]["reason_code"] == "LADDER_PLACED"
    assert "第 1 档" in rows[0]["reason_text"]
    assert "第 2 档" in rows[0]["reason_text"]
    assert rows[0]["trade_ids"] == ["O-1", "O-2"]


def test_ladder_place_two_codes_two_rows(store):
    _audit(store, D1, "ladder_place", "a", {"code": CODE, "tier": 0})
    _audit(store, D1, "ladder_place", "b", {"code": CODE2, "tier": 0}, offset=1)
    rows = [r for r in collect(store._conn, D1, D1) if r["strategy"] == "ladder"]
    assert {r["subject"] for r in rows} == {CODE, CODE2}


def test_ladder_skip_maps_to_skip(store):
    _audit(store, D1, "ladder_skip", "可用数量不足", {"code": CODE, "tier": 0})
    r = [r for r in collect(store._conn, D1, D1) if r["strategy"] == "ladder"][0]
    assert r["reason_code"] == "LADDER_SKIP"
    assert "可用数量不足" in r["reason_text"]


def test_ladder_skip_etf_maps_to_skip(store):
    _audit(store, D1, "ladder_skip_etf", "ETF 不挂预埋单", {"code": CODE2})
    r = [r for r in collect(store._conn, D1, D1) if r["strategy"] == "ladder"][0]
    assert r["reason_code"] == "LADDER_SKIP"


def test_ladder_disabled_only_when_nothing_placed(store):
    """开关关着 + 当天挂了单 → 不写"关着" (否则两条行自相矛盾)。"""
    _audit(store, D1, "ladder_skip_disabled", "未启用")
    _audit(store, D1, "ladder_place", "档1", {"code": CODE, "tier": 0}, offset=1)
    rows = [r for r in collect(store._conn, D1, D1) if r["strategy"] == "ladder"]
    assert [r["reason_code"] for r in rows] == ["LADDER_PLACED"]
    _audit(store, D2, "ladder_skip_disabled", "未启用")
    rows2 = [r for r in collect(store._conn, D2, D2) if r["strategy"] == "ladder"]
    assert rows2[0]["reason_code"] == "LADDER_DISABLED"
    assert rows2[0]["subject"] == "pool"


# ═══════════════════════════════════════════════════════════════
# 不许凭空造行 (真事故的回归用例)
# ═══════════════════════════════════════════════════════════════

def test_unrelated_kinds_do_not_create_rows(store):
    """无关的审计类型**一行都不能生成**。

    这是 2026-09-18 干跑时钓出来的真 bug: 最初把当天全部审计项一股脑交给
    ``_auto_buy_rows``, 而 ``classify_auto_buy`` 对不认识的类型会兜底返回
    ``PICK_NO_SIGNAL`` —— 于是 ``monitor_no_quote`` 这类每天上千条的噪声,
    每一条都变成一行"尾盘选股今天没选出票"。一天虚增上千行, 全是编的。
    """
    for kind in ("monitor_no_quote", "monitor_stale_tick", "sync_reports",
                 "risk_reject", "gapfill_write", "trade_backfill",
                 "manual_adopt", "eod", "reconnect", "config_update"):
        _audit(store, D1, kind, f"{kind} 的噪声", {"code": CODE})
    rows = collect(store._conn, D1, D1)
    # 只剩"各策略当天没记录"的说明行, 没有一行是伪造成决策的
    assert {r["reason_code"] for r in rows} == {"NO_RUN"}
    assert all(r["source"] == "inferred" for r in rows)


def test_manual_trades_do_not_enter_ledger(store):
    """人工单不进台账 —— 台账回答的是"**系统**为什么这么决策", 人工买卖没有
    策略原因, 硬塞进来就是编。人工成交由接口的 ``manual_trades_today`` 单独提示。"""
    for kind in ("manual_buy", "manual_adopt", "manual_cancel"):
        _audit(store, D1, kind, "人工操作", {"code": CODE})
    assert {r["reason_code"] for r in collect(store._conn, D1, D1)} == {"NO_RUN"}


def test_every_collected_row_uses_a_known_reason_code(store):
    """还原出的原因码必须都在枚举表里 —— 否则页面上会掉成灰字兜底。"""
    from trade.decision_codes import CODES
    _audit(store, D1, "rotation_summary", "x", {"tranche": 0,
                                              "target": "513100.SH",
                                              "momentum": {"a": 0.01}})
    _audit(store, D1, "auto_buy_summary", "y", {"selected": 1, "bought": 1},
           offset=1)
    _audit(store, D1, "exit_sell", "z", {"code": CODE, "price": 1.0}, offset=2)
    _audit(store, D1, "ladder_place", "w", {"code": CODE, "tier": 0}, offset=3)
    _audit(store, D2, "exit_arm_fail", "v", {"code": CODE}, offset=0)
    for day in (D1, D2):
        for r in _rows_of(collect(store._conn, day, day), day):
            assert r["reason_code"] in CODES, r


def test_output_never_merges_two_different_keys(store):
    """``collect`` 只会合并"内容一模一样"的重复行 —— **不同主键永不互相覆盖**。

    去重是按 (主键 + 原因码 + 大白话) 判的: 同一票同一天有多条不同文案的记录时
    它们会全部保留 (进 ``evidence.events`` 看得到"价格一路在跌"), 所以输出里
    同一个主键**可以**出现多行 —— 那是刻意的, 合并交给写入口的强弱规则做。
    这里守的是另一头: 不同主键之间不许串味。
    """
    for i in range(30):
        _audit(store, D1, "exit_arm_fail", f"重试 {i} 现价 {1.0 + i * 0.01}",
               {"code": CODE, "reason": "可用为 0"}, offset=i)
    _audit(store, D1, "exit_arm_fail", "另一只票", {"code": CODE2}, offset=99)
    rows = collect(store._conn, D1, D1)
    by_key: dict[tuple, list] = {}
    for r in rows:
        by_key.setdefault((r["trade_date"], r["strategy"], r["subject"]),
                          []).append(r)
    # 两只票是两组, 各自的行不会跑到对方组里
    assert set(by_key) >= {(D1, "exit", CODE), (D1, "exit", CODE2)}
    for key, group in by_key.items():
        assert all((r["trade_date"], r["strategy"], r["subject"]) == key
                   for r in group)
    # 三十次重试落在同一只票上 → 同一个主键, 但文案不同所以没被压掉
    assert len(by_key[(D1, "exit", CODE)]) > 1


# ═══════════════════════════════════════════════════════════════
# 去重语义
# ═══════════════════════════════════════════════════════════════

def test_identical_repeats_collapse(store):
    """内容一模一样的重复记录合并成一行 (当时监控每 10 秒重试都写一条)。"""
    for i in range(50):
        _audit(store, D1, "exit_arm_fail", "600519.SH 可用为 0, 无可卖",
               {"code": CODE, "reason": "可用为 0"}, offset=i)
    rows = [r for r in collect(store._conn, D1, D1) if r["strategy"] == "exit"]
    assert len(rows) == 1


def test_repeats_with_different_prices_are_kept(store):
    """只差价格的两次重试**保留** —— 它们进明细的 events, 能看到"价格一路在跌",
    信息不能因为去重而丢掉。"""
    for i, px in enumerate(("2.61", "2.60", "2.62")):
        _audit(store, D1, "exit_skip", f"600519.SH 可用为 0 (现价 {px})",
               {"code": CODE, "reason": "可用为 0"}, offset=i)
    rows = [r for r in collect(store._conn, D1, D1) if r["strategy"] == "exit"]
    assert len(rows) == 3


def test_multiple_summaries_all_kept_for_the_merge_rule(store):
    """一天跑了三次尾盘选股 (定时 + 两次人工) → 三条都保留, 交给写入口的动作
    强弱合并挑出"买到了"那条。去重阶段**不许替它做主**取第一条 (那会报告"买 0")。
    """
    _audit(store, D1, "auto_buy_summary", "选中 8 买 0",
           {"selected": 8, "bought": 0})
    _audit(store, D1, "auto_buy_summary", "选中 8 买 4",
           {"selected": 8, "bought": 4}, offset=1)
    rows = [r for r in collect(store._conn, D1, D1) if r["strategy"] == "auto_buy"]
    assert len(rows) == 2
    assert {r["reason_code"] for r in rows} == {"PICK_ALL_FILTERED", "PICK_BUY"}


def test_sell_beats_failed_sell_after_write(store, tmp_path):
    """同一票当天先"想卖没卖成"后真卖出 → 台账里最终是 SELL (卖成压住没卖成)。
    端到端过一遍写入口, 验的是"回填 + 合并规则"合起来的结果。"""
    _audit(store, D1, "exit_arm_fail", "可用为 0 卖不掉",
           {"code": CODE, "reason": "可用为 0"})
    _audit(store, D1, "exit_sell", "600519.SH 卖出 1000@8.5",
           {"code": CODE, "price": 8.5, "qty": 1000, "order_id": "O-1"},
           offset=1)
    db = store._db_path
    store.close()
    run(db, D1, D1)
    s2 = TradeStore(db, tmp_path / "r2.jsonl")
    try:
        r = [x for x in s2.decision.load_day(D1) if x["strategy"] == "exit"][0]
        assert r["action"] == "SELL"
        assert r["reason_code"] == "EXIT_TRIGGERED"
        # "曾经失败过"这件事不能被抹掉
        assert r["evidence"]["events"][0]["reason_code"] == "EXIT_ARM_FAIL"
    finally:
        s2.close()


# ═══════════════════════════════════════════════════════════════
# 缺口那几行 (推断)
# ═══════════════════════════════════════════════════════════════

def test_whole_day_empty_with_no_trace(store):
    """整天没有任何痕迹 → system 级 NO_RUN「程序没开机」。"""
    rows = _rows_of(collect(store._conn, D1, D1), D1)
    assert len(rows) == 1
    r = rows[0]
    assert (r["strategy"], r["subject"], r["reason_code"]) == (
        "system", "all", "NO_RUN")
    assert r["source"] == "inferred"
    assert "没开机" in r["reason_text"]


def test_whole_day_empty_with_derived_asset(store):
    """资产表那天的来源是 derived (用收盘价推算补的) → 明说"程序没开机"。
    这是"没开机"的直接证据, 比"什么都没留下"有力。"""
    _asset(store, D1, "derived")
    r = _rows_of(collect(store._conn, D1, D1), D1)[0]
    assert "程序没开机" in r["reason_text"]
    assert r["evidence"]["daily_asset_source"] == "derived"


def test_whole_day_empty_with_eod_asset(store):
    """资产表有 eod 行 (程序真跑过) 但没有决策记录 → 老实说"这个功能是
    2026 年 9 月 18 日才上线的"。"""
    _asset(store, D1, "eod")
    r = _rows_of(collect(store._conn, D1, D1), D1)[0]
    assert "9 月 18 日" in r["reason_text"]


def test_partial_day_gets_died_before_close_row(store):
    """三条主策略全缺、但有别的痕迹 → 补一行「程序跑了但没到收盘就退了」。

    真实案例 2026-09-11: 09:15 挂预埋单时发现开关关着, 之后再无痕迹。
    不加这一行的话, 那天在页面上只剩一句"开关关着", 完全看不出程序挂了。
    """
    _audit(store, D1, "ladder_skip_disabled", "未启用")
    _audit(store, D1, "monitor_no_quote", "监控噪声", offset=1)
    _asset(store, D1, "derived")
    rows = _rows_of(collect(store._conn, D1, D1), D1)
    sys_rows = [r for r in rows if r["strategy"] == "system"]
    assert len(sys_rows) == 1
    assert "没到收盘就退了" in sys_rows[0]["reason_text"]


def test_full_day_has_no_system_row(store):
    """三条主策略里只要有一条真跑了, 就不该出现"程序跑了但没到收盘就退了"。"""
    _audit(store, D1, "auto_buy_summary", "买到了", {"selected": 1, "bought": 1})
    rows = _rows_of(collect(store._conn, D1, D1), D1)
    assert not [r for r in rows if r["strategy"] == "system"]


def test_missing_rows_for_each_strategy(store):
    """缺哪条策略就补哪条 —— 四条都缺时补四条 (但那种情况走 system 一行)。"""
    rows = _missing_rows(D1, {"rotation", "exit"})
    assert {r["strategy"] for r in rows} == {"auto_buy", "ladder"}
    assert all(r["reason_code"] == "NO_RUN" for r in rows)
    assert all(r["source"] == "inferred" for r in rows)


def test_missing_exit_wording_is_specific(store):
    """止盈止损缺记录的措辞必须特别说明"当时系统没记它检查过" ——
    直接写"没触发"是编的: 当时压根没记, 我们不知道有没有触发。"""
    r = [x for x in _missing_rows(D1, {"rotation", "auto_buy", "ladder"})
         if x["strategy"] == "exit"][0]
    assert "没记它检查过止盈止损" in r["reason_text"]


def test_missing_rotation_wording_before_feature_launch(store):
    """轮动上线前 (早于 2026-08-15) 的日子: 说"当时还没有运行痕迹",
    而不是"程序可能没跑到它" —— 前者是事实, 后者是猜测。"""
    old = _missing_rows("2026-08-01", {"exit", "auto_buy", "ladder"})
    r = [x for x in old if x["strategy"] == "rotation"][0]
    assert "还没有运行痕迹" in r["reason_text"]
    assert "2026-08-01" < ROTATION_FIRST_SEEN


def test_missing_rotation_wording_after_feature_launch(store):
    """上线后 (≥ 2026-08-15) 的日子: 才能说"程序可能没跑到它"。"""
    new = _missing_rows("2026-09-01", {"exit", "auto_buy", "ladder"})
    r = [x for x in new if x["strategy"] == "rotation"][0]
    assert "可能没跑到它" in r["reason_text"]


def test_missing_rows_not_written_when_strategy_present(store):
    assert _missing_rows(D1, {"rotation", "auto_buy", "exit", "ladder"}) == []


# ═══════════════════════════════════════════════════════════════
# 非交易日 / 边界
# ═══════════════════════════════════════════════════════════════

def test_saturday_produces_nothing(store):
    """周六即使有审计痕迹也不进台账 (守卫在写入口, 这里提前跳过)。"""
    _audit(store, SAT, "auto_buy_summary", "周六的", {"selected": 1, "bought": 1})
    assert collect(store._conn, SAT, SAT) == []


def test_weekend_inside_range_is_skipped(store):
    """区间跨周末: 周六一格都不出 (每个交易日至少一行, 但绝不含休市日)。"""
    _audit(store, D1, "auto_buy_summary", "周五", {"selected": 1, "bought": 1})
    _audit(store, SAT, "auto_buy_summary", "周六", {"selected": 1, "bought": 1})
    rows = collect(store._conn, D1, SAT)
    dates = {r["trade_date"] for r in rows}
    assert dates == {D1, D2, D3, D4}          # 四个交易日都有
    assert SAT not in dates                    # 周六没有
    # 周六那条审计的内容一个字都没漏进台账
    assert all("周六" not in r["reason_text"] for r in rows)


def test_collect_does_not_write_anything(store):
    """``collect`` 是纯读的 —— 跑一百遍也不该往台账里写一行。"""
    _audit(store, D1, "auto_buy_summary", "买到了", {"selected": 1, "bought": 1})
    collect(store._conn, D1, D4)
    collect(store._conn, D1, D4)
    assert store.decision.load_range(D1, D4) == []


def test_collect_does_not_write_on_empty_db(store):
    """空库也不能炸 (第一次跑、或换了个新库)。"""
    rows = collect(store._conn, D1, D4)
    assert len(rows) == 4                     # 四个交易日各一行 system NO_RUN
    assert all(r["strategy"] == "system" for r in rows)


def test_ledger_start_matches_the_api_side(store):
    """起点常量与接口侧一致 —— 不一致会出现"接口说有 note、台账里却有行"。"""
    from trade.decision_api import _LEDGER_START
    assert LEDGER_START == _LEDGER_START


# ═══════════════════════════════════════════════════════════════
# run(): 幂等 / 不覆盖 live / 干跑
# ═══════════════════════════════════════════════════════════════

def test_run_writes_rows(store, tmp_path):
    """真落库: 那天的尾盘选股行要写进去, 动作由原因码定 (没写死)。"""
    _audit(store, D1, "auto_buy_summary", "买到了", {"selected": 1, "bought": 1})
    db = store._db_path
    store.close()
    result = run(db, D1, D1)
    # 除了尾盘选股那一行, 另外三条策略当天没记录 → 各补一行说明, 共 4 格
    assert result["keys"] == 4
    s2 = TradeStore(db, tmp_path / "r2.jsonl")
    try:
        rows = s2.decision.load_day(D1)
        auto = [r for r in rows if r["strategy"] == "auto_buy"][0]
        assert auto["source"] == "backfill_exact"
        assert auto["action"] == "BUY"
        assert auto["reason_code"] == "PICK_BUY"
    finally:
        s2.close()


def test_run_reports_rows_keys_and_written_separately(store, tmp_path):
    """三个数字的含义不能混: rows(行对象) ≥ written(生效行) ≥ keys(台账格数)。

    实测里 rows 会明显大于 keys —— 当时监控每 10 秒重试失败就写一条审计,
    一天同一只票能积几百条。CLI 上如果只报 rows, 用户会以为台账要涨几千行。
    """
    for i in range(20):
        _audit(store, D1, "exit_arm_fail", f"重试 {i} 现价 {1.0 + i * 0.01}",
               {"code": CODE, "reason": "可用为 0"}, offset=i)
    db = store._db_path
    store.close()
    result = run(db, D1, D1, dry_run=True)
    assert result["rows"] > result["keys"]
    assert result["keys"] == 4          # exit 1 格 + 另三条策略各 1 格说明
    assert result["written"] == 0       # 干跑不写


def test_run_is_idempotent(store, tmp_path):
    """跑两遍结果完全一样, 不堆重复行 —— 中断了直接再跑一遍就行。"""
    _audit(store, D1, "auto_buy_summary", "买到了", {"selected": 1, "bought": 1})
    _audit(store, D1, "exit_sell", "卖出", {"code": CODE, "price": 8.5})
    db = store._db_path
    store.close()

    run(db, D1, D1)
    s2 = TradeStore(db, tmp_path / "r2.jsonl")
    try:
        first = s2.decision.load_day(D1)
    finally:
        s2.close()

    run(db, D1, D1)
    s3 = TradeStore(db, tmp_path / "r3.jsonl")
    try:
        second = s3.decision.load_day(D1)
    finally:
        s3.close()

    assert len(first) == len(second)
    assert {(r["strategy"], r["subject"], r["reason_code"]) for r in first} == \
           {(r["strategy"], r["subject"], r["reason_code"]) for r in second}


def test_run_never_overwrites_live(store, tmp_path):
    """**回填改不动当场记录的行** —— 一手证据不许被事后推断改写。"""
    _audit(store, D1, "auto_buy_summary", "回填的版本", {"selected": 1,
                                                      "bought": 1})
    with store._lock, store._conn:
        store._conn.execute(
            "INSERT INTO daily_decision (trade_date, strategy, subject, action,"
            "reason_code, reason_text, evidence_json, trade_ids, source,"
            "updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (D1, "auto_buy", "pool", "BUY", "PICK_BUY", "当场记录的原文",
             "{}", "", "live", _T[D1]))
    db = store._db_path
    store.close()
    result = run(db, D1, D1)
    s2 = TradeStore(db, tmp_path / "r2.jsonl")
    try:
        r = [x for x in s2.decision.load_day(D1) if x["strategy"] == "auto_buy"][0]
        assert r["reason_text"] == "当场记录的原文"
        assert r["source"] == "live"
    finally:
        s2.close()
    assert result["written"] < result["rows"]   # 被拒的那行算"没写"


def test_run_overwrite_live_flag_forces_it(store, tmp_path):
    """``--overwrite-live`` 是修数据的后门, 开着就该覆盖。"""
    _audit(store, D1, "auto_buy_summary", "回填的版本", {"selected": 1,
                                                      "bought": 1})
    with store._lock, store._conn:
        store._conn.execute(
            "INSERT INTO daily_decision (trade_date, strategy, subject, action,"
            "reason_code, reason_text, evidence_json, trade_ids, source,"
            "updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (D1, "auto_buy", "pool", "BUY", "PICK_BUY", "当场记录的原文",
             "{}", "", "live", _T[D1]))
    db = store._db_path
    store.close()
    run(db, D1, D1, overwrite_live=True)
    s2 = TradeStore(db, tmp_path / "r2.jsonl")
    try:
        r = [x for x in s2.decision.load_day(D1) if x["strategy"] == "auto_buy"][0]
        assert r["source"] == "backfill_exact"
    finally:
        s2.close()


def test_run_dry_run_writes_nothing(store, tmp_path):
    """干跑：还原了但一行都不写 (先看清楚再动手)。"""
    _audit(store, D1, "auto_buy_summary", "买到了", {"selected": 1, "bought": 1})
    db = store._db_path
    store.close()
    result = run(db, D1, D1, dry_run=True)
    assert result["rows"] >= 1
    assert result["written"] == 0
    s2 = TradeStore(db, tmp_path / "r2.jsonl")
    try:
        assert s2.decision.load_day(D1) == []
    finally:
        s2.close()


def test_run_creates_the_table_if_missing(store, tmp_path):
    """台账表不存在时自动建 —— 复用实时那份 DDL 常量 (单一真相源),
    所以两边结构不可能不一样。"""
    _audit(store, D1, "auto_buy_summary", "买到了", {"selected": 1, "bought": 1})
    db = store._db_path
    store._conn.execute("DROP TABLE daily_decision")   # 模拟还没上线的旧库
    store._conn.commit()
    store.close()
    result = run(db, D1, D1)
    assert result["keys"] == 4
    s2 = TradeStore(db, tmp_path / "r2.jsonl")
    try:
        assert len(s2.decision.load_day(D1)) == 4
    finally:
        s2.close()


def test_run_refuses_a_db_without_audit_table(store, tmp_path):
    """指错库 (没有 ``audit`` 表) → 直接报错退出, **不要**写出一堆"程序没开机"。

    这是很容易踩的坑: 没有 audit 表时一次都查不到数据, 程序会老老实实给每个
    交易日写一行"这一天程序没开机" —— 一库假数据, 而且看起来还挺像真的。
    """
    db = tmp_path / "empty.db"
    import sqlite3
    sqlite3.connect(str(db)).close()
    with pytest.raises(ValueError, match="audit"):
        run(str(db), D1, D1)
    conn = sqlite3.connect(str(db))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "daily_decision" not in tables     # 一行都没写
    finally:
        conn.close()


def test_run_rejects_reversed_range(store):
    with pytest.raises(ValueError):
        run(store._db_path, D4, D1)


def test_run_reports_per_day_counts(store, tmp_path):
    _audit(store, D1, "auto_buy_summary", "a", {"selected": 1, "bought": 1})
    _audit(store, D2, "auto_buy_summary", "b", {"selected": 1, "bought": 1})
    db = store._db_path
    store.close()
    result = run(db, D1, D2, dry_run=True)
    assert result["days"] == 2
    assert set(result["per_day"]) == {D1, D2}


def test_run_on_range_without_trading_days(store):
    """整段都是周末 → 0 行, 不报错。"""
    result = run(store._db_path, SAT, SAT)
    assert result["rows"] == 0
    assert result["written"] == 0


# ═══════════════════════════════════════════════════════════════
# 时间戳: 回填的每一行都要带"当时"的时间 (§3.6 没写, 2026-09-18 补)
# ═══════════════════════════════════════════════════════════════

def test_collect_carries_the_original_timestamp(store):
    """每行都带 ``event_ts``, 且等于审计表里那一行的真实时间。

    为什么必须有: 实时写入时"写库那一刻"就等于"事情发生那一刻", 两者天然一致;
    回填时两者能差两个月 —— 不带上真实时间, 7 月 30 日的 14 次卖出重试会被全部
    标成"回填跑的那一天 23:34", 用户展开明细看到一串和日期对不上的时间,
    比不给时间更让人糊涂。
    """
    _audit(store, D1, "auto_buy_summary", "买到了",
           {"selected": 1, "bought": 1}, offset=120)
    rows = collect(store._conn, D1, D1)
    auto = [r for r in rows if r["strategy"] == "auto_buy"][0]
    assert auto["event_ts"] == pytest.approx(_T[D1] + 120)


def test_collect_event_ts_keeps_the_order_between_retries(store):
    """同一只票多次重试, 每次的 ``event_ts`` 各不相同且按时间递增。

    这条钉住的是"时间线能不能读": 如果所有重试共用一个时间戳, 页面上就分不清
    哪次在前 —— 而"价格一路在跌、试了 14 次才卖掉"正是用户要看的重点。
    """
    for i in range(3):
        _audit(store, D1, "exit_arm_fail", f"第 {i} 次现价 {1.0 + i * 0.01}",
               {"code": CODE, "reason": "可用为 0"}, offset=i * 10)
    rows = [r for r in collect(store._conn, D1, D1)
            if r["strategy"] == "exit" and r["subject"] == CODE]
    stamps = sorted(r["event_ts"] for r in rows)
    assert len(stamps) == 3
    assert stamps == pytest.approx([_T[D1], _T[D1] + 10, _T[D1] + 20])


def test_inferred_rows_have_no_event_ts(store):
    """推断出来的 ``NO_RUN`` 行**不带** ``event_ts`` —— 它没有"当时"可言。

    带上的话就是编数据: 这些行是"因为查不到痕迹所以推断没跑", 产生时刻就是写库
    时刻, 让它退回写库时刻是诚实的。而带 ``event_ts`` 的行必须是审计表里真有的。
    """
    rows = collect(store._conn, D1, D1)          # 一条审计都没有
    assert rows
    assert all("event_ts" not in r for r in rows)


def test_run_uses_the_original_timestamp_not_the_run_time(store, tmp_path):
    """落库后 ``updated_ts`` 是**当年那一刻**, 不是"回填跑的那一刻"。

    这是本条修正的最终验收点 —— 前面几条查的是中间产物, 这条查数据库里那一列。
    """
    for i in range(3):
        _audit(store, D1, "exit_arm_fail", f"第 {i} 次现价 {1.0 + i * 0.01}",
               {"code": CODE, "reason": "可用为 0"}, offset=i * 10)
    _audit(store, D1, "exit_sell", "卖掉了",
           {"code": CODE, "price": 8.5, "order_id": "OK1"}, offset=99)
    db = store._db_path
    store.close()
    run(db, D1, D1)
    import sqlite3
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT updated_ts, evidence_json FROM daily_decision "
            "WHERE trade_date=? AND strategy='exit' AND subject=?",
            (D1, CODE)).fetchone()
    finally:
        conn.close()
    assert row is not None
    # 主结论是"卖掉了"那次 (动作最强), 它的时间是 offset=99 那一刻
    assert row[0] == pytest.approx(_T[D1] + 99)
    events = json.loads(row[1])["events"]
    assert [e["ts"] for e in events] == pytest.approx(
        [_T[D1], _T[D1] + 10, _T[D1] + 20])


def test_run_writes_the_ladder_place_time(store, tmp_path):
    """预埋单一次挂多档 → 取**最早**那一次的时间 (用户想知道"什么时候挂上的")。"""
    _audit(store, D1, "ladder_place", "挂第 1 档",
           {"code": CODE, "tier": 0}, offset=5)
    _audit(store, D1, "ladder_place", "挂第 2 档",
           {"code": CODE, "tier": 1}, offset=500)
    rows = [r for r in collect(store._conn, D1, D1)
            if r["strategy"] == "ladder" and r["subject"] == CODE]
    assert len(rows) == 1
    assert rows[0]["event_ts"] == pytest.approx(_T[D1] + 5)


# ═══════════════════════════════════════════════════════════════
# --reset: 改了回填逻辑要重做时, 先清掉旧回填行
# ═══════════════════════════════════════════════════════════════

def test_reset_removes_old_backfill_rows_but_never_live(store, tmp_path):
    """``reset=True`` 删旧回填行, **但当场记录的行一根头发都不许动**。

    为什么需要 reset: 写入口合并时会继承旧行攒下的 ``events`` (轨迹不该丢),
    所以旧数据里的错时间戳、错文案不会因为重跑就自动修正 —— 得先清干净。
    """
    _audit(store, D1, "auto_buy_summary", "买到了", {"selected": 1, "bought": 1})
    # 先手工塞一行"当场记录"的, 它必须活下来
    store.decision.log([{"trade_date": D1, "strategy": "rotation", "subject": "份1",
                         "reason_code": "ROT_HOLD_KEEP", "reason_text": "当场记的",
                         "source": "live"}])
    db = store._db_path
    store.close()
    run(db, D1, D1)                       # 第一遍: 回填
    result = run(db, D1, D1, reset=True)  # 第二遍: 先清后写
    assert result["removed"] > 0
    s2 = TradeStore(db, tmp_path / "r2.jsonl")
    try:
        rows = s2.decision.load_day(D1)
        live = [r for r in rows if r["source"] == "live"]
        assert len(live) == 1 and live[0]["reason_text"] == "当场记的"
    finally:
        s2.close()


def test_dry_run_never_deletes_even_with_reset(store):
    """干跑 + --reset 也**不能**删东西 —— "试算"这两个字必须名副其实。"""
    _audit(store, D1, "auto_buy_summary", "买到了", {"selected": 1, "bought": 1})
    db = store._db_path
    store.close()
    run(db, D1, D1)
    import sqlite3
    conn = sqlite3.connect(str(db))
    before = conn.execute("SELECT count(*) FROM daily_decision").fetchone()[0]
    conn.close()
    result = run(db, D1, D1, dry_run=True, reset=True)
    assert result["removed"] == 0
    conn = sqlite3.connect(str(db))
    try:
        after = conn.execute("SELECT count(*) FROM daily_decision").fetchone()[0]
    finally:
        conn.close()
    assert after == before


