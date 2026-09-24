"""TradeStore 单元测试 (P1 地基, 2026-07-26).

锁住: 建表、订单/成交读写往返、traded_id 唯一约束、JSONL
append-only 落盘、WAL 下只读连接与写连接并发不互堵。
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.store import TradeStore


@pytest.fixture()
def store(tmp_path):
    s = TradeStore(tmp_path / "trade.db", tmp_path / "raw.jsonl")
    yield s
    s.close()


def test_tables_created(store):
    """七张表建仓即存在 (审计L4修复: 补 position_snapshot 守护)。"""
    tables = {
        r[0]
        for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"orders", "trades", "tier_state", "audit",
            "kill_flag", "reconcile_log", "position_snapshot"} <= tables


def test_tier_state_table_has_trade_date(store):
    """审计C1修复: tier_state 必须有 trade_date 列 (日期维度)。"""
    cols = [r[1] for r in store._conn.execute("PRAGMA table_info(tier_state)")]
    assert "trade_date" in cols


def test_tier_state_migration_rebuilds_old_table(tmp_path):
    """审计C1修复: 旧表 (无 trade_date) 打开即重建, 不炸不残留旧结构。"""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE tier_state (code TEXT PRIMARY KEY, "
                 "tiers_json TEXT NOT NULL, updated_ts REAL NOT NULL)")
    conn.commit()
    conn.close()
    s = TradeStore(db, tmp_path / "raw.jsonl")
    try:
        cols = [r[1] for r in s._conn.execute("PRAGMA table_info(tier_state)")]
        assert "trade_date" in cols
        s.tier_state.save("600519.SH", [0], "20260726")  # 重建后可正常写
        assert s.tier_state.load("20260726") == {"600519.SH": [0]}
    finally:
        s.close()


def test_wal_mode_enabled(store):
    """WAL 模式必须开启 (只读连接并发读的前提)。"""
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_save_order_roundtrip_and_upsert(store):
    """订单落库可读回; 同 order_id 再存是更新不是重复。"""
    store.save_order({"order_id": "O1", "code": "600519.SH", "direction": 23,
                      "price": 1700.0, "qty": 100, "status": 50, "remark": "V0726-1A"})
    store.save_order({"order_id": "O1", "code": "600519.SH", "direction": 23,
                      "price": 1700.0, "qty": 100, "status": 56,
                      "filled_qty": 100, "remark": "V0726-1A"})
    rows = store._conn.execute(
        "SELECT status, filled_qty, remark FROM orders WHERE order_id='O1'"
    ).fetchall()
    assert rows == [(56, 100, "V0726-1A")]


def test_save_order_created_ts_conflict_rules(store):
    """2026-08-10 (泰山石油事件): QMT 复用 order_id 时,
    显式提供 created_ts (新单下单时刻/QMT order_time) 必须覆盖旧值;
    纯状态回写 (不显式提供) 不得把 created_ts 盖成回写时刻。"""
    old_ts = 1000.0
    store.save_order({"order_id": "O9", "code": "000721.SZ", "direction": 23,
                      "price": 5.0, "qty": 100, "status": 56,
                      "created_ts": old_ts})
    # 纯状态回写 (如 _on_order_error 不传 created_ts): 保留原创建时间
    store.save_order({"order_id": "O9", "code": "000721.SZ", "direction": 23,
                      "price": 5.0, "qty": 100, "status": 57})
    row = store._conn.execute(
        "SELECT created_ts FROM orders WHERE order_id='O9'").fetchone()
    assert row[0] == old_ts
    # order_id 复用: 新单显式带下单时刻 → 覆盖旧值
    new_ts = 2000.0
    store.save_order({"order_id": "O9", "code": "000554.SZ", "direction": 24,
                      "price": 6.43, "qty": 1500, "status": 50,
                      "created_ts": new_ts})
    row = store._conn.execute(
        "SELECT created_ts, code FROM orders WHERE order_id='O9'").fetchone()
    assert row == (new_ts, "000554.SZ")
    # 之后再来的纯状态回写: 不动新创建时间
    store.save_order({"order_id": "O9", "code": "000554.SZ", "direction": 24,
                      "price": 6.43, "qty": 1500, "status": 56,
                      "filled_qty": 1500})
    row = store._conn.execute(
        "SELECT created_ts FROM orders WHERE order_id='O9'").fetchone()
    assert row[0] == new_ts


def test_save_trade_roundtrip(store):
    store.save_trade({"traded_id": "T1", "order_id": "O1", "code": "600519.SH",
                      "direction": 23, "price": 1700.0, "qty": 100, "ts": 1.0})
    row = store._conn.execute(
        "SELECT order_id, amount FROM trades WHERE traded_id='T1'"
    ).fetchone()
    assert row == ("O1", 1700.0 * 100)


def test_traded_id_unique_constraint(store):
    """重复 traded_id 插入必须炸 IntegrityError (幂等的物理底线)。"""
    rec = {"traded_id": "T1", "order_id": "O1", "code": "600519.SH",
           "direction": 23, "price": 10.0, "qty": 100}
    store.save_trade(rec)
    with pytest.raises(sqlite3.IntegrityError):
        store.save_trade(rec)


def test_tier_state_roundtrip(store):
    """档位状态落库/读回 (按当日过滤); 重存覆盖, 读回升序。
    审计C1修复: 昨日标记不混入当日查询 (日期维度)。"""
    store.tier_state.save("600519.SH", {2, 0}, "20260726")
    store.tier_state.save("000001.SZ", [1], "20260726")
    assert store.tier_state.load("20260726") == {
        "600519.SH": [0, 2], "000001.SZ": [1],
    }
    store.tier_state.save("600519.SH", [0, 1, 2], "20260726")
    assert store.tier_state.load("20260726")["600519.SH"] == [0, 1, 2]
    # 昨日标记留痕但不出现在当日查询里
    store.tier_state.save("600519.SH", [0], "20260725")
    assert store.tier_state.load("20260726")["600519.SH"] == [0, 1, 2]
    assert store.tier_state.load("20260725") == {"600519.SH": [0]}


def test_load_today_trade_ids(store):
    """审计H4修复: 当日 traded_id 全量返回, 昨日的不返回。"""
    today = time.strftime("%Y%m%d")
    store.save_trade({"traded_id": "T-today", "order_id": "O1",
                      "code": "600519.SH", "direction": 23,
                      "price": 10.0, "qty": 100, "ts": time.time()})
    store.save_trade({"traded_id": "T-old", "order_id": "O2",
                      "code": "600519.SH", "direction": 23,
                      "price": 10.0, "qty": 100, "ts": time.time() - 86400 * 3})
    assert store.load_today_trade_ids(today) == {"T-today"}


def test_kill_flag_roundtrip(store):
    """默认未激活; 置位/清位往返。"""
    assert store.get_kill_flag()["active"] is False
    store.set_kill_flag(True, "人工按钮")
    flag = store.get_kill_flag()
    assert flag["active"] is True and flag["source"] == "人工按钮"
    store.set_kill_flag(False, "")
    assert store.get_kill_flag()["active"] is False


def test_audit_written(store):
    store.write_audit("risk_reject", "[kill_switch] 急停激活",
                      {"code": "600519.SH"})
    row = store._conn.execute(
        "SELECT kind, message, detail_json FROM audit"
    ).fetchone()
    assert row[0] == "risk_reject"
    assert "急停激活" in row[1]
    assert json.loads(row[2]) == {"code": "600519.SH"}


def test_append_raw_jsonl(store, tmp_path):
    """JSONL append-only: 每行一个合法 JSON, 内容即 payload。
    2026-08-06 异步化 (HIGH#3): append 只入队, flush_raw 后文件可见。"""
    store.append_raw({"kind": "order", "order_id": "O1"})
    store.append_raw({"kind": "trade", "traded_id": "T1"})
    assert store.flush_raw()
    lines = (tmp_path / "raw.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"kind": "order", "order_id": "O1"}
    assert json.loads(lines[1]) == {"kind": "trade", "traded_id": "T1"}


def test_readonly_connection_reads_while_writer_open(store):
    """WAL 语义: 写连接未关闭时, 独立只读连接能读到已提交数据。"""
    store.save_order({"order_id": "O9", "code": "000001.SZ", "direction": 24,
                      "price": 10.0, "qty": 100, "status": 50})
    ro = store.open_readonly()
    try:
        row = ro.execute(
            "SELECT code, status FROM orders WHERE order_id='O9'"
        ).fetchone()
        assert row == ("000001.SZ", 50)
        # 只读连接只读: 写操作必须被拒
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO audit (ts, kind, message) VALUES (0,'x','y')")
    finally:
        ro.close()


# ── 2026-07-30: trades.source 列 (系统单/手工单区分) ──────────

def test_save_trade_source_default_and_manual(store):
    """source 缺省 system (回调链路); 对账认领传 manual。"""
    store.save_trade({"traded_id": "T-sys", "order_id": "O1",
                      "code": "600519.SH", "direction": 23,
                      "price": 10.0, "qty": 100})
    store.save_trade({"traded_id": "T-man", "order_id": "0",
                      "code": "159226.SZ", "direction": 24,
                      "price": 1.2, "qty": 100, "source": "manual"})
    rows = dict(store._conn.execute(
        "SELECT traded_id, source FROM trades").fetchall())
    assert rows == {"T-sys": "system", "T-man": "manual"}


def test_trades_source_migration_adds_column(tmp_path):
    """旧库 (trades 无 source 列) 打开即 ALTER 补列, 历史行默认空串
    (未知, 不回填), 新写入正常。"""
    db = tmp_path / "old_trades.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trades (traded_id TEXT PRIMARY KEY, order_id TEXT NOT NULL,"
        " code TEXT NOT NULL, direction INTEGER NOT NULL, price REAL NOT NULL,"
        " qty INTEGER NOT NULL, amount REAL NOT NULL, ts REAL NOT NULL)")
    conn.execute("INSERT INTO trades VALUES ('T-old','O0','600519.SH',23,1.0,1,1.0,1.0)")
    conn.commit()
    conn.close()
    s = TradeStore(db, tmp_path / "raw.jsonl")
    try:
        cols = [r[1] for r in s._conn.execute("PRAGMA table_info(trades)")]
        assert "source" in cols
        row = s._conn.execute(
            "SELECT source FROM trades WHERE traded_id='T-old'").fetchone()
        assert row == ("",)                       # 历史行留白不回填
        s.save_trade({"traded_id": "T-new", "order_id": "O1",
                      "code": "600519.SH", "direction": 23,
                      "price": 10.0, "qty": 100, "source": "manual"})
        assert s._conn.execute(
            "SELECT source FROM trades WHERE traded_id='T-new'"
        ).fetchone() == ("manual",)
    finally:
        s.close()


# ── 2026-07-31: trades.reason 列 (成交原因, 成交记录页"原因"列) ──

def test_save_trade_reason_roundtrip(store):
    """reason 缺省空串 (买入/无 fill context); 卖出带 ctx 时落原因串。"""
    store.save_trade({"traded_id": "T-buy", "order_id": "O1",
                      "code": "600519.SH", "direction": 23,
                      "price": 10.0, "qty": 100})
    store.save_trade({"traded_id": "T-sell", "order_id": "O2",
                      "code": "300996.SZ", "direction": 24,
                      "price": 33.8, "qty": 100, "reason": "阶梯止盈·档1"})
    rows = dict(store._conn.execute(
        "SELECT traded_id, reason FROM trades").fetchall())
    assert rows == {"T-buy": "", "T-sell": "阶梯止盈·档1"}


def test_trades_reason_migration_adds_column(tmp_path):
    """旧库 (trades 无 reason 列) 打开即 ALTER 补列, 历史行默认空串。"""
    db = tmp_path / "old_trades_no_reason.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trades (traded_id TEXT PRIMARY KEY, order_id TEXT NOT NULL,"
        " code TEXT NOT NULL, direction INTEGER NOT NULL, price REAL NOT NULL,"
        " qty INTEGER NOT NULL, amount REAL NOT NULL, ts REAL NOT NULL,"
        " source TEXT NOT NULL DEFAULT '')")
    conn.execute(
        "INSERT INTO trades VALUES ('T-old','O0','600519.SH',23,1.0,1,1.0,1.0,'')")
    conn.commit()
    conn.close()
    s = TradeStore(db, tmp_path / "raw.jsonl")
    try:
        cols = [r[1] for r in s._conn.execute("PRAGMA table_info(trades)")]
        assert "reason" in cols
        assert s._conn.execute(
            "SELECT reason FROM trades WHERE traded_id='T-old'"
        ).fetchone() == ("",)                        # 历史行留白不回填
    finally:
        s.close()


def test_reason_from_ctx():
    """trade_main._reason_from_ctx: 优先 detail 全文; 旧格式 label+档位;
    空 ctx 空串。"""
    from trade_main import _reason_from_ctx
    # 2026-07-31: detail (自然语言全文) 优先于 label+tier 拼装
    assert _reason_from_ctx({"label": "阶梯止盈", "tier": 0,
                             "detail": "阶梯止盈·档1: 预埋价 33.80 "
                                       "(成本 30.73 +10%), 卖 30%"}) == \
        "阶梯止盈·档1: 预埋价 33.80 (成本 30.73 +10%), 卖 30%"
    assert _reason_from_ctx({"label": "阶梯止盈", "tier": 0}) == "阶梯止盈·档1"
    assert _reason_from_ctx({"label": "阶梯止盈", "tier": 2}) == "阶梯止盈·档3"
    assert _reason_from_ctx({"label": "移动止盈"}) == "移动止盈"
    assert _reason_from_ctx({}) == ""


def test_orders_status_msg_migration_and_roundtrip(tmp_path):
    """2026-08-07 (0807 废单事件): orders 表 status_msg 列 —— 老库 (无该列)
    打开即自动迁移; 废单原因写入/读出一致。"""
    import sqlite3 as _sq
    db = tmp_path / "old.db"
    conn = _sq.connect(str(db))
    conn.execute("""CREATE TABLE orders (
        order_id TEXT PRIMARY KEY, remark TEXT NOT NULL DEFAULT '',
        code TEXT NOT NULL, direction INTEGER NOT NULL, price REAL NOT NULL,
        qty INTEGER NOT NULL, filled_qty INTEGER NOT NULL DEFAULT 0,
        status INTEGER NOT NULL, created_ts REAL NOT NULL,
        updated_ts REAL NOT NULL)""")
    conn.commit()
    conn.close()
    s = TradeStore(db, tmp_path / "raw.jsonl")      # 打开即触发迁移
    s.save_order({"order_id": "O1", "code": "600519.SH", "direction": 23,
                  "price": 10.0, "qty": 100, "status": 57,
                  "status_msg": "无科创板交易权限"})
    row = s._conn.execute(
        "SELECT status, status_msg FROM orders WHERE order_id='O1'").fetchone()
    s.close()
    assert row == (57, "无科创板交易权限")


def test_load_today_trades_detail_with_pnl(store):
    """2026-08-07: 当日成交明细含 pnl_amount/pnl_pct (日报数据源), ts 升序, 跨日不返。"""
    import datetime as _dt
    today = _dt.datetime.now().strftime("%Y-%m-%d")
    base = time.time()
    store.save_trade({"traded_id": "T-B", "order_id": "O-B", "code": "300750.SZ",
                      "direction": 23, "price": 10.0, "qty": 100, "amount": 1000.0,
                      "ts": base, "source": "system"})
    store.save_trade({"traded_id": "T-S", "order_id": "O-S", "code": "300750.SZ",
                      "direction": 24, "price": 11.0, "qty": 100, "amount": 1100.0,
                      "ts": base + 1, "source": "system", "reason": "移动止盈",
                      "pnl_amount": 100.0, "pnl_pct": 10.0})
    det = store.load_today_trades_detail(today)
    assert len(det) == 2
    assert det[0]["traded_id"] == "T-B"          # ts 升序
    assert det[1]["pnl_amount"] == 100.0 and det[1]["pnl_pct"] == 10.0
    assert det[0]["pnl_amount"] == 0.0           # 买入默认 0
    yest = (_dt.datetime.now() - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    assert store.load_today_trades_detail(yest) == []   # 跨日不返


def test_daily_report_upsert_and_latest(store):
    """daily_report 表 UPSERT + load_latest 取最近日期 (2026-08-07)。"""
    store.daily_report.save("2026-08-01", {"day_pnl": 100.0})
    store.daily_report.save("2026-08-07", {"day_pnl": 200.0})
    store.daily_report.save("2026-08-03", {"day_pnl": 150.0})
    assert store.daily_report.load("2026-08-07") == {"day_pnl": 200.0}
    assert store.daily_report.load_latest() == {"day_pnl": 200.0}   # 最新日期
    store.daily_report.save("2026-08-07", {"day_pnl": 999.0})       # UPSERT
    assert store.daily_report.load("2026-08-07") == {"day_pnl": 999.0}
    assert store.daily_report.load("2099-01-01") is None
