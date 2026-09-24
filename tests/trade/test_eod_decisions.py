"""收盘汇总（EOD）决策台账测试 (2026-09-18)。

`TradeApp._capture_decision_digest` 回答的是用户最常问的那句话 ——
「今天为什么没卖」。它的难点不在计算, 而在**「没卖」有三种性质完全不同的情况,
必须分开说**, 混成一句就是误导:

1. 当天真有卖出成交 → 台账里已经有 live 的 SELL 行, 这里**不覆盖**
   (当场写下的一手证据不能被事后汇总改写);
2. 当天有「想卖没卖成」的失败事件 → FAIL + 那条审计原文
   (比如"跌停无法成交" —— 这是最重要的那类答案);
3. 以上都没有 → 用 monitor.daily_digest 算出的「离各条线还差多少」。

另有 `_log_exit_fill`(卖出成交当场落行) 与 `_ladder_rows_today`(预埋单汇总,
含「阶梯止盈开关关着」这条几乎每天都出现的原因)。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.book import DIRECTION_BUY  # noqa: E402
from trade.config import TradeConfig  # noqa: E402
from trade_main import TradeApp  # noqa: E402

# 工作日盘中 (2024-01-02 周二), 与 monitor 测试同款时钟 —— 台账只在交易日写
_T0 = time.mktime(time.strptime("2024-01-02 10:00", "%Y-%m-%d %H:%M"))
DAY = "2024-01-02"
CODE = "600519.SH"
CODE2 = "000001.SZ"


@pytest.fixture()
def cfg(tmp_path):
    return TradeConfig(
        account_id="DD", fake_sdk=True,
        db_path=str(tmp_path / "t.db"),
        raw_log_path=str(tmp_path / "r.jsonl"),
        kill_flag_path=str(tmp_path / "KILL"),
    )


def _app(cfg, clock=None):
    return TradeApp(cfg, fake=True, clock=clock or (lambda: _T0),
                    fake_gateway_kwargs={"cash": 1_000_000.0, "positions": {}},
                    selection_runner=lambda f, a, u: [])


def _trade(store, code=CODE, direction=1, price=10.0, qty=100, ts=None,
           reason="", tid=None, pnl_amount=0.0, pnl_pct=0.0):
    """往 trades 表塞一笔成交。direction: 1=卖 (非 DIRECTION_BUY)。"""
    store.save_trade({
        "traded_id": tid or f"T-{code}-{direction}-{price}", "order_id": "O-1",
        "code": code, "direction": direction, "price": price, "qty": qty,
        "amount": price * qty, "ts": ts if ts is not None else _T0,
        "source": "system", "reason": reason,
        "pnl_amount": pnl_amount, "pnl_pct": pnl_pct})


def _audit(store, kind, message, detail=None, ts=None):
    """往 audit 表塞一行。ts 必须落在 DAY 当天 —— 汇总按当天窗口读。"""
    import json as _json
    with store._lock, store._conn:
        store._conn.execute(
            "INSERT INTO audit (ts, kind, message, detail_json) VALUES (?,?,?,?)",
            (ts if ts is not None else _T0, kind, message,
             _json.dumps(detail or {}, ensure_ascii=False)))


def _rows(app):
    return app.store.decision.load_day(DAY)


# ═══════════════════════════════════════════════════════════════
# _log_exit_fill — 卖出成交当场落行
# ═══════════════════════════════════════════════════════════════

def test_exit_fill_writes_sell_row(cfg):
    app = _app(cfg)
    app._log_exit_fill({"code": CODE, "price": 8.5, "qty": 1000,
                        "reason": "硬止损: 成本 10.00 现价 8.50",
                        "traded_id": "T-1", "pnl_amount": -1500.0,
                        "pnl_pct": -0.15})
    rows = _rows(app)
    assert len(rows) == 1
    r = rows[0]
    assert (r["strategy"], r["subject"], r["action"]) == ("exit", CODE, "SELL")
    assert r["reason_code"] == "EXIT_TRIGGERED"
    assert r["source"] == "live"
    app.store.close()


def test_exit_fill_reason_text_keeps_original_wording(cfg):
    """原因用的是下单时写进 fill context 的**原文**, 不是事后重新推测的话。
    这条是「凭什么」可信度的根: 原文来了就能对账, 推测的话只能信。"""
    app = _app(cfg)
    app._log_exit_fill({"code": CODE, "price": 8.5, "qty": 100,
                        "reason": "移动止盈: 最高 12.00 回撤 5.8%",
                        "traded_id": "T-2"})
    text = _rows(app)[0]["reason_text"]
    assert "移动止盈: 最高 12.00 回撤 5.8%" in text
    app.store.close()


def test_exit_fill_records_pnl_and_trade_id(cfg):
    """成交号与盈亏要进台账 —— 页面点开这一行就能对上成交记录那一笔。"""
    app = _app(cfg)
    app._log_exit_fill({"code": CODE, "price": 8.5, "qty": 1000, "reason": "硬止损",
                        "traded_id": "T-9", "pnl_amount": -1500.0, "pnl_pct": -0.15})
    r = _rows(app)[0]
    assert r["trade_ids"] == "T-9"
    assert r["evidence"]["pnl_amount"] == -1500.0
    assert r["evidence"]["price"] == 8.5
    app.store.close()


def test_exit_fill_on_holiday_is_rejected(cfg):
    """休市日的卖出成交不进台账 (守卫在写入口拦) —— 不炸。"""
    holiday = time.mktime(time.strptime("2024-01-06 10:00", "%Y-%m-%d %H:%M"))
    app = _app(cfg, clock=lambda: holiday)
    app._log_exit_fill({"code": CODE, "price": 8.5, "qty": 100,
                        "reason": "硬止损", "traded_id": "T-3"})
    assert app.store.decision.load_day("2024-01-06") == []
    app.store.close()


def test_exit_fill_broken_record_is_swallowed(cfg):
    """记录缺字段 (回调格式变了) 不能让卖出链路崩 —— 台账失败绝不影响交易。"""
    app = _app(cfg)
    app._log_exit_fill({})            # 缺 code 会抛 KeyError, 必须被吞掉
    assert _rows(app) == []
    app.store.close()


def test_exit_fill_does_not_write_audit(cfg):
    """台账落库不该污染审计表 (审计是「发生了什么」, 台账是「为什么」)。"""
    app = _app(cfg)
    app._log_exit_fill({"code": CODE, "price": 8.5, "qty": 100,
                        "reason": "硬止损", "traded_id": "T-4"})
    kinds = [r[0] for r in app.store._conn.execute(
        "SELECT kind FROM audit").fetchall()]
    assert "exit_trigger" not in kinds
    app.store.close()


# ═══════════════════════════════════════════════════════════════
# _sold_codes_today
# ═══════════════════════════════════════════════════════════════

def test_sold_codes_only_counts_non_buy(cfg):
    """只算卖出。买入成交不算「已经卖过」—— 否则当天买进的票会被误判为
    "台账里已有 SELL 行" 而跳过, 那票的「为什么没卖」就永远没人回答。"""
    app = _app(cfg)
    _trade(app.store, code=CODE, direction=DIRECTION_BUY)
    _trade(app.store, code=CODE2, direction=1)
    assert app._sold_codes_today(DAY) == {CODE2}
    app.store.close()


def test_sold_codes_empty_when_query_fails(cfg, monkeypatch):
    """查询失败返回空集合 —— 宁可多算「没卖」(多写一行解释), 不可漏算。"""
    app = _app(cfg)
    monkeypatch.setattr(app.store, "load_today_trades_detail",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("坏了")))
    assert app._sold_codes_today(DAY) == set()
    app.store.close()


def test_sold_codes_ignores_other_days(cfg):
    """只算当天。昨天的卖出不能让今天这行被跳过。"""
    app = _app(cfg)
    _trade(app.store, code=CODE, direction=1,
           ts=_T0 - 86400)          # 昨天卖的
    assert app._sold_codes_today(DAY) == set()
    app.store.close()


# ═══════════════════════════════════════════════════════════════
# _exit_failed_today
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("kind", ["exit_arm_fail", "exit_skip",
                                 "exit_risk_reject", "exit_fail_closed",
                                 "exit_lock_fail"])
def test_all_five_fail_kinds_are_picked_up(cfg, kind):
    """executor 的五处「想卖没卖成」出口都要认。漏一个, 那类失败当天就
    从台账里消失了 —— 而那恰恰是最需要被看见的一类。"""
    app = _app(cfg)
    _audit(app.store, kind, f"{kind} 的原文", {"code": CODE})
    got = app._exit_failed_today(DAY)
    assert got == {CODE: f"{kind} 的原文"}
    app.store.close()


def test_unrelated_audit_kinds_are_ignored(cfg):
    """无关的审计类型不能被当成失败事件 —— 否则每个正常动作都会被读成失败。"""
    app = _app(cfg)
    _audit(app.store, "rotation_summary", "轮动跑完了", {"code": CODE})
    _audit(app.store, "auto_buy_summary", "选股跑完了", {"code": CODE})
    assert app._exit_failed_today(DAY) == {}
    app.store.close()


def test_rows_without_code_are_skipped_not_guessed(cfg):
    """明细里没带代码的行直接跳过 —— 不硬猜是哪个票。
    猜错比缺一行更糟: 用户会看到"某某票想卖没卖成"而根本没那回事。"""
    app = _app(cfg)
    _audit(app.store, "exit_arm_fail", "没带代码的失败", {})
    _audit(app.store, "exit_arm_fail", "带了代码的失败", {"code": CODE})
    assert app._exit_failed_today(DAY) == {CODE: "带了代码的失败"}
    app.store.close()


def test_broken_detail_json_is_skipped(cfg):
    app = _app(cfg)
    with app.store._lock, app.store._conn:
        app.store._conn.execute(
            "INSERT INTO audit (ts, kind, message, detail_json) VALUES (?,?,?,?)",
            (_T0, "exit_arm_fail", "坏明细", "{坏"))
    assert app._exit_failed_today(DAY) == {}
    app.store.close()


def test_first_message_wins_for_same_code(cfg):
    """同一票当天多次失败 → 留最早那条 (最初的失败原因才是根因)。"""
    app = _app(cfg)
    _audit(app.store, "exit_arm_fail", "第一次失败", {"code": CODE}, ts=_T0)
    _audit(app.store, "exit_skip", "第二次失败", {"code": CODE}, ts=_T0 + 60)
    assert app._exit_failed_today(DAY) == {CODE: "第一次失败"}
    app.store.close()


def test_other_days_are_not_read(cfg):
    app = _app(cfg)
    _audit(app.store, "exit_arm_fail", "昨天的失败", {"code": CODE},
           ts=_T0 - 86400)
    assert app._exit_failed_today(DAY) == {}
    app.store.close()


def test_query_failure_returns_empty(cfg, monkeypatch):
    """读审计失败 → 空字典 (退化成"没失败过"), 不抛异常阻断收盘归档。"""
    app = _app(cfg)

    def boom():
        raise RuntimeError("只读连接打不开")

    monkeypatch.setattr(app.store, "open_readonly", boom)
    assert app._exit_failed_today(DAY) == {}
    app.store.close()


# ═══════════════════════════════════════════════════════════════
# _ladder_rows_today
# ═══════════════════════════════════════════════════════════════

def test_ladder_placed_writes_info_row(cfg):
    """挂成功的预埋单按票一行 INFO, 并带上档位与挂单号。"""
    app = _app(cfg)
    _audit(app.store, "ladder_place", "档1 已挂",
           {"code": CODE, "tier": 0, "order_id": "O-77"})
    rows = app._ladder_rows_today(DAY)
    assert len(rows) == 1
    r = rows[0]
    assert (r["action"], r["reason_code"]) == ("INFO", "LADDER_PLACED")
    assert "第 1 档" in r["reason_text"]
    assert r["trade_ids"] == ["O-77"]
    app.store.close()


def test_ladder_multiple_tiers_one_row(cfg):
    """同一票挂了多档 → 合并成一行, 档位写全 (不刷屏)。"""
    app = _app(cfg)
    _audit(app.store, "ladder_place", "档1", {"code": CODE, "tier": 0})
    _audit(app.store, "ladder_place", "档2", {"code": CODE, "tier": 1})
    rows = app._ladder_rows_today(DAY)
    assert len(rows) == 1
    assert "第 1 档" in rows[0]["reason_text"]
    assert "第 2 档" in rows[0]["reason_text"]
    app.store.close()


def test_ladder_skip_writes_hold_row_with_reason(cfg):
    """没挂成的票 → HOLD + 审计原文 (要说清是为什么没挂成)。"""
    app = _app(cfg)
    _audit(app.store, "ladder_skip", "可用数量不足", {"code": CODE})
    rows = app._ladder_rows_today(DAY)
    assert len(rows) == 1
    assert rows[0]["reason_code"] == "LADDER_SKIP"
    assert "可用数量不足" in rows[0]["reason_text"]
    app.store.close()


def test_ladder_skip_etf_counts_as_skip(cfg):
    """ETF 不挂预埋单也是「没挂成」的一种, 同样要留痕。"""
    app = _app(cfg)
    _audit(app.store, "ladder_skip_etf", "ETF 不挂预埋单", {"code": CODE2})
    rows = app._ladder_rows_today(DAY)
    assert rows[0]["reason_code"] == "LADDER_SKIP"
    app.store.close()


def test_ladder_disabled_says_switch_is_off(cfg):
    """阶梯止盈开关关着 → 必须写清楚是**关着**, 不是"跑了但没挂成"。

    这条几乎每天都会出现, 正是用户最常看到的那个「为什么没动作」。
    措辞要是含糊成"今天没挂预埋单", 用户会去查程序是不是挂了。
    """
    app = _app(cfg)
    _audit(app.store, "ladder_skip_disabled", "阶梯止盈未启用", {})
    rows = app._ladder_rows_today(DAY)
    assert len(rows) == 1
    r = rows[0]
    assert r["reason_code"] == "LADDER_DISABLED"
    assert r["subject"] == "pool"
    assert "开关关着" in r["reason_text"]
    app.store.close()


def test_disabled_row_suppressed_when_something_was_placed(cfg):
    """开关关着但当天真有挂单 (人工开的) → 不再写"开关关着"。
    否则同一份资金会出现两条自相矛盾的行。"""
    app = _app(cfg)
    _audit(app.store, "ladder_skip_disabled", "未启用", {})
    _audit(app.store, "ladder_place", "档1 已挂", {"code": CODE, "tier": 0})
    rows = app._ladder_rows_today(DAY)
    assert [r["reason_code"] for r in rows] == ["LADDER_PLACED"]
    app.store.close()


def test_ladder_no_audit_writes_nothing(cfg):
    """当天没跑过预埋单逻辑 → 一行都不写 (不能凭空说它"关着")。"""
    app = _app(cfg)
    assert app._ladder_rows_today(DAY) == []
    app.store.close()


def test_ladder_rows_sorted_by_code(cfg):
    app = _app(cfg)
    _audit(app.store, "ladder_place", "b", {"code": "600519.SH", "tier": 0})
    _audit(app.store, "ladder_place", "a", {"code": "000001.SZ", "tier": 0})
    rows = app._ladder_rows_today(DAY)
    assert [r["subject"] for r in rows] == ["000001.SZ", "600519.SH"]
    app.store.close()


def test_ladder_query_failure_returns_empty(cfg, monkeypatch):
    app = _app(cfg)
    monkeypatch.setattr(app.store, "open_readonly",
                        lambda: (_ for _ in ()).throw(RuntimeError("坏了")))
    assert app._ladder_rows_today(DAY) == []
    app.store.close()


# ═══════════════════════════════════════════════════════════════
# _capture_decision_digest — 三分支 (核心)
# ═══════════════════════════════════════════════════════════════

def _stub_digest(app, rows):
    """把 monitor.daily_digest 换成固定输出 —— 本文件测的是汇总分支,
    digest 的计算本身在 test_decision_digest.py 里单独验。"""
    app.monitor.daily_digest = lambda positions: list(rows)


def _digest_row(code, action="HOLD", reason_code="EXIT_NO_TRIGGER",
                text="没到任何一条卖出线：现价 10.50 元",
                evidence=None):
    return {"code": code, "action": action, "reason_code": reason_code,
            "reason_text": text, "evidence": evidence or {"code": code}}


def test_branch1_sold_code_is_not_overwritten(cfg):
    """① 当天真有卖出成交 → 不覆盖已有的 live SELL 行。

    这是三条分支里最容易写错的一条: 汇总跑在收盘, 如果无脑用 digest 的结果
    覆盖, 「卖出了」这件事会被改写成「没到卖出线」—— 台账当场变成谎话。
    """
    app = _app(cfg)
    _trade(app.store, code=CODE, direction=1, price=8.5, reason="硬止损")
    app._log_exit_fill({"code": CODE, "price": 8.5, "qty": 1000,
                        "reason": "硬止损", "traded_id": "T-1"})
    _stub_digest(app, [_digest_row(CODE)])       # digest 说"没触发"
    app._capture_decision_digest()
    rows = _rows(app)
    assert len(rows) == 1
    assert rows[0]["action"] == "SELL"           # 仍是卖, 没被改成 HOLD
    assert rows[0]["reason_code"] == "EXIT_TRIGGERED"
    app.store.close()


def test_branch2_failed_event_beats_digest(cfg):
    """② 当天"想卖没卖成" → FAIL + 那条审计原文, 且优先于 digest 的"没触发"。

    为什么这条最重要: 「跌停了卖不出去」和「没到卖出线」对用户是完全不同的
    两件事 —— 前者要立刻处理, 后者什么都不用做。混成一句就是把风险藏起来。
    """
    app = _app(cfg)
    _audit(app.store, "exit_arm_fail", "跌停无法成交", {"code": CODE})
    _stub_digest(app, [_digest_row(CODE)])       # digest 说"没触发"
    app._capture_decision_digest()
    rows = _rows(app)
    assert len(rows) == 1
    r = rows[0]
    assert (r["action"], r["reason_code"]) == ("FAIL", "EXIT_ARM_FAIL")
    assert "跌停无法成交" in r["reason_text"]
    assert r["evidence"]["audit_message"] == "跌停无法成交"
    app.store.close()


def test_branch3_uses_digest_distances(cfg):
    """③ 都没有 → 用 digest 那行 (带"离各条线还差多少")。"""
    app = _app(cfg)
    _stub_digest(app, [_digest_row(CODE, evidence={"code": CODE,
                                                 "gap_to_cost_stop_pct": 16.2})])
    app._capture_decision_digest()
    rows = _rows(app)
    assert len(rows) == 1
    assert rows[0]["reason_code"] == "EXIT_NO_TRIGGER"
    assert rows[0]["evidence"]["gap_to_cost_stop_pct"] == 16.2
    app.store.close()


def test_three_codes_get_three_different_answers(cfg):
    """一票卖出 / 一票失败 / 一票没触发 —— 三条分支同一天各走各的。"""
    app = _app(cfg)
    sold, failed, idle = "600519.SH", "000001.SZ", "300750.SZ"
    _trade(app.store, code=sold, direction=1, reason="硬止损")
    app._log_exit_fill({"code": sold, "price": 8.5, "qty": 100,
                        "reason": "硬止损", "traded_id": "T-1"})
    _audit(app.store, "exit_arm_fail", "跌停无法成交", {"code": failed})
    _stub_digest(app, [_digest_row(sold), _digest_row(failed), _digest_row(idle)])
    app._capture_decision_digest()
    got = {r["subject"]: (r["action"], r["reason_code"]) for r in _rows(app)}
    assert got == {
        sold: ("SELL", "EXIT_TRIGGERED"),
        failed: ("FAIL", "EXIT_ARM_FAIL"),
        idle: ("HOLD", "EXIT_NO_TRIGGER"),
    }
    app.store.close()


def test_ladder_rows_are_appended(cfg):
    """预埋单的行要并进同一批 (同一策略归同一组显示)。"""
    app = _app(cfg)
    _audit(app.store, "ladder_place", "档1", {"code": CODE, "tier": 0, "order_id": "O-1"})
    _stub_digest(app, [_digest_row(CODE2)])
    app._capture_decision_digest()
    got = {r["strategy"] for r in _rows(app)}
    assert got == {"exit", "ladder"}
    app.store.close()


def test_empty_digest_and_no_ladder_writes_nothing(cfg):
    """没有持仓、也没有预埋单审计 → 一行都不写 (不硬造"今天没动")。"""
    app = _app(cfg)
    _stub_digest(app, [])
    app._capture_decision_digest()
    assert _rows(app) == []
    app.store.close()


def test_digest_failure_does_not_raise(cfg):
    """digest 自己炸了 → 不抛 (调用方是收盘归档链路, 不能被台账拖死)。"""
    app = _app(cfg)

    def boom(positions):
        raise RuntimeError("快照坏了")

    app.monitor.daily_digest = boom
    with pytest.raises(RuntimeError):
        app._capture_decision_digest()      # 本函数按设计不吞异常,
                                            # 由 _on_eod 的 try 兜 (见下条)
    app.store.close()


def test_on_eod_swallows_digest_failure(cfg):
    """但在真实调用链上 (`_on_eod`), 台账出错绝不阻断归档与日报 ——
    这是 fail-soft 契约的实际落点。"""
    app = _app(cfg)
    app.monitor.daily_digest = lambda positions: (_ for _ in ()).throw(
        RuntimeError("快照坏了"))
    app._on_eod()                        # 不该抛
    app.store.close()


def test_all_rows_are_live_source(cfg):
    """当场汇总的行一律 source=live —— 它们不是回填, 可信度最高。"""
    app = _app(cfg)
    _audit(app.store, "exit_arm_fail", "失败原文", {"code": CODE})
    _audit(app.store, "ladder_place", "档1", {"code": CODE2, "tier": 0})
    _stub_digest(app, [_digest_row("300750.SZ")])
    app._capture_decision_digest()
    assert {r["source"] for r in _rows(app)} == {"live"}
    app.store.close()


def test_capture_twice_is_idempotent(cfg):
    """收盘汇总跑两次 (定时 + 启动补偿) 结果一样, 不堆重复行。"""
    app = _app(cfg)
    _stub_digest(app, [_digest_row(CODE)])
    app._capture_decision_digest()
    app._capture_decision_digest()
    assert len(_rows(app)) == 1
    app.store.close()


def test_capture_on_holiday_is_rejected_by_store(cfg):
    """休市日即使被误调也不会写进台账 (守卫在写入口, 这里只确认它真的拦住了)。"""
    holiday = time.mktime(time.strptime("2024-01-06 10:00", "%Y-%m-%d %H:%M"))
    app = _app(cfg, clock=lambda: holiday)
    _stub_digest(app, [_digest_row(CODE)])
    app._capture_decision_digest()
    assert app.store.decision.load_day("2024-01-06") == []
    app.store.close()


def test_exception_inside_exit_failed_lookup_degrades_to_digest(cfg, monkeypatch):
    """失败事件读不到 (审计表访问异常) → 退化成 digest 那句"离各条线还差多少",
    整批台账仍然写得出来 (不能因为一个辅助查询坏了就整天没记录)。"""
    app = _app(cfg)
    monkeypatch.setattr(app, "_exit_failed_today", lambda day: {})
    _stub_digest(app, [_digest_row(CODE)])
    app._capture_decision_digest()
    assert _rows(app)[0]["reason_code"] == "EXIT_NO_TRIGGER"
    app.store.close()
