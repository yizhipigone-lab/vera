"""决策台账写入口契约测试 (2026-09-18)。

锁住 `TradeStore.decision` (即 `trade/store.py::DailyDecisionStore`) 的四条硬规矩:

1. **非交易日拒写** —— 守卫收在唯一写入口上, 四个写入点谁也不必各自记牢;
   因为用的是「行自己的日期」, 历史回填到周末同样被拦;
2. **幂等** —— 主键 (日期, 策略, 对象) 天然去重, 同一天定时 + 人工各跑一次
   只覆盖成最新, 不堆重复行;
3. **动作强弱合并** —— SELL > BUY > FAIL > INFO > HOLD。更弱的动作不丢,
   它被追加进 `evidence_json.events` 里可查;
4. **来源可信度保护** —— 低可信度不许覆盖高可信度。这条容易写错成「是 live
   就不许覆盖」, 那会把**实时路径自己重跑**也拦掉 (2026-09-18 冒烟实测抓到),
   所以下面既有「回填改不动 live」也有「live 能改回填」还有「live 能改 live」。
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade import store as store_mod  # noqa: E402
from trade.store import TradeStore  # noqa: E402

# 真实交易日 (用真历法, 不 monkeypatch 的那几条用它们)
D1 = "2026-09-16"   # 周三, 交易日
D2 = "2026-09-17"   # 周四, 交易日
SAT = "2026-09-19"  # 周六, 休市


@pytest.fixture()
def store(tmp_path):
    s = TradeStore(tmp_path / "trade.db", tmp_path / "raw.jsonl")
    yield s
    s.close()


def _row(date=D1, strategy="rotation", subject="份1", code="ROT_HOLD_KEEP",
         action=None, text="维持持仓（不用换腿）", source="live",
         evidence=None, trade_ids=""):
    r = {"trade_date": date, "strategy": strategy, "subject": subject,
         "reason_code": code, "reason_text": text, "source": source,
         "evidence": evidence if evidence is not None else {}, "trade_ids": trade_ids}
    if action is not None:
        r["action"] = action
    return r


def _tables(store):
    return {r[0] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


# ── 建表 ────────────────────────────────────────────────────────

def test_table_and_index_created(store):
    """表 + 日期索引都要有。索引漏了, 决策日历按月取数会全表扫。"""
    assert "daily_decision" in _tables(store)
    idx = {r[0] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    assert "idx_daily_decision_date" in idx


def test_decision_store_hangs_off_trade_store(store):
    """`store.decision` 与 `store.orders` 同级 —— 同一个 SQLite、同一把锁,
    不新增进程/中间件/转发层 (铁律: 如无必要勿增实体)。"""
    assert store.decision._conn is store._conn
    assert store.decision._lock is store._lock


def test_ddl_is_reusable_constant():
    """DDL 必须是独立常量, 供离线回填工具 import 同一份 ——
    否则实时与回填各建一张结构不同的表, 迟早对不上。"""
    assert "CREATE TABLE IF NOT EXISTS daily_decision" in store_mod.DAILY_DECISION_DDL
    assert "PRIMARY KEY (trade_date, strategy, subject)" in store_mod.DAILY_DECISION_DDL


# ── 非交易日拒写 ────────────────────────────────────────────────

def test_saturday_is_rejected(store):
    """真历法: 周六休市, 一行都不该写进去。"""
    assert store.decision.log([_row(date=SAT)]) == 0
    assert store.decision.load_day(SAT) == []


def test_national_holiday_is_rejected(store):
    """真历法: 国庆假期同样不是交易日。"""
    assert store.decision.log([_row(date="2026-10-01")]) == 0
    assert store.decision.load_day("2026-10-01") == []


def test_bad_date_string_is_rejected(store):
    """坏日期串一律拒写, 不能靠异常兜底 —— 那样会污染审计表。
    注意 8 位数字的 ``20260916`` **不在**这里: 它是 trades 表的口径, 应当被收下
    并转换 (见下面那条), 不是坏值。"""
    for bad in ("", "2026-13-45", "2026-02-30", None, "今天", "2026/09/16",
                "2026-W37-3", "2026-9-16", "2616-09-16x"):
        assert store.decision.log([_row(date=bad)]) == 0, bad
    assert store.decision.load_day("2026-09-16") == []


def test_compact_date_is_normalized_to_iso(store):
    """``20260916`` (trades 表口径) 必须被收下并**收敛成** ``2026-09-16``。

    这条锁的是一个静默数据事故: 如果只靠 ``date.fromisoformat`` 的宽松语法放行,
    那行会以 ``"20260916"`` 原串为键落库 —— 同一天在表里就有了两个键,
    ``load_day("2026-09-16")`` 和决策日历都找不到它。表现是"这天明明跑过却没记录",
    而且不报错。修复后两个口径必须落进同一个键。
    """
    assert store.decision.log([_row(date="20260916")]) == 1
    rows = store.decision.load_day("2026-09-16")          # 用规范口径能读到
    assert len(rows) == 1
    assert rows[0]["trade_date"] == "2026-09-16"          # 落库的是规范口径
    assert store.decision.load_day("20260916") == []      # 不存在第二个键


def test_two_date_formats_do_not_split_one_day(store):
    """同一天分别用两种写法写两次 → 仍然只有一行 (主键收敛到同一口径)。"""
    store.decision.log([_row(date="20260916", text="第一次(紧凑口径)")])
    store.decision.log([_row(date="2026-09-16", text="第二次(规范口径)")])
    rows = store.decision.load_day("2026-09-16")
    assert len(rows) == 1
    assert rows[0]["reason_text"] == "第二次(规范口径)"


def test_caller_dict_is_not_mutated(store):
    """归一化不能顺手改掉调用方自己那份 dict —— 那种副作用最难排查。"""
    row = _row(date="20260916")
    store.decision.log([row])
    assert row["trade_date"] == "20260916"


def test_guard_uses_row_own_date_not_today(store):
    """守卫用的是**行自己的日期** —— 所以历史回填到周末照样被拦。
    如果守卫改成「看今天是不是交易日」, 这条会红。"""
    rows = [_row(date=D1, subject="份1"), _row(date=SAT, subject="份2")]
    assert store.decision.log(rows) == 1
    assert [r["subject"] for r in store.decision.load_day(D1)] == ["份1"]
    assert store.decision.load_day(SAT) == []


def test_mixed_batch_keeps_the_valid_rows(store):
    """一批里混了休市日, 不能因此整批放弃 —— 有效的照写。"""
    rows = [_row(date=SAT), _row(date=D1), _row(date="2026-10-01")]
    assert store.decision.log(rows) == 1


# ── 幂等 / 覆盖 ─────────────────────────────────────────────────

def test_same_key_twice_keeps_one_row(store):
    """同一天同一策略同一对象写两次 = 只留一行 (主键天然幂等)。"""
    store.decision.log([_row()])
    store.decision.log([_row()])
    assert len(store.decision.load_day(D1)) == 1


def test_live_can_overwrite_live(store):
    """实时路径**自己重跑**必须允许覆盖 (定时一次 + 人工触发一次是常见情形)。
    这正是「简化成『是 live 就不许覆盖』」会踩的坑。"""
    store.decision.log([_row(code="ROT_HOLD_KEEP", text="第一次：维持")])
    store.decision.log([_row(code="ROT_SWITCH", text="第二次：换腿",
                             evidence={"target": "513100.SH"})])
    rows = store.decision.load_day(D1)
    assert len(rows) == 1
    assert rows[0]["reason_code"] == "ROT_SWITCH"
    assert rows[0]["reason_text"] == "第二次：换腿"
    assert rows[0]["evidence"]["target"] == "513100.SH"


def test_upsert_only_updates_that_one_key(store):
    """覆盖是「按主键精确定位」的 —— 别的行不能被顺手改掉。"""
    store.decision.log([_row(subject="份1"), _row(subject="份2", code="ROT_SWITCH")])
    store.decision.log([_row(subject="份1", code="ROT_TRAILING_STOP")])
    got = {r["subject"]: r["reason_code"] for r in store.decision.load_day(D1)}
    assert got == {"份1": "ROT_TRAILING_STOP", "份2": "ROT_SWITCH"}


def test_empty_input_is_noop(store):
    """空批次 / None / 全是假值 —— 返回 0, 不写任何东西, 不报错。"""
    assert store.decision.log([]) == 0
    assert store.decision.log(None) == 0
    assert store.decision.log([None, {}]) == 0
    assert store.decision.load_day(D1) == []


# ── 动作强弱合并 ────────────────────────────────────────────────

def test_sell_beats_buy(store):
    """同一天先「换腿买」(BUY) 后「回撤卖」(SELL) → 主结论显示卖。
    有成交的那条永远压得住没成交/更弱的那条。"""
    store.decision.log([_row(code="ROT_SWITCH", action="BUY", text="换腿买入")])
    store.decision.log([_row(code="ROT_TRAILING_STOP", action="SELL", text="回撤超线卖出")])
    r = store.decision.load_day(D1)[0]
    assert r["action"] == "SELL"
    assert r["reason_code"] == "ROT_TRAILING_STOP"


def test_weaker_action_is_appended_to_events_not_lost(store):
    """更弱的动作主结论不动, 但「当天还发生过这件事」必须留在 events 里可查。
    真实场景: 上午「想卖没卖成」(FAIL), 下午真卖成了 (SELL) ——
    「曾经失败过」不能被抹掉, 否则复盘时找不到那次失败。"""
    store.decision.log([_row(action="SELL", code="EXIT_TRIGGERED")])
    store.decision.log([_row(action="FAIL", code="EXIT_ARM_FAIL", text="想卖但没卖成")])
    r = store.decision.load_day(D1)[0]
    assert r["action"] == "SELL"                       # 主结论仍是卖成
    events = r["evidence"]["events"]
    assert len(events) == 1
    assert events[0]["action"] == "FAIL"
    assert events[0]["reason_code"] == "EXIT_ARM_FAIL"
    assert events[0]["reason_text"] == "想卖但没卖成"


def test_equal_rank_action_overwrites(store):
    """同级动作 (SELL vs SELL) 允许覆盖成最新 —— 不是「同级就拦」。"""
    store.decision.log([_row(action="SELL", code="EXIT_TRIGGERED", text="第一次卖")])
    store.decision.log([_row(action="SELL", code="ROT_TRAILING_STOP", text="第二次卖")])
    r = store.decision.load_day(D1)[0]
    assert r["reason_text"] == "第二次卖"


def test_multiple_weaker_events_accumulate(store):
    """弱动作可以叠加多条, 不会被后一条挤掉。"""
    store.decision.log([_row(action="SELL")])
    store.decision.log([_row(action="HOLD", code="EXIT_NO_TRIGGER")])
    store.decision.log([_row(action="INFO", code="LADDER_PLACED")])
    events = store.decision.load_day(D1)[0]["evidence"]["events"]
    assert [e["action"] for e in events] == ["HOLD", "INFO"]


def test_events_survive_broken_old_evidence_json(store):
    """老行的 evidence_json 坏了 (不是合法 JSON) 也不能炸 —— 坏值丢弃重来。"""
    store.decision.log([_row(action="SELL")])
    store._conn.execute("UPDATE daily_decision SET evidence_json='{坏' "
                        "WHERE trade_date=?", (D1,))
    store._conn.commit()
    store.decision.log([_row(action="HOLD")])
    assert store.decision.load_day(D1)[0]["evidence"]["events"]


# ── 来源可信度保护 ──────────────────────────────────────────────

def test_backfill_cannot_overwrite_live(store):
    """历史回填改不动当场记录的行 —— 当场写下的原文是一手证据, 不能被事后推断改写。"""
    store.decision.log([_row(code="ROT_SWITCH", text="当场记录的原文")])
    n = store.decision.log([_row(code="ROT_HOLD_KEEP", text="回填猜的",
                                 source="backfill_text", action="BUY")])
    assert n == 0
    r = store.decision.load_day(D1)[0]
    assert r["reason_text"] == "当场记录的原文"
    assert r["source"] == "live"


def test_inferred_cannot_overwrite_backfill_exact(store):
    """同为历史来源, 推断(inferred) 改不动复原(backfill_exact)。"""
    store.decision.log([_row(source="backfill_exact", text="从审计明细复原")])
    assert store.decision.log([_row(source="inferred", text="猜的")]) == 0
    assert store.decision.load_day(D1)[0]["reason_text"] == "从审计明细复原"


def test_live_can_overwrite_backfill(store):
    """「回填过的日子后来真跑起来了」→ 实时写入放行 (更好的证据来了)。"""
    store.decision.log([_row(source="backfill_text", text="回填的原文")])
    assert store.decision.log([_row(text="今天真跑起来了")]) == 1
    r = store.decision.load_day(D1)[0]
    assert r["source"] == "live"
    assert r["reason_text"] == "今天真跑起来了"


def test_backfill_exact_can_overwrite_backfill_text(store):
    """回填精度升级 (只抄到文字 → 翻出了结构化明细) 允许覆盖。"""
    store.decision.log([_row(source="backfill_text", text="只能照抄的原文")])
    assert store.decision.log([_row(source="backfill_exact", text="翻出了明细")]) == 1
    assert store.decision.load_day(D1)[0]["source"] == "backfill_exact"


def test_overwrite_live_true_forces_through(store):
    """`overwrite_live=True` 是回填工具「修数据」的后门, 强制覆盖。"""
    store.decision.log([_row(text="当场原文")])
    assert store.decision.log([_row(text="人工修正", source="backfill_text")],
                              overwrite_live=True) == 1
    assert store.decision.load_day(D1)[0]["reason_text"] == "人工修正"


def test_stronger_action_arriving_later_also_keeps_the_weaker_one(store):
    """**先失败、后成交** —— 真实顺序几乎总是这样, 更弱的那次也必须留住。

    2026-09-18 修正的真缺陷: 原来只有"先强后弱"的顺序才会把弱动作追加进 events,
    而现实中上午"想卖没卖成"(可用为 0 / 跌停)、下午真卖成了才是常态。结果是
    失败信息被整个丢掉, 页面上看起来一路顺利 —— 而那次失败恰恰是用户最需要
    知道的那件事 (要不要人工处理)。
    """
    store.decision.log([_row(action="FAIL", code="EXIT_ARM_FAIL",
                             text="跌停无法成交, 卖不掉")])
    store.decision.log([_row(action="SELL", code="EXIT_TRIGGERED",
                             text="卖出 600519.SH：移动止盈")])
    r = store.decision.load_day(D1)[0]
    assert r["action"] == "SELL"                       # 主结论 = 卖成了
    assert r["reason_code"] == "EXIT_TRIGGERED"
    events = r["evidence"]["events"]
    assert [e["reason_code"] for e in events] == ["EXIT_ARM_FAIL"]
    assert events[0]["reason_text"] == "跌停无法成交, 卖不掉"
    assert events[0]["action"] == "FAIL"


def test_both_orders_keep_two_events(store):
    """三件事都发生 (失败 → 成交 → 又来一条没动) → 两条弱事件都留住。"""
    store.decision.log([_row(action="FAIL", code="EXIT_ARM_FAIL", text="第一次失败")])
    store.decision.log([_row(action="SELL", code="EXIT_TRIGGERED", text="成交了")])
    store.decision.log([_row(action="HOLD", code="EXIT_NO_TRIGGER", text="另一条腿没动")])
    r = store.decision.load_day(D1)[0]
    assert r["action"] == "SELL"
    codes = [e["reason_code"] for e in r["evidence"]["events"]]
    assert sorted(codes) == ["EXIT_ARM_FAIL", "EXIT_NO_TRIGGER"]


def test_event_keeps_its_own_timestamp(store):
    """每条事件带自己的时间戳 —— 页面据此还原"先发生了什么、后发生了什么"。
    """
    store.decision.log([_row(action="FAIL", code="EXIT_ARM_FAIL", text="先失败")])
    store.decision.log([_row(action="SELL", code="EXIT_TRIGGERED", text="后成交")])
    ev = store.decision.load_day(D1)[0]["evidence"]["events"][0]
    assert ev["ts"] > 0


def test_equal_rank_identical_text_adds_no_event(store):
    """同强度 + **原话一模一样** → 只覆盖, 不往 events 里塞。

    监控每 10 秒重试一次, 多数时候说的是同一句话; 全留就是几百条噪音。
    """
    store.decision.log([_row(action="SELL", code="EXIT_TRIGGERED", text="卖出成功")])
    store.decision.log([_row(action="SELL", code="EXIT_TRIGGERED", text="卖出成功")])
    r = store.decision.load_day(D1)[0]
    assert r["reason_text"] == "卖出成功"
    assert "events" not in r["evidence"]


def test_equal_rank_different_text_keeps_the_trail(store):
    """同强度但**原话不一样** → 前一次要留进 events。

    这条是被真实数据钓出来的 (2026-09-18): 同强度重复时原来是整份覆盖 evidence,
    那份新 evidence 手里没有 events, 于是之前攒的轨迹被清空一次 —— 结果"留下几条"
    完全取决于写入顺序。先成交后失败, 14 条失败全留下; 先失败后成交, 只剩 1 条。
    真实影响: 2026 年 8 月 4 日 002039.SZ 那天有 12 种不同的失败文案, 台账里只留了
    1 条。**两种顺序都必须留下同样多**。
    """
    store.decision.log([_row(action="FAIL", code="EXIT_ARM_FAIL", text="重试 现价 2.61")])
    store.decision.log([_row(action="FAIL", code="EXIT_ARM_FAIL", text="重试 现价 2.60")])
    store.decision.log([_row(action="FAIL", code="EXIT_ARM_FAIL", text="重试 现价 2.59")])
    r = store.decision.load_day(D1)[0]
    assert r["reason_text"] == "重试 现价 2.59"          # 主结论 = 最新那次
    texts = [e["reason_text"] for e in r["evidence"]["events"]]
    assert texts == ["重试 现价 2.61", "重试 现价 2.60"]  # 轨迹一条不丢


def test_failed_first_then_filled_keeps_the_same_trail(store, tmp_path):
    """**顺序无关**: 先失败后成交 与 先成交后失败, 攒下的 events 条数必须一样。

    这是上一条的"对拍"版本 —— 用两个库跑两种顺序, 数量对齐才算真的修好了。
    """
    texts = ["重试 现价 2.61", "重试 现价 2.60", "重试 现价 2.59"]

    def _count(order, tag):
        s = TradeStore(tmp_path / f"{tag}.db", tmp_path / f"{tag}.jsonl")
        try:
            fails = [_row(action="FAIL", code="EXIT_ARM_FAIL", text=t) for t in texts]
            sell = _row(action="SELL", code="EXIT_TRIGGERED", text="成交了")
            for row in (fails + [sell]) if order == "fail_first" else ([sell] + fails):
                s.decision.log([row])
            return len(s.decision.load_day(D1)[0]["evidence"]["events"])
        finally:
            s.close()

    assert _count("fail_first", "a") == _count("sell_first", "b") == len(texts)


def test_events_are_capped(store):
    """明细里的事件条数封顶 —— 实时监控一天能试上千次, 不能让它无限涨。

    到顶后保住的必须是**最早那批**: 故事的开头 ("什么时候开始卖不掉的")
    比中间第 500 次重试有用得多。
    """
    from trade.store import _EVENTS_MAX
    total = _EVENTS_MAX + 20
    for i in range(total):
        store.decision.log([_row(action="FAIL", code="EXIT_ARM_FAIL",
                                 text=f"重试 {i}")])
    r = store.decision.load_day(D1)[0]
    events = r["evidence"]["events"]
    # 最新那条升成主结论 (不在 events 里), 所以 events 里正好是前 _EVENTS_MAX 条
    assert r["reason_text"] == f"重试 {total - 1}"
    assert len(events) == _EVENTS_MAX
    assert events[0]["reason_text"] == "重试 0"
    assert events[-1]["reason_text"] == f"重试 {_EVENTS_MAX - 1}"


def test_rerunning_the_same_rows_does_not_grow_events(store):
    """同一批行再送一遍 (定时任务重跑) → events **不会**翻倍。

    重跑时"上一次的主结论"会再被当作"更弱的那次"塞一遍, 所以条目数会在
    第一遍的基础上多几条; 关键是**同一句话不会存两份** (靠原话去重),
    因此反复重跑也收敛, 不会像滚雪球一样涨。
    """
    for t in ("重试 A", "重试 B"):
        store.decision.log([_row(action="FAIL", code="EXIT_ARM_FAIL", text=t)])
    first = [e["reason_text"] for e in store.decision.load_day(D1)[0]["evidence"]["events"]]
    for t in ("重试 A", "重试 B"):
        store.decision.log([_row(action="FAIL", code="EXIT_ARM_FAIL", text=t)])
    second = [e["reason_text"] for e in store.decision.load_day(D1)[0]["evidence"]["events"]]
    assert len(second) == len(set(second))       # 没有重复条目
    assert set(second) == set(first) | {"重试 B"}  # 收敛到"出现过的原话"集合
    assert len(second) <= 2


def test_stronger_overwrite_keeps_new_evidence_numbers(store):
    """换成更强动作时, 新行自己的"凭什么"数字必须带过来 (别把新的丢了)。"""
    store.decision.log([_row(action="FAIL", code="EXIT_ARM_FAIL",
                             evidence={"code": "x", "audit_message": "失败原文"})])
    store.decision.log([_row(action="SELL", code="EXIT_TRIGGERED",
                             evidence={"code": "x", "price": 8.5,
                                       "pnl_amount": -1500.0})])
    ev = store.decision.load_day(D1)[0]["evidence"]
    assert ev["price"] == 8.5
    assert ev["pnl_amount"] == -1500.0
    assert "audit_message" not in ev            # 旧的正文进 events, 不占主位
    assert ev["events"][0]["reason_text"]


def test_blocked_write_does_not_append_event(store):
    """被可信度拦下的写入必须**完全无副作用** —— 不能顺手往 events 里塞一条。"""
    store.decision.log([_row(code="ROT_SWITCH", action="BUY", text="当场")])
    store.decision.log([_row(code="ROT_HOLD_KEEP", action="HOLD",
                             source="backfill_text")])
    assert "events" not in store.decision.load_day(D1)[0]["evidence"]


# ── 字段兜底 ────────────────────────────────────────────────────

def test_action_defaults_from_reason_code(store):
    """没传 action 时按**原因码的默认动作**落库, 不是拍脑袋写 HOLD。

    这是 ``log()`` 文档里写明的契约。默认成 HOLD 的坑很隐蔽: 将来哪个写入点忘了
    传 ``action``, 一笔真卖出 (EXIT_TRIGGERED 默认 SELL) 会被记成"没动", 页面上
    直接看错, 而且不报错。下面逐类各验一个, 避免只对某一种动作碰巧成立。
    """
    store.decision.log([
        _row(subject="卖", code="ROT_TRAILING_STOP"),   # 默认 SELL
        _row(subject="买", code="ROT_SWITCH"),          # 默认 BUY
        _row(subject="错", code="EXIT_ARM_FAIL"),       # 默认 FAIL
        _row(subject="告", code="LADDER_PLACED"),       # 默认 INFO
        _row(subject="没", code="EXIT_NO_TRIGGER"),     # 默认 HOLD
    ])
    got = {r["subject"]: r["action"] for r in store.decision.load_day(D1)}
    assert got == {"卖": "SELL", "买": "BUY", "错": "FAIL", "告": "INFO", "没": "HOLD"}


def test_unknown_reason_code_without_action_falls_back_to_hold(store):
    """原因码也不认识 + 没传 action → HOLD 兜底, 不抛异常 (页面少点描述可以,
    整批台账写不进去不行)。"""
    assert store.decision.log([_row(code="FUTURE_CODE")]) == 1
    assert store.decision.load_day(D1)[0]["action"] == "HOLD"


def test_explicit_action_overrides_reason_code(store):
    """显式传的 action 优先于原因码默认值 (写入点可以覆盖)。"""
    store.decision.log([_row(code="ROT_HOLD_KEEP", action="SELL")])
    assert store.decision.load_day(D1)[0]["action"] == "SELL"


def test_trade_ids_list_is_joined(store):
    """trade_ids 传列表自动逗号连接 (写入点不必自己拼字符串)。"""
    store.decision.log([_row(trade_ids=["t1", "t2", "t3"])])
    assert store.decision.load_day(D1)[0]["trade_ids"] == "t1,t2,t3"


def test_trade_ids_string_kept(store):
    assert store.decision.log([_row(trade_ids="o-9")]) == 1
    assert store.decision.load_day(D1)[0]["trade_ids"] == "o-9"


def test_evidence_broken_json_reads_as_empty_dict(store):
    """明细 JSON 坏了 → 给空字典, **不让页面因此打不开** (fail-soft)。
    这条是页面可用性的底线: 一行坏数据不该让整张卡白屏。"""
    store.decision.log([_row(evidence={"a": 1})])
    store._conn.execute("UPDATE daily_decision SET evidence_json='不是JSON' "
                        "WHERE trade_date=?", (D1,))
    store._conn.commit()
    assert store.decision.load_day(D1)[0]["evidence"] == {}


def test_evidence_non_dict_json_reads_as_empty_dict(store):
    """明细是个 JSON 数组/数字 (不是字典) → 同样给空字典, 不给前端埋雷。"""
    store.decision.log([_row()])
    store._conn.execute("UPDATE daily_decision SET evidence_json='[1,2]' "
                        "WHERE trade_date=?", (D1,))
    store._conn.commit()
    assert store.decision.load_day(D1)[0]["evidence"] == {}


def test_evidence_is_json_serializable_objects(store):
    """detail 里混进 datetime / Decimal 这类不可直接序列化的值也不能炸
    (`default=str` 兜底), 否则整批台账静默丢失。"""
    import datetime as _dt
    assert store.decision.log([_row(evidence={"when": _dt.datetime(2026, 9, 16)})]) == 1
    assert isinstance(store.decision.load_day(D1)[0]["evidence"]["when"], str)


# ── 读接口 ──────────────────────────────────────────────────────

def test_load_day_only_that_day(store):
    store.decision.log([_row(date=D1), _row(date=D2)])
    assert [r["trade_date"] for r in store.decision.load_day(D1)] == [D1]


def test_load_day_sorted_by_strategy_then_subject(store):
    """同一天内的顺序固定 (策略名 → 对象), 页面分组显示不抖。"""
    store.decision.log([
        _row(strategy="rotation", subject="份2"),
        _row(strategy="auto_buy", subject="pool"),
        _row(strategy="rotation", subject="份1"),
    ])
    assert [(r["strategy"], r["subject"])
            for r in store.decision.load_day(D1)] == [
        ("auto_buy", "pool"), ("rotation", "份1"), ("rotation", "份2")]


def test_load_range_inclusive_both_ends(store):
    """区间**含两端** —— 决策日历按月取数依赖这条, 少一天就少一格。"""
    store.decision.log([_row(date=D1), _row(date=D2)])
    got = [r["trade_date"] for r in store.decision.load_range(D1, D2)]
    assert got == [D1, D2]


def test_load_range_sorted_by_date(store):
    store.decision.log([_row(date=D2), _row(date=D1)])
    got = [r["trade_date"] for r in store.decision.load_range(D1, D2)]
    assert got == sorted(got)


def test_load_range_empty_window(store):
    """区间内没有任何行 → 空列表 (不是异常), 日历据此标「没运行」。"""
    store.decision.log([_row(date=D1)])
    assert store.decision.load_range("2026-09-01", "2026-09-10") == []


def test_load_with_external_readonly_connection(store, tmp_path):
    """传外部连接时走那条连接 (Web 端点在 WAL 下用只读连接, 与写连接不互堵)。"""
    import sqlite3
    store.decision.log([_row()])
    ro = sqlite3.connect(f"file:{store._db_path}?mode=ro", uri=True)
    try:
        assert len(store.decision.load_day(D1, conn=ro)) == 1
        assert len(store.decision.load_range(D1, D1, conn=ro)) == 1
    finally:
        ro.close()


# ── 出错吞掉 + 审计 (台账失败绝不能影响交易) ────────────────────

def test_failure_is_swallowed_and_audited(store, monkeypatch):
    """写台账炸了: ①不往上抛 (绝不影响交易); ②最多留一条 decision_log_fail 审计。

    用 monkeypatch 让单行写入抛异常, 连接保持完好 —— 这样正好能验到「审计补写」
    那一段 (真把连接关掉的话审计也写不进去, 就测不到这条路径了)。
    """
    def boom(*a, **kw):
        raise RuntimeError("模拟台账写失败")

    monkeypatch.setattr(store.decision, "_upsert_one", boom)
    monkeypatch.setattr(store_mod.time, "sleep", lambda *_: None)  # 免掉 0.1s 重试等待
    assert store.decision.log([_row()]) == 0          # ① 不抛异常, 返回 0
    audits = store._conn.execute(
        "SELECT kind, message FROM audit WHERE kind='decision_log_fail'").fetchall()
    assert len(audits) == 1                            # ② 审计留痕
    assert "模拟台账写失败" in audits[0][1]


def test_failure_retries_once_before_giving_up(store, monkeypatch):
    """重试一次才对 —— WAL 写偶尔被 SQLite busy 挡一下, 第二次就成功了。
    这条锁住「重试」这个行为本身: 去掉重试会红。"""
    calls = {"n": 0}
    real = store.decision._upsert_one

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("第一次 busy")
        return real(*a, **kw)

    monkeypatch.setattr(store.decision, "_upsert_one", flaky)
    monkeypatch.setattr(store_mod.time, "sleep", lambda *_: None)
    assert store.decision.log([_row()]) == 1
    assert calls["n"] == 2
    assert len(store.decision.load_day(D1)) == 1


def test_failure_never_raises_to_caller(store, monkeypatch):
    """连重试都失败也要返回 0 而不是抛 —— 调用方是交易线程, 不能被台账拖死。"""
    monkeypatch.setattr(store.decision, "_upsert_one",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("一直坏")))
    monkeypatch.setattr(store_mod.time, "sleep", lambda *_: None)
    assert store.decision.log([_row()]) == 0


# ── 端到端: 一天一行的样子 ──────────────────────────────────────

def test_full_day_four_strategies(store):
    """一天跑满四条策略 → 四行, 每条策略各回答自己的问题。"""
    store.decision.log([
        _row(strategy="rotation", subject="份1", code="ROT_SWITCH",
             action="BUY", trade_ids=["t1"]),
        _row(strategy="auto_buy", subject="pool", code="PICK_BUY",
             action="BUY", evidence={"selected": 3, "bought": 1}),
        _row(strategy="exit", subject="600519.SH", code="EXIT_NO_TRIGGER"),
        _row(strategy="ladder", subject="600519.SH", code="LADDER_PLACED"),
    ])
    rows = store.decision.load_day(D1)
    assert {(r["strategy"], r["subject"]) for r in rows} == {
        ("rotation", "份1"), ("auto_buy", "pool"),
        ("exit", "600519.SH"), ("ladder", "600519.SH")}
    assert len(rows) == 4


def test_updated_ts_is_written_and_refreshed(store):
    """一次写入要有时间戳; 覆盖后时间戳要往前走 (页面据此判最后更新时间)。"""
    store.decision.log([_row()])
    t1 = store.decision.load_day(D1)[0]["updated_ts"]
    assert t1 > 0
    time.sleep(0.01)
    store.decision.log([_row(text="改了一下")])
    assert store.decision.load_day(D1)[0]["updated_ts"] >= t1
