"""决策台账两个只读端点的契约测试 (2026-09-18)。

`GET /api/trade/decisions` 与 `GET /api/trade/decisions/calendar` 是卡片一/卡片二的
唯一数据源。它们的契约里最容易被忽略、也最容易出错的是**「没有数据时说什么」**:

- 交易日但一行都没有 → 合成一行 `NO_RUN` 回答"为什么什么都没有"。这一行
  **只在读取侧合成、永不落库** —— 进程都没开机, 写入侧怎么可能写它;
- 而且"没运行"有四种性质完全不同的情况 (没痕迹 / 程序没开机 / 跑了但没留记录 /
  台账功能上线前), 混成一句"该日无成交"会把真故障藏起来;
- 休市日不出"没运行"的卡 —— 那是日历格标「休市」的事, 否则每个周末都报一次故障;
- 台账起点 (`2026-07-27`) 之前返回一句 note, 而不是让人对着空白页猜。

另有一条容易漏的: **空组也要返回**。前端靠组的存在显示「这条策略今天没有留痕」的
灰色占位, 组一消失用户就分不清"没跑"和"页面坏了"。
"""
import datetime as _dt
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from trade.api import create_api_app  # noqa: E402
from trade.config import TradeConfig  # noqa: E402
from trade.decision_api import GROUP_ORDER, STRATEGY_LABELS  # noqa: E402
from trade_main import TradeApp  # noqa: E402

# 两个真交易日 + 一个真休市日 (都用真历法, 不 mock 日历 —— 日历本身是被依赖的契约)
D1 = "2026-09-16"      # 周三
D2 = "2026-09-17"      # 周四
SAT = "2026-09-19"     # 周六
DAY8 = "20260916"      # D1 的 8 位写法

_T0 = time.mktime(time.strptime("2026-09-16 10:00", "%Y-%m-%d %H:%M"))


@pytest.fixture()
def ctx(tmp_path):
    cfg = TradeConfig(
        account_id="DEC", fake_sdk=True,
        db_path=str(tmp_path / "t.db"),
        raw_log_path=str(tmp_path / "r.jsonl"),
        kill_flag_path=str(tmp_path / "KILL"),
    )
    app = TradeApp(cfg, fake=True, clock=lambda: _T0,
                   fake_gateway_kwargs={"cash": 1_000_000.0, "positions": {}},
                   selection_runner=lambda f, a, u: [],
                   config_path=str(tmp_path / "trade.yaml"))
    app.start(start_timers=False)
    yield TestClient(create_api_app(app)), app
    app.stop()


def _log(app, rows):
    """直接往台账写行 (绕开策略 —— 本文件测的是读侧契约)。"""
    assert app.store.decision.log(rows) == len(rows)


def _audit(app, kind, message="", detail=None):
    """塞一行审计, ts 钉在 D1 当天。

    `store.write_audit` 不接 ts (用真实 time.time()), 而运行痕迹/人工成交都是
    按"当天窗口"查的 —— 用真实现在的时间戳会落到窗口外, 测试随真实日期漂移。
    """
    import json as _json
    with app.store._lock, app.store._conn:
        app.store._conn.execute(
            "INSERT INTO audit (ts, kind, message, detail_json) VALUES (?,?,?,?)",
            (_T0, kind, message, _json.dumps(detail or {}, ensure_ascii=False)))


def _row(date=D1, strategy="rotation", subject="份1",
         action="HOLD", code="ROT_HOLD_KEEP", text="维持持仓（不用换腿）",
         source="live", evidence=None):
    return {"trade_date": date, "strategy": strategy, "subject": subject,
            "action": action, "reason_code": code, "reason_text": text,
            "source": source, "evidence": evidence or {}, "trade_ids": ""}


def _get(client, day=None):
    url = "/api/trade/decisions" + (f"?date={day}" if day else "")
    r = client.get(url)
    assert r.status_code == 200, r.text
    return r.json()


def _cal(client, month=None):
    url = "/api/trade/decisions/calendar" + (f"?month={month}" if month else "")
    r = client.get(url)
    assert r.status_code == 200, r.text
    return r.json()


# ═══════════════════════════════════════════════════════════════
# 基本形状
# ═══════════════════════════════════════════════════════════════

def test_response_shape(ctx):
    """顶层字段齐备 —— 前端 statusLine / summary / groups 全靠这几个键。"""
    client, app = ctx
    _log(app, [_row()])
    d = _get(client, DAY8)
    for key in ("date", "trading_day", "run_any", "run_trace", "groups",
                "manual_trades_today", "summary"):
        assert key in d, key
    assert d["date"] == D1
    assert d["trading_day"] is True


def test_row_shape(ctx):
    """每一行都要带上人话短标签与色调 —— 前端不该自己去认原因码。"""
    client, app = ctx
    _log(app, [_row()])
    r = _get(client, DAY8)["groups"][0]["rows"][0]
    assert r["reason_label"] == "维持持仓（不用换腿）"
    assert r["tone"] == "muted"
    assert r["reason_text"] == "维持持仓（不用换腿）"
    assert isinstance(r["evidence"], dict)


def test_unknown_reason_code_still_returns_a_row(ctx):
    """表外原因码 (将来新增策略) 也要能出图, 不报错 —— 退回灰字。"""
    client, app = ctx
    _log(app, [_row(code="FUTURE_CODE", text="未来策略的原因")])
    r = _get(client, DAY8)["groups"][0]["rows"][0]
    assert r["reason_code"] == "FUTURE_CODE"
    assert r["tone"] == "muted"
    assert r["reason_label"] == "FUTURE_CODE"      # 兜底用码本身, 便于排查


# ═══════════════════════════════════════════════════════════════
# 分组: 空组也要返回
# ═══════════════════════════════════════════════════════════════

def test_all_four_groups_always_present_in_order(ctx):
    """四条策略的组永远都在、顺序固定, 即使当天一条都没跑。

    为什么空组不能省: 前端在空组里显示「这条策略今天没有留痕」的灰色占位。
    组一消失, 用户就分不清"这条策略没跑"和"页面渲染坏了"。
    """
    client, app = ctx
    _log(app, [_row()])
    groups = _get(client, DAY8)["groups"]
    assert [g["strategy"] for g in groups] == list(GROUP_ORDER)
    assert all(g["rows"] == [] for g in groups[1:])   # 后三组是空的但有壳
    assert all(g["label"] for g in groups)


def test_empty_groups_carry_human_label(ctx):
    """空组也要有中文名 (页面直接显示, 不能露出英文键名)。"""
    client, app = ctx
    _log(app, [_row()])
    got = {g["strategy"]: g["label"] for g in _get(client, DAY8)["groups"]}
    assert got["rotation"] == STRATEGY_LABELS["rotation"]
    assert got["exit"] == STRATEGY_LABELS["exit"]


def test_rows_grouped_by_strategy(ctx):
    client, app = ctx
    _log(app, [_row(strategy="rotation", subject="份1"),
               _row(strategy="rotation", subject="份2"),
               _row(strategy="auto_buy", subject="pool", action="BUY",
                    code="PICK_BUY"),
               _row(strategy="exit", subject="600519.SH", code="EXIT_NO_TRIGGER"),
               _row(strategy="ladder", subject="600519.SH", code="LADDER_PLACED")])
    got = {g["strategy"]: len(g["rows"]) for g in _get(client, DAY8)["groups"]}
    assert got == {"rotation": 2, "auto_buy": 1, "exit": 1, "ladder": 1}


def test_future_strategy_is_not_dropped(ctx):
    """将来新增第五条策略: 即使不在 GROUP_ORDER 里也要兜底返回, 不能静默吞掉。
    吞掉的表现是"跑了但页面上看不见", 最难排查。"""
    client, app = ctx
    _log(app, [_row(strategy="new_future_kind", subject="x")])
    groups = _get(client, DAY8)["groups"]
    assert [g["strategy"] for g in groups][-1] == "new_future_kind"


# ═══════════════════════════════════════════════════════════════
# summary 计数
# ═══════════════════════════════════════════════════════════════

def test_summary_counts_by_action(ctx):
    client, app = ctx
    _log(app, [
        _row(subject="a", action="BUY", code="ROT_SWITCH"),
        _row(subject="b", action="SELL", code="ROT_TRAILING_STOP"),
        _row(subject="c", action="HOLD"),
        _row(subject="d", action="HOLD"),
        _row(subject="e", action="FAIL", code="ROT_ERROR"),
        _row(subject="f", action="INFO", code="ROT_INIT"),
    ])
    d = _get(client, DAY8)
    assert d["summary"] == {"buy": 1, "sell": 1, "hold": 2, "fail": 1,
                            "info": 1, "inferred": 0}


def test_summary_counts_inferred_separately(ctx):
    """可信度单列一个计数 —— 页面据此提示"这些不是当场记的"。"""
    client, app = ctx
    _log(app, [_row(subject="a"), _row(subject="b", source="inferred")])
    assert _get(client, DAY8)["summary"]["inferred"] == 1


def test_summary_zero_when_no_rows(ctx):
    """一行都没有时 summary 也要成套返回全 0 (前端不做空值判断)。"""
    client, app = ctx
    d = _get(client, "20260919")   # 周六 → 不合成 NO_RUN, groups 空
    assert d["summary"] == {"buy": 0, "sell": 0, "hold": 0, "fail": 0,
                            "info": 0, "inferred": 0}


# ═══════════════════════════════════════════════════════════════
# 没数据时说什么 (核心)
# ═══════════════════════════════════════════════════════════════

def test_holiday_returns_no_groups(ctx):
    """休市日不出"没运行"的卡 —— 那是日历格标「休市」的事。
    不然每个周末都要报一次"程序没跑", 用户会以为系统周末都在挂。"""
    client, app = ctx
    d = _get(client, SAT.replace("-", ""))
    assert d["trading_day"] is False
    assert d["groups"] == []
    assert d["run_any"] is False


def test_trading_day_without_rows_synthesizes_no_run(ctx):
    """交易日但一行都没有 → 合成 system 组里的 NO_RUN 行。"""
    client, app = ctx
    _log(app, [_row(date=D2)])                  # 只在 D2 有数据
    d = _get(client, DAY8)                      # 查 D1
    assert d["trading_day"] is True
    assert d["run_any"] is False
    assert len(d["groups"]) == 1
    g = d["groups"][0]
    assert g["strategy"] == "system"
    row = g["rows"][0]
    assert row["reason_code"] == "NO_RUN"
    assert row["source"] == "inferred"
    assert row["action"] == "HOLD"
    assert row["tone"] == "muted"


def test_synthesized_no_run_stays_inferred_if_logged(ctx):
    """合成的 NO_RUN **必须保持 inferred 可信度**。

    这条防的是"把推断当事实": 合成行要是被标成 live, 用户会以为系统当天真跑过,
    而实际上我们对那天的了解只来自"资产表里没有这一行"这一条间接证据。
    """
    client, app = ctx
    _log(app, [_row(date=D2)])
    row = _get(client, DAY8)["groups"][0]["rows"][0]
    assert row["source"] == "inferred"
    assert _get(client, DAY8)["summary"]["inferred"] == 1


def test_no_run_wording_without_any_trace(ctx):
    """没有任何痕迹 (连资产行都没有) → 说"程序没开机, 也没留下别的记录"。"""
    client, app = ctx
    row = _get(client, DAY8)["groups"][0]["rows"][0]
    assert "没有任何运行痕迹" in row["reason_text"]


def test_no_run_wording_when_asset_was_derived(ctx):
    """资产表里那天的来源是 derived (用收盘价推算补的) → 说"程序没开机"。
    这是"程序没开机"的**直接证据**, 比"什么都没留下"这句话更有信息量。"""
    client, app = ctx
    with app.store._lock, app.store._conn:
        app.store._conn.execute(
            "INSERT INTO daily_asset (date,total_asset,available,market_value,"
            "ts,source) VALUES (?,?,?,?,?,?)",
            (D1, 1000000.0, 1000000.0, 0.0, _T0, "derived"))
    row = _get(client, DAY8)["groups"][0]["rows"][0]
    assert "程序没开机" in row["reason_text"]
    assert row["evidence"]["daily_asset_source"] == "derived"


def test_no_run_wording_when_program_ran_but_no_ledger(ctx):
    """资产表有 eod 行 (程序真跑过) 但没有决策记录 → 要老实说"这个功能是
    2026 年 9 月 18 日才上线的"。这句比"没运行"准确得多。"""
    client, app = ctx
    with app.store._lock, app.store._conn:
        app.store._conn.execute(
            "INSERT INTO daily_asset (date,total_asset,available,market_value,"
            "ts,source) VALUES (?,?,?,?,?,?)",
            (D1, 1000000.0, 1000000.0, 0.0, _T0, "eod"))
    row = _get(client, DAY8)["groups"][0]["rows"][0]
    assert "9 月 18 日" in row["reason_text"]
    assert row["evidence"]["daily_asset_source"] == "eod"


def test_before_ledger_start_returns_note(ctx):
    """台账起点之前的日子 → 一句 note 说明, 而不是对着空白页猜。"""
    client, app = ctx
    d = _get(client, "20260701")
    assert "note" in d
    assert "2026-07-27" in d["note"]
    assert d["groups"] == []


def test_ledger_start_itself_is_queried_normally(ctx):
    """起点当天要正常查 (边界含端点)。"""
    client, app = ctx
    _log(app, [_row(date="2026-07-27")])
    d = _get(client, "20260727")
    assert "note" not in d
    assert d["run_any"] is True


# ═══════════════════════════════════════════════════════════════
# 运行痕迹 / 人工成交
# ═══════════════════════════════════════════════════════════════

def test_run_trace_reads_audit_not_ledger(ctx):
    """运行痕迹来自审计表 —— 台账行可能是回填来的, 不能用来证明"那天跑过"。"""
    client, app = ctx
    _log(app, [_row()])                    # 只有回填来的台账行
    d = _get(client, DAY8)
    assert d["run_any"] is True            # 台账里有行
    assert d["run_trace"] == {}            # 但审计表里没有运行痕迹


def test_run_trace_reports_each_leg(ctx):
    """三条腿各自跑过都要能看出来, 并带上各自的时点。"""
    client, app = ctx
    _audit(app, "rotation_summary", "轮动跑完")
    _audit(app, "auto_buy_summary", "选股跑完")
    _audit(app, "eod", "收盘归档")
    _log(app, [_row()])
    trace = _get(client, DAY8)["run_trace"]
    assert set(trace) == {"rotation", "auto_buy", "eod"}
    assert "已归档" in trace["eod"]


def test_run_trace_recognizes_skip_regime_as_ran(ctx):
    """弱市闸门拦下也算"跑过" —— 它跑到了判断那一步, 只是结论是不买。"""
    client, app = ctx
    _audit(app, "auto_buy_skip_regime", "大盘没站上年线")
    _log(app, [_row(strategy="auto_buy", subject="pool")])
    assert "auto_buy" in _get(client, DAY8)["run_trace"]


def test_run_trace_uses_rotation_start_too(ctx):
    """只有 rotation_start (还没收盘汇总) 也算跑过 —— 状态条要能显示"正在跑"。"""
    client, app = ctx
    _audit(app, "rotation_start", "轮动开始")
    _log(app, [_row()])
    assert "rotation" in _get(client, DAY8)["run_trace"]


def test_run_trace_ignores_other_days(ctx):
    """昨天的运行痕迹不能算到今天头上。"""
    client, app = ctx
    import json as _json
    with app.store._lock, app.store._conn:
        app.store._conn.execute(
            "INSERT INTO audit (ts, kind, message, detail_json) VALUES (?,?,?,?)",
            (_T0 - 86400, "rotation_summary", "昨天的", _json.dumps({})))
    _log(app, [_row()])
    assert _get(client, DAY8)["run_trace"] == {}


def test_manual_trades_are_counted(ctx):
    """人工成交要单独报出来 —— 让人不会因为"系统什么都没干"而以为程序挂了。"""
    client, app = ctx
    app.store.save_trade({
        "traded_id": "T-M1", "order_id": "O-M1", "code": "600519.SH",
        "direction": 1, "price": 10.0, "qty": 100, "ts": _T0,
        "source": "manual", "reason": "人工卖出"})
    assert _get(client, DAY8)["manual_trades_today"] == 1


def test_system_trades_are_not_counted_as_manual(ctx):
    """系统单不算人工 —— 否则每天都会提示"有 N 笔人工成交", 提示就失效了。"""
    client, app = ctx
    app.store.save_trade({
        "traded_id": "T-S1", "order_id": "O-S1", "code": "600519.SH",
        "direction": 1, "price": 10.0, "qty": 100, "ts": _T0,
        "source": "system", "reason": "硬止损"})
    assert _get(client, DAY8)["manual_trades_today"] == 0


# ═══════════════════════════════════════════════════════════════
# 缺省日期
# ═══════════════════════════════════════════════════════════════

def test_default_date_is_today(ctx):
    """不传 date → 查今天 (卡片一切到页面就自动加载当天)。"""
    client, _ = ctx
    assert _get(client)["date"] == _dt.date.today().isoformat()


def test_default_month_is_current(ctx):
    client, _ = ctx
    assert _cal(client)["month"] == _dt.date.today().strftime("%Y-%m")


# ═══════════════════════════════════════════════════════════════
# 入参校验
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("bad", ["2026-09-16", "2026091", "202609166",
                                 "abcdefgh", "20261332", "%%%"])
def test_bad_date_is_422(ctx, bad):
    """非法日期一律 422 (与页面其它日期框同口径: 只收 8 位数字)。
    不能默默当成今天 —— 那会让用户以为查到了那天。"""
    client, _ = ctx
    assert client.get(f"/api/trade/decisions?date={bad}").status_code == 422


@pytest.mark.parametrize("bad", ["2026-09", "20269", "2026091", "202613", "abc"])
def test_bad_month_is_422(ctx, bad):
    client, _ = ctx
    assert client.get(
        f"/api/trade/decisions/calendar?month={bad}").status_code == 422


def test_month_13_is_422(ctx):
    """13 月这种"长度对但值不对"的也要拦 —— 只判长度会放行。"""
    client, _ = ctx
    assert client.get(
        "/api/trade/decisions/calendar?month=202613").status_code == 422


def test_ledger_read_failure_is_soft(ctx, monkeypatch):
    """台账读异常 → 200 + note 说明, 不是 500。
    一张只读卡片挂了不该把整个交易服务页面带崩。"""
    client, app = ctx
    monkeypatch.setattr(app.store, "open_readonly",
                        lambda: (_ for _ in ()).throw(RuntimeError("库坏了")))
    r = client.get(f"/api/trade/decisions?date={DAY8}")
    assert r.status_code == 200
    assert "note" in r.json()


# ═══════════════════════════════════════════════════════════════
# 日历
# ═══════════════════════════════════════════════════════════════

def test_calendar_covers_whole_month(ctx):
    """9 月 30 天, 每天一格, 顺序递增 —— 前端按 ISO 日期取格。"""
    client, _ = ctx
    d = _cal(client, "202609")
    assert d["month"] == "2026-09"
    assert len(d["days"]) == 30
    assert d["days"][0]["date"] == "2026-09-01"
    assert d["days"][-1]["date"] == "2026-09-30"


def test_calendar_marks_weekends_as_holiday(ctx):
    """周六日的格必须标 trading_day=False —— 前端据此显示灰底「休市」。"""
    client, _ = ctx
    days = {d["date"]: d for d in _cal(client, "202609")["days"]}
    assert days["2026-09-19"]["trading_day"] is False      # 周六
    assert days["2026-09-20"]["trading_day"] is False      # 周日
    assert days["2026-09-18"]["trading_day"] is True        # 周五


def test_calendar_marks_trading_day_without_rows_as_not_run(ctx):
    """交易日一行都没有 → 标「没运行」(与卡片一同一个判据)。

    这条是用户最需要的: 翻历史时一眼看出哪天程序没开。
    """
    client, app = ctx
    _log(app, [_row(date=D1)])
    days = {d["date"]: d for d in _cal(client, "202609")["days"]}
    d = days["2026-09-15"]                    # 周二, 交易日, 没数据
    assert d["trading_day"] is True
    assert d["headline"] == "没有任何运行痕迹"
    assert d["inferred"] is True


def test_calendar_does_not_mark_before_ledger_start(ctx):
    """起点之前的交易日不标「没运行」—— 那时台账功能还不存在, 标了是冤枉程序。"""
    client, _ = ctx
    days = {d["date"]: d for d in _cal(client, "202607")["days"]}
    assert days["2026-07-01"]["headline"] == ""
    assert days["2026-07-01"]["inferred"] is False


def test_calendar_marks_ledger_start_itself(ctx):
    """起点当天要参与标记 (边界含端点)。"""
    client, app = ctx
    _log(app, [_row(date="2026-07-27")])
    days = {d["date"]: d for d in _cal(client, "202607")["days"]}
    assert days["2026-07-27"]["rows"] == 1


def test_calendar_reports_row_counts(ctx):
    client, app = ctx
    _log(app, [_row(date=D1, subject="份1"), _row(date=D1, subject="份2")])
    days = {d["date"]: d for d in _cal(client, "202609")["days"]}
    assert days[D1]["rows"] == 2


def test_calendar_reports_tone_distribution(ctx):
    """每格的色调分布 —— 前端据此给格子刷底色 (红=买 / 绿=卖 / 黄=出问题)。"""
    client, app = ctx
    _log(app, [_row(date=D1, subject="a", action="BUY", code="ROT_SWITCH"),
               _row(date=D1, subject="b", action="HOLD"),
               _row(date=D1, subject="c", action="HOLD")])
    tones = {d["date"]: d for d in _cal(client, "202609")["days"]}[D1]["tones"]
    assert tones == {"up": 1, "muted": 2}


def test_calendar_headline_picks_strongest_action(ctx):
    """一格只能写一行字 → 取当天"动作最强"的那条 (卖 > 买 > 失败 > 告知 > 没动)。

    取错了会误导: 一天里既有卖出又有没动, 摘要该说"卖出了什么",
    而不是最不重要的那条"没动"。
    """
    client, app = ctx
    _log(app, [
        _row(date=D1, subject="a", strategy="exit", action="HOLD",
             code="EXIT_NO_TRIGGER"),
        _row(date=D1, subject="b", strategy="exit", action="SELL",
             code="EXIT_TRIGGERED"),
        _row(date=D1, subject="c", strategy="rotation", action="BUY",
             code="ROT_SWITCH")])
    day = {d["date"]: d for d in _cal(client, "202609")["days"]}[D1]
    assert day["headline"].startswith(STRATEGY_LABELS["exit"])
    assert "触发了卖出" in day["headline"]


def test_calendar_marks_all_inferred_day(ctx):
    """整天都是推断出来的 → inferred=True, 前端可以打个"不确切"的标记。"""
    client, app = ctx
    _log(app, [_row(date=D1, source="inferred")])
    day = {d["date"]: d for d in _cal(client, "202609")["days"]}[D1]
    assert day["inferred"] is True


def test_calendar_mixed_source_is_not_inferred(ctx):
    """只要有一条是当场/回填的, 整天就不能标成"全是推断"。"""
    client, app = ctx
    _log(app, [_row(date=D1, subject="a", source="inferred"),
               _row(date=D1, subject="b", source="live")])
    assert {d["date"]: d for d in _cal(client, "202609")["days"]}[D1][
        "inferred"] is False


def test_calendar_month_boundaries(ctx):
    """跨月边界: 2 月天数、12 月收尾都要对 (前端按天数铺格子)。"""
    client, _ = ctx
    feb = _cal(client, "202602")
    assert len(feb["days"]) == 28          # 2026 非闰年
    dec = _cal(client, "202612")
    assert dec["days"][0]["date"] == "2026-12-01"
    assert dec["days"][-1]["date"] == "2026-12-31"


def test_calendar_empty_month_has_all_zero_cells(ctx):
    """整月没数据也要把格子给全 (rows=0), 不能返回空数组让前端没格子铺。"""
    client, _ = ctx
    d = _cal(client, "202605")
    assert len(d["days"]) == 31
    assert all("rows" in day for day in d["days"])


def test_calendar_read_failure_does_not_claim_not_run(ctx, monkeypatch):
    """读失败 → 空格子 + note, **绝不能**把交易日标成「没运行」。

    这条锁的是一个会误导人的 bug (2026-09-18 补测试时发现并修掉): 原来读库抛异常
    时只把 rows 置空, 然后照常走"交易日没行 → 标没运行"的分支 —— 结果一次瞬时
    数据库抖动就让整月每个交易日都显示「没运行」, 页面等于在指控程序整月没开机。
    卡片一的端点遇到同样情况本来就会给 note, 两边行为现已对齐。
    """
    client, app = ctx
    monkeypatch.setattr(app.store, "open_readonly",
                        lambda: (_ for _ in ()).throw(RuntimeError("库坏了")))
    r = client.get("/api/trade/decisions/calendar?month=202609")
    assert r.status_code == 200                     # 不是 500
    body = r.json()
    assert len(body["days"]) == 30                  # 骨架照给, 前端还能画格子
    assert body["note"] == "查询失败：台账读取异常（不影响交易）"
    # 关键: 每个交易日都**没有**被说成"没运行"
    trading_days = [d for d in body["days"] if d["trading_day"]]
    assert trading_days, "测试前提: 9 月得真有交易日"
    assert all(d["rows"] == 0 and d["headline"] == "" for d in trading_days)
    assert all(d["inferred"] is False for d in body["days"])


def test_calendar_success_has_no_note(ctx):
    """正常读到数据时不该出现 note (有 note = 出过错)。"""
    client, app = ctx
    _log(app, [_row(date=D1)])
    assert "note" not in _cal(client, "202609")
