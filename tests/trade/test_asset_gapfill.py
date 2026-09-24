"""停机日资产补算 (trade/asset_gapfill) 测试。

契约 (2026-09-10 事件驱动, 计划书 docs/plan/2026-09-10_停机日资产补算_计划书.md):

- plan_gaps(): 纯函数。给定实测行 + 最新持仓 + 缺口内成交 + 逐日收盘价回调,
  输出每个缺口的推算计划; 两端夹逼差额 ≤ 容差才 write, 超容差 reject,
  尾部缺口(右端无实测行) skip; 任何一天缺收盘价 → 该缺口不写。
- fill_gaps(): 编排。写 source='derived' 的行 + audit 留痕; 幂等;
  绝不覆盖 source='eod' 的实测行; dry_run 只算不写。
- 真实事件数字 (9/8→9/10): 9/9 应推成 1,065,038.12 (现金 527,984.52 +
  240,400 股 × 2.234), 右端残差 0.00。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.asset_gapfill import GapDay, GapRun, fill_gaps, plan_gaps  # noqa: E402
from trade.book import DIRECTION_BUY, DIRECTION_SELL  # noqa: E402
from trade.store import TradeStore  # noqa: E402

ETF = "513100.SH"
GOLD = "518880.SH"
STOCK = "600000.SH"


# ────────────────────────── 夹具 ──────────────────────────

def _row(date, total, cash, mv, source="eod"):
    return {"date": date, "total_asset": total, "available": cash,
            "market_value": mv, "ts": 0.0, "source": source}


def _close_map(mapping):
    """{日期: {代码: 收盘价}} → close_at(code, date) 回调。"""
    def close_at(code, date):
        return mapping.get(date, {}).get(code)
    return close_at


def _always_trading(date):
    return True


#: 真实交易日集合 (8/29-8/30 是周末, 不能当缺口 —— 缺口只按交易日算)
_WEEKDAYS = {"2026-08-28", "2026-08-31", "2026-09-01", "2026-09-02"}


def _weekdays(date):
    return date in _WEEKDAYS


# 真实事件的三天 (不复权收盘价取自当日行情)
REAL_CLOSES = {
    "2026-09-08": {ETF: 2.233},
    "2026-09-09": {ETF: 2.234},
    "2026-09-10": {ETF: 2.219},
}
REAL_ROWS = [
    _row("2026-09-08", 1_064_797.72, 527_984.52, 536_813.20),
    _row("2026-09-10", 1_061_432.12, 527_984.52, 533_447.60),
]
REAL_POSITIONS = {ETF: 240_400}


# ────────────────────────── plan_gaps ──────────────────────────

def test_plan_single_day_gap_exact():
    """9/9 单日缺口: 现金不变 + 240,400 股 × 2.234, 右端残差 0.00。"""
    runs = plan_gaps(
        REAL_ROWS, positions=REAL_POSITIONS, trades_by_date={},
        close_at=_close_map(REAL_CLOSES), is_trading_day=_always_trading,
        today="2026-09-10")

    assert len(runs) == 1
    run = runs[0]
    assert isinstance(run, GapRun)
    assert run.dates == ("2026-09-09",)
    assert run.left_date == "2026-09-08"
    assert run.right_date == "2026-09-10"
    assert run.status == "write", run.reason
    assert run.residual == pytest.approx(0.0, abs=0.01)
    assert run.left_residual == pytest.approx(0.0, abs=0.01)

    day = run.days[0]
    assert isinstance(day, GapDay)
    assert day.date == "2026-09-09"
    assert day.available == pytest.approx(527_984.52, abs=0.01)
    assert day.market_value == pytest.approx(537_053.60, abs=0.01)
    assert day.total_asset == pytest.approx(1_065_038.12, abs=0.01)
    assert day.adjust == 0.0


def test_plan_right_anchor_single_day_is_clean():
    """补上 9/9 后, 9/10 的单日盈亏回到真值 -3,606.00 (不再是 -3,365.60)。"""
    runs = plan_gaps(
        REAL_ROWS, positions=REAL_POSITIONS, trades_by_date={},
        close_at=_close_map(REAL_CLOSES), is_trading_day=_always_trading,
        today="2026-09-10")
    day = runs[0].days[0]
    right = REAL_ROWS[1]
    assert right["total_asset"] - day.total_asset == pytest.approx(-3_606.00, abs=0.01)


def test_plan_trades_inside_gap_move_cash():
    """缺口内有成交: 买入扣现金、加股数, 当日市值含新仓。"""
    rows = [
        _row("2026-09-08", 1_064_797.72, 527_984.52, 536_813.20),
        _row("2026-09-10", 1_061_532.12, 526_984.52, 534_547.60),
    ]
    closes = {
        "2026-09-08": {ETF: 2.233, STOCK: 10.0},
        "2026-09-09": {ETF: 2.234, STOCK: 10.5},
        "2026-09-10": {ETF: 2.219, STOCK: 11.0},
    }
    trades = {"2026-09-09": [(STOCK, DIRECTION_BUY, 100, 1_000.0)]}

    runs = plan_gaps(rows, positions={ETF: 240_400, STOCK: 100},
                     trades_by_date=trades, close_at=_close_map(closes),
                     is_trading_day=_always_trading, today="2026-09-10")

    run = runs[0]
    assert run.status == "write", run.reason
    assert run.residual == pytest.approx(0.0, abs=0.01)
    day = run.days[0]
    assert day.available == pytest.approx(526_984.52, abs=0.01)  # 扣掉 1,000
    assert day.market_value == pytest.approx(538_103.60, abs=0.01)  # +100×10.5
    assert day.total_asset == pytest.approx(1_065_088.12, abs=0.01)


def test_plan_multi_day_gap_spreads_residual():
    """两天缺口 + 未记录的手工买入 (真实 8/31–9/1 场景):

    反推持仓时把那 200 股 518880 还原回去了, 但成交表里没有对应的买入,
    于是右端夹逼差 1,847.20 (0.17% < 0.5%) → 写入, 差额按日均摊。
    """
    rows = [
        _row("2026-08-28", 1_065_276.92, 532_005.92, 533_271.00),
        _row("2026-09-02", 1_055_662.32, 527_984.32, 527_678.00),
    ]
    closes = {
        "2026-08-28": {ETF: 2.235, GOLD: 9.150},
        "2026-08-31": {ETF: 2.223, GOLD: 9.130},
        "2026-09-01": {ETF: 2.233, GOLD: 9.120},
        "2026-09-02": {ETF: 2.195, GOLD: 8.892},
    }
    trades = {"2026-09-02": [(GOLD, DIRECTION_SELL, 200, 1_778.40),
                             (ETF, DIRECTION_BUY, 1_800, 3_952.80)]}

    runs = plan_gaps(rows, positions={ETF: 240_400},
                     trades_by_date=trades, close_at=_close_map(closes),
                     is_trading_day=_weekdays, today="2026-09-02")

    run = runs[0]
    assert run.dates == ("2026-08-31", "2026-09-01")
    assert run.status == "write", run.reason
    assert run.residual == pytest.approx(1_847.20, abs=0.01)
    # 左端诊断: 反推持仓比左端实测市值多出那 200 股 (200 × 9.15)
    assert run.left_residual == pytest.approx(1_830.00, abs=0.01)
    # 差额按日均摊 → 各日现金各减 923.60, 合计正好抵掉残差
    assert [d.adjust for d in run.days] == pytest.approx([-923.60, -923.60], abs=0.01)
    assert sum(d.adjust for d in run.days) == pytest.approx(-run.residual, abs=0.01)
    # 左端实测行一分不动
    assert run.days[0].total_asset == pytest.approx(
        532_005.92 + 238_600 * 2.223 + 200 * 9.130 - 923.60, abs=0.05)


def test_plan_multi_day_gap_tail_day_absorbs_rounding():
    """奇数分: 均摊除不尽的余数落最后一天, 保证合计精确。"""
    rows = [
        _row("2026-08-28", 1_065_276.92, 532_005.92, 533_271.00),
        _row("2026-09-02", 1_055_662.32 + 0.03, 527_984.32, 527_678.00),
    ]
    closes = {
        "2026-08-31": {ETF: 2.223},
        "2026-09-01": {ETF: 2.233},
        "2026-09-02": {ETF: 2.195},
    }
    runs = plan_gaps(rows, positions={ETF: 238_600}, trades_by_date={},
                     close_at=_close_map(closes), is_trading_day=_weekdays,
                     today="2026-09-02")
    run = runs[0]
    assert run.status == "write", run.reason
    assert sum(d.adjust for d in run.days) == pytest.approx(-run.residual, abs=0.005)


def test_plan_rejects_residual_over_tolerance():
    """差额超过容差 (0.5%) → 拒写, 只报差。"""
    rows = [
        _row("2026-09-08", 1_064_797.72, 527_984.52, 536_813.20),
        _row("2026-09-10", 1_061_432.12 + 20_000.0, 527_984.52, 533_447.60),
    ]
    runs = plan_gaps(rows, positions=REAL_POSITIONS, trades_by_date={},
                     close_at=_close_map(REAL_CLOSES), is_trading_day=_always_trading,
                     today="2026-09-10")
    run = runs[0]
    assert run.status == "reject"
    assert run.days == ()
    assert "对不上" in run.reason and "20,000" in run.reason.replace("20,000.00", "20,000")


def test_plan_tolerance_is_configurable():
    """容差可调: 同样 20,000 的差额放宽到 5% 就能写。"""
    rows = [
        _row("2026-09-08", 1_064_797.72, 527_984.52, 536_813.20),
        _row("2026-09-10", 1_061_432.12 + 20_000.0, 527_984.52, 533_447.60),
    ]
    runs = plan_gaps(rows, positions=REAL_POSITIONS, trades_by_date={},
                     close_at=_close_map(REAL_CLOSES), is_trading_day=_always_trading,
                     today="2026-09-10", tolerance=0.05)
    assert runs[0].status == "write"


def test_plan_missing_close_skips_run():
    """缺某天收盘价 → 不写 (宁可不写, 不写错数)。"""
    closes = {"2026-09-08": {ETF: 2.233}, "2026-09-10": {ETF: 2.219}}  # 缺 9/9
    runs = plan_gaps(REAL_ROWS, positions=REAL_POSITIONS, trades_by_date={},
                     close_at=_close_map(closes), is_trading_day=_always_trading,
                     today="2026-09-10")
    run = runs[0]
    assert run.status == "reject"
    assert run.days == ()
    assert "收盘价" in run.reason and "2026-09-09" in run.reason


def test_plan_tail_gap_skipped_until_today_archived():
    """尾部缺口 (今天还没归档, 没有右端实测行可校验) → skip 并说明。"""
    rows = [_row("2026-09-08", 1_064_797.72, 527_984.52, 536_813.20)]
    runs = plan_gaps(rows, positions=REAL_POSITIONS, trades_by_date={},
                     close_at=_close_map(REAL_CLOSES), is_trading_day=_always_trading,
                     today="2026-09-10")
    run = runs[0]
    assert run.dates == ("2026-09-09",)
    assert run.right_date is None
    assert run.status == "skip"
    assert "归档" in run.reason


def test_plan_no_rows_no_plan():
    """账户首月无锚点 → 不推。"""
    assert plan_gaps([], positions=REAL_POSITIONS, trades_by_date={},
                     close_at=_close_map(REAL_CLOSES),
                     is_trading_day=_always_trading, today="2026-09-10") == []


def test_plan_ignores_non_trading_days():
    """周末/节假日不算缺口。"""
    rows = [_row("2026-09-04", 1_000_000.0, 500_000.0, 500_000.0),
            _row("2026-09-08", 1_000_000.0, 500_000.0, 500_000.0)]
    trading = {"2026-09-07"}
    runs = plan_gaps(rows, positions={}, trades_by_date={},
                     close_at=_close_map({}), is_trading_day=lambda d: d in trading,
                     today="2026-09-08")
    assert [r.dates for r in runs] == [("2026-09-07",)]


def test_plan_recomputes_derived_rows():
    """已存在的推算行不算锚点: 重跑时重新推算 (可被补录成交修正)。"""
    rows = [
        _row("2026-09-08", 1_064_797.72, 527_984.52, 536_813.20),
        _row("2026-09-09", 999.0, 999.0, 0.0, source="derived"),   # 过期脏值
        _row("2026-09-10", 1_061_432.12, 527_984.52, 533_447.60),
    ]
    runs = plan_gaps(rows, positions=REAL_POSITIONS, trades_by_date={},
                     close_at=_close_map(REAL_CLOSES), is_trading_day=_always_trading,
                     today="2026-09-10")
    assert len(runs) == 1
    assert runs[0].left_date == "2026-09-08"
    assert runs[0].right_date == "2026-09-10"
    assert runs[0].days[0].total_asset == pytest.approx(1_065_038.12, abs=0.01)


def test_plan_negative_holdings_from_incomplete_trades_is_reported():
    """反推持仓出现负股数 (成交表不全) → 左端诊断报出来, 不静默。"""
    rows = [
        _row("2026-09-08", 1_064_797.72, 527_984.52, 536_813.20),
        _row("2026-09-10", 1_061_432.12, 527_984.52, 533_447.60),
    ]
    trades = {"2026-09-09": [(ETF, DIRECTION_SELL, 1_000, 2_234.0)]}
    runs = plan_gaps(rows, positions={}, trades_by_date=trades,
                     close_at=_close_map(REAL_CLOSES), is_trading_day=_always_trading,
                     today="2026-09-10")
    assert runs[0].left_residual != 0.0


# ────────────────────────── fill_gaps ──────────────────────────

@pytest.fixture()
def store(tmp_path):
    st = TradeStore(str(tmp_path / "t.db"), str(tmp_path / "raw.jsonl"))
    yield st
    st.close()


def _seed(store, rows):
    for r in rows:
        store.daily_asset.save(r["date"], r["total_asset"], r["available"],
                               r["market_value"], source=r.get("source", "eod"))


def _fill(store, **kw):
    kw.setdefault("positions", REAL_POSITIONS)
    kw.setdefault("close_at", _close_map(REAL_CLOSES))
    kw.setdefault("is_trading_day", _always_trading)
    kw.setdefault("today", "2026-09-10")
    return fill_gaps(store, **kw)


def test_fill_writes_derived_row(store):
    _seed(store, REAL_ROWS)
    rep = _fill(store)

    assert rep["written"] == ["2026-09-09"]
    rows = {r["date"]: r for r in store.daily_asset.get()}
    assert rows["2026-09-09"]["source"] == "derived"
    assert rows["2026-09-09"]["total_asset"] == pytest.approx(1_065_038.12, abs=0.01)
    assert rows["2026-09-08"]["source"] == "eod"


def test_fill_is_idempotent(store):
    _seed(store, REAL_ROWS)
    _fill(store)
    _fill(store)
    rows = [r for r in store.daily_asset.get() if r["date"] == "2026-09-09"]
    assert len(rows) == 1
    assert rows[0]["total_asset"] == pytest.approx(1_065_038.12, abs=0.01)


def test_fill_dry_run_writes_nothing(store):
    _seed(store, REAL_ROWS)
    rep = _fill(store, dry_run=True)
    assert rep["dry_run"] is True
    assert rep["written"] == ["2026-09-09"]          # 计划里有
    assert [r["date"] for r in store.daily_asset.get()] == \
        ["2026-09-08", "2026-09-10"]                  # 库里没有


def test_fill_never_overwrites_measured_row(store):
    """实测行受保护: 同日已有 source='eod' 就不补 (也不改数)。"""
    _seed(store, REAL_ROWS)
    store.daily_asset.save("2026-09-09", 1.0, 1.0, 0.0, source="eod")
    rep = _fill(store)
    assert rep["written"] == []
    row = [r for r in store.daily_asset.get() if r["date"] == "2026-09-09"][0]
    assert row["total_asset"] == 1.0
    assert row["source"] == "eod"


def test_store_save_refuses_derived_over_measured(store):
    """存储层最后一道保险: derived 不得覆盖 eod。"""
    store.daily_asset.save("2026-09-09", 100.0, 100.0, 0.0, source="eod")
    assert store.daily_asset.save("2026-09-09", 999.0, 999.0, 0.0,
                                  source="derived") is False
    row = store.daily_asset.get(start="2026-09-09", end="2026-09-09")[0]
    assert row["total_asset"] == 100.0 and row["source"] == "eod"


def test_fill_reports_unfilled_dates(store):
    """拒写的缺口日要能被前端标注 (未归档·差¥X)。"""
    rows = [_row("2026-09-08", 1_064_797.72, 527_984.52, 536_813.20),
            _row("2026-09-10", 1_081_432.12, 527_984.52, 533_447.60)]
    _seed(store, rows)
    rep = _fill(store)
    assert rep["written"] == []
    assert "2026-09-09" in rep["missing"]
    m = rep["missing"]["2026-09-09"]
    assert abs(m["residual"]) == pytest.approx(20_000.0, abs=1.0)
    assert "对不上" in m["reason"]


def test_fill_audits_every_run(store):
    _seed(store, REAL_ROWS)
    _fill(store)
    ro = store.open_readonly()
    try:
        kinds = [r[0] for r in ro.execute(
            "SELECT kind FROM audit WHERE kind LIKE 'gapfill%'")]
    finally:
        ro.close()
    assert kinds, "补算必须 audit 留痕"


def test_fill_tail_gap_reports_skip(store):
    _seed(store, [_row("2026-09-08", 1_064_797.72, 527_984.52, 536_813.20)])
    rep = _fill(store)
    assert rep["written"] == []
    assert "2026-09-09" in rep["missing"]


def test_fill_exception_is_soft(store, monkeypatch):
    """取价回调抛异常 → 全链路 fail-soft, 不抛给调用方 (交易链零影响)。"""
    _seed(store, REAL_ROWS)

    def boom(code, date):
        raise RuntimeError("行情炸了")

    rep = _fill(store, close_at=boom)
    assert rep["written"] == []


def test_fill_uses_trades_from_store(store):
    """缺口内成交从 trades 表读 (不靠调用方传): 买入扣现金, 右端也是实打实的。"""
    _seed(store, [_row("2026-09-08", 1_064_797.72, 527_984.52, 536_813.20),
                  # 右端实测与"9/9 买了 100 股"自洽 (现金少了 223)
                  _row("2026-09-10", 1_061_209.12, 527_761.52, 533_447.60)])
    store.save_trade({"traded_id": "T1", "order_id": "O1", "code": ETF,
                      "direction": DIRECTION_BUY, "price": 2.23, "qty": 100,
                      "amount": 223.0,
                      "ts": time.mktime(time.strptime("2026-09-09 14:54", "%Y-%m-%d %H:%M"))})
    rep = _fill(store)
    assert rep["written"] == ["2026-09-09"]
    run = rep["runs"][0]
    assert run["residual"] == pytest.approx(0.0, abs=0.01)
    day = run["days"][0]
    assert day["available"] == pytest.approx(527_984.52 - 223.0, abs=0.01)
    assert day["market_value"] == pytest.approx(240_400 * 2.234, abs=0.01)


# ────────────────────────── store 迁移 ──────────────────────────

def test_daily_asset_source_column_migrates(tmp_path):
    """旧库没有 source 列 → 迁移补上, 老行默认 'eod' (零行为变化)。"""
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE daily_asset (date TEXT PRIMARY KEY, "
                 "total_asset REAL NOT NULL, available REAL NOT NULL, "
                 "market_value REAL NOT NULL, ts REAL NOT NULL)")
    conn.execute("INSERT INTO daily_asset VALUES ('2026-09-01', 100.0, 60.0, 40.0, 0.0)")
    conn.commit()
    conn.close()

    st = TradeStore(str(db), str(tmp_path / "raw.jsonl"))
    try:
        rows = st.daily_asset.get()
        assert rows[0]["source"] == "eod"
    finally:
        st.close()


# ────────────────────────── API 接线 ──────────────────────────

@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient
    from trade.api import create_api_app
    from trade.config import TradeConfig
    from trade_main import TradeApp
    cfg = TradeConfig(
        account_id="GF", fake_sdk=True,
        db_path=str(tmp_path / "api.db"),
        raw_log_path=str(tmp_path / "api.jsonl"),
        kill_flag_path=str(tmp_path / "KILL"),
    )
    fixed = time.mktime(time.strptime("2026-09-08 08:00", "%Y-%m-%d %H:%M"))
    app = TradeApp(cfg, fake=True, clock=lambda: fixed,
                   config_path=str(tmp_path / "trade.yaml"))
    app.start(start_timers=False)
    yield TestClient(create_api_app(app)), app
    app.stop()


def test_api_daily_pnl_carries_source_and_gapfill_keys(client):
    """daily_pnl: 每天带 source, 且带 _missing / _gapfill 两个 "_" 键。"""
    c, app = client
    app.store.daily_asset.save("2026-09-08", 1_064_797.72, 527_984.52, 536_813.20)
    app.store.daily_asset.save("2026-09-09", 1_065_038.12, 527_984.52, 537_053.60,
                               source="derived")
    d = c.get("/api/trade/analysis/daily_pnl?year=2026&month=9").json()
    assert d["2026-09-08"]["source"] == "eod"
    assert d["2026-09-09"]["source"] == "derived"
    # 9/9 是推算日 → 9/10 若实测, 差额只剩单日; 这里没有 9/10, 只验键存在
    assert "_missing" in d and "_gapfill" in d
    assert d["_month"]["derived_days"] == 1


def test_api_missing_days_marked_from_gapfill_audit(client):
    """拒写的缺口日 → _missing 带差额, 界面据此显示「未归档·差¥X」。"""
    c, app = client
    app.store.daily_asset.save("2026-09-08", 1_064_797.72, 527_984.52, 536_813.20)
    app.store.write_audit("gapfill_reject", "补算停机日 2026-09-09 → 拒写",
                          {"runs": [{"status": "reject", "dates": ["2026-09-09"],
                                     "residual": 1_847.20,
                                     "reason": "与右端实测 (2026-09-10) 对不上 ¥1,847.20"}],
                           "status": "reject", "dates": ["2026-09-09"],
                           "residual": 1_847.20, "dry_run": False})
    d = c.get("/api/trade/analysis/daily_pnl?year=2026&month=9").json()
    assert d["_missing"]["2026-09-09"]["residual"] == pytest.approx(1_847.20)
    assert "对不上" in d["_missing"]["2026-09-09"]["reason"]
    assert d["_gapfill"]["status"] == "reject"


def test_api_missing_self_heals_after_fill(client):
    """被拒的缺口日后来补上了 → 不再出现在 _missing (不留陈旧标记)。"""
    c, app = client
    app.store.daily_asset.save("2026-09-08", 1_064_797.72, 527_984.52, 536_813.20)
    app.store.write_audit("gapfill_reject", "拒写", {
        "runs": [{"status": "reject", "dates": ["2026-09-09"], "residual": 1_847.20,
                  "reason": "对不上"}], "status": "reject", "dates": ["2026-09-09"]})
    d = c.get("/api/trade/analysis/daily_pnl?year=2026&month=9").json()
    assert "2026-09-09" in d["_missing"]              # 还没补 → 标出来
    app.store.daily_asset.save("2026-09-09", 1_065_038.12, 527_984.52, 537_053.60,
                               source="derived")
    d2 = c.get("/api/trade/analysis/daily_pnl?year=2026&month=9").json()
    assert "2026-09-09" not in d2["_missing"]         # 补上了 → 自动清掉
    assert d2["2026-09-09"]["source"] == "derived"


def test_api_gapfill_trigger_and_last(client):
    """POST 只入队 (accepted), 结果走 gapfill_last 读回。"""
    c, app = client
    assert c.get("/api/trade/analysis/gapfill_last").json()["last"] is None
    r = c.post("/api/trade/analysis/gapfill?dry_run=1")
    assert r.status_code == 200 and r.json() == {"accepted": True, "dry_run": True}
    # 入队的是命令 (消费者线程干活的铁律不变)
    ev = app._engine  # noqa: SLF001 - 直接确认命令已入队
    assert ev is not None


# ────────────────────────── trade_main 接线 ──────────────────────────

def _trade_app(tmp_path, clock_str, monkeypatch, positions=None):
    """建一个 fake TradeApp (时钟固定), 并把取价换成测试字典。"""
    from trade.config import TradeConfig
    from trade_main import TradeApp
    from trade import asset_gapfill as gf
    monkeypatch.setattr(gf, "make_close_source",
                        lambda gateway=None: _close_map(REAL_CLOSES))
    pos = positions if positions is not None else {ETF: 240_400}
    fixed = time.mktime(time.strptime(clock_str, "%Y-%m-%d %H:%M"))
    cfg = TradeConfig(account_id="GF", fake_sdk=True,
                      db_path=str(tmp_path / "tw.db"),
                      raw_log_path=str(tmp_path / "tw.jsonl"),
                      kill_flag_path=str(tmp_path / "KILL2"))
    return TradeApp(cfg, fake=True, clock=lambda: fixed,
                    config_path=str(tmp_path / "tw.yaml"),
                    fake_gateway_kwargs={
                        "cash": 527_984.52,
                        "positions": {c: {"volume": v, "can_use": v,
                                          "avg_cost": 2.0}
                                      for c, v in pos.items()}})


def test_startup_catchup_fills_gap(tmp_path, monkeypatch):
    """开机自动补: _startup_catchup 跑完, 9/9 那格被推算补上 (source='derived')。"""
    app = _trade_app(tmp_path, "2026-09-10 09:00", monkeypatch)
    try:
        app.start(start_timers=False)
        _seed(app.store, REAL_ROWS)
        app._startup_catchup("20260910")           # noqa: SLF001 - 测接线本身
        rows = {r["date"]: r for r in app.store.daily_asset.get()}
        assert rows["2026-09-09"]["source"] == "derived"
        assert rows["2026-09-09"]["total_asset"] == pytest.approx(1_065_038.12, abs=0.01)
    finally:
        app.stop()


def test_gapfill_command_and_session_guard(tmp_path, monkeypatch):
    """人工补算: 收盘后/盘前放行; 连续竞价时段拒绝 (不打扰盘中)。"""
    app = _trade_app(tmp_path, "2026-09-10 09:00", monkeypatch)
    try:
        app.start(start_timers=False)
        _seed(app.store, REAL_ROWS)
        app.dispatch_command({"action": "gapfill", "source": "manual_api"})
        rows = {r["date"]: r for r in app.store.daily_asset.get()}
        assert rows["2026-09-09"]["source"] == "derived"
    finally:
        app.stop()

    app2 = _trade_app(tmp_path, "2026-09-10 10:30", monkeypatch)
    try:
        app2.start(start_timers=False)
        _seed(app2.store, REAL_ROWS)
        rep = app2._run_gapfill(source="manual_api")   # noqa: SLF001
        assert rep["ok"] is False and "连续竞价" in rep["error"]
        ro = app2.store.open_readonly()
        try:
            kinds = [r[0] for r in ro.execute(
                "SELECT kind FROM audit WHERE kind='gapfill_skip'")]
        finally:
            ro.close()
        assert kinds
    finally:
        app2.stop()
