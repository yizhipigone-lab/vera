"""trade/store.py — SQLite (WAL) 持久化 + JSONL 原始回报落盘。

设计意图:
    两类存储各管一段: JSONL 是 append-only 原始回报, 先落盘再处理,
    崩溃可重放 (计划书 §5.4); SQLite 是结构化状态, WAL 模式下
    写连接 (消费者线程专用) 与只读连接 (Web 读快照) 互不阻塞。
    替代 QP 的自研 WAL 叠 DuckDB —— 那是过度设计。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from trade.book import DIRECTION_BUY
from utils.logger import get_logger

_logger = get_logger("trade.store")

# 审计C1修复: tier_state 加日期维度 (主键 (code, trade_date))。
# 预埋跳过只认当日标记, 昨日标记不阻碍今日重挂 (计划书 §5.1 "日终自动
# 过期, 次日重挂"); 历史不清, 留痕供复盘。DDL 独立成常量供 migration 重建用。
_TIER_STATE_DDL = """
CREATE TABLE IF NOT EXISTS tier_state (
    code        TEXT NOT NULL,
    trade_date  TEXT NOT NULL,      -- YYYYMMDD
    tiers_json  TEXT NOT NULL,      -- 当日已预埋阶梯止盈档位索引列表
    updated_ts  REAL NOT NULL,
    PRIMARY KEY (code, trade_date)
);
"""

_SCHEMA = _TIER_STATE_DDL + """
CREATE TABLE IF NOT EXISTS daily_asset (
    date        TEXT PRIMARY KEY,   -- YYYY-MM-DD
    total_asset REAL NOT NULL,      -- 总资产
    available   REAL NOT NULL,      -- 可用资金
    market_value REAL NOT NULL,     -- 持仓市值
    ts          REAL NOT NULL       -- 写入时间戳
);
CREATE TABLE IF NOT EXISTS orders (
    order_id    TEXT PRIMARY KEY,
    remark      TEXT NOT NULL DEFAULT '',
    code        TEXT NOT NULL,
    direction   INTEGER NOT NULL,
    price       REAL NOT NULL,
    qty         INTEGER NOT NULL,
    filled_qty  INTEGER NOT NULL DEFAULT 0,
    status      INTEGER NOT NULL,
    created_ts  REAL NOT NULL,
    updated_ts  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    traded_id   TEXT PRIMARY KEY,   -- 唯一约束即幂等底线: 重复回报插入即炸
    order_id    TEXT NOT NULL,
    code        TEXT NOT NULL,
    direction   INTEGER NOT NULL,
    price       REAL NOT NULL,
    qty         INTEGER NOT NULL,
    amount      REAL NOT NULL,
    ts          REAL NOT NULL,
    source      TEXT NOT NULL DEFAULT '', -- 2026-07-30: system=系统单 / manual=手工单
                                          -- (券商端/手机端, 对账认领); 历史行空=未知
    reason      TEXT NOT NULL DEFAULT ''  -- 2026-07-31: 成交原因 (阶梯止盈·档1/移动止盈/
                                          -- 硬止损/人工卖出...), 来自下单侧 fill context;
                                          -- 无 ctx (部成第二笔/手工单/买入) 空串
);
CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,
    message     TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS kill_flag (
    id          INTEGER PRIMARY KEY CHECK (id = 1),  -- 单行表
    active      INTEGER NOT NULL,
    source      TEXT NOT NULL DEFAULT '',
    ts          REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reconcile_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    level       TEXT NOT NULL,
    code        TEXT NOT NULL DEFAULT '',
    expected    TEXT NOT NULL DEFAULT '',
    actual      TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}'
);
-- 昨仓快照 (EOD 归档 / 启动对账写入): 对账 C 方 "成交还原" 的基准,
-- 不存它重启后 C 方无从谈起 (计划书 §5.3)
CREATE TABLE IF NOT EXISTS position_snapshot (
    code        TEXT PRIMARY KEY,
    volume      INTEGER NOT NULL,
    can_use     INTEGER NOT NULL,
    avg_cost    REAL NOT NULL,
    ts          REAL NOT NULL
);
"""


class TradeStore:
    """单写连接 + JSONL 落盘。写连接由消费者线程专用, 内部锁兜底。"""

    def __init__(self, db_path: str | Path, raw_log_path: str | Path):
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: 连接在主线程创建、消费者线程使用;
        # 纪律上只允许消费者线程写, 内部锁是防手滑的兜底, 不是并发设计
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate_tier_state()
        self._migrate_trades_source()
        self._migrate_trades_reason()
        self._lock = threading.Lock()

        raw_path = Path(raw_log_path)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        self._raw_fp = open(raw_path, "a", encoding="utf-8")

    # ── 写接口 (消费者线程) ─────────────────────────────────────

    def save_order(self, record: dict) -> None:
        """按 order_id upsert 委托记录 (回报乱序/重复都以最新状态覆盖)。
        2026-08-03 修复: QMT order_id 跨会话复用 (同 order_id 可能先分配给
        000721, 下次分配给 300158), ON CONFLICT 必须更新全部业务字段,
        否则新订单静默合并进旧记录的 code/price/qty。"""
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO orders
                   (order_id, remark, code, direction, price, qty,
                    filled_qty, status, created_ts, updated_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(order_id) DO UPDATE SET
                    remark=excluded.remark,
                    code=excluded.code,
                    direction=excluded.direction,
                    price=excluded.price,
                    qty=excluded.qty,
                    filled_qty=excluded.filled_qty,
                    status=excluded.status,
                    updated_ts=excluded.updated_ts""",
                (
                    record["order_id"], record.get("remark", ""),
                    record["code"], record["direction"], record["price"],
                    record["qty"], record.get("filled_qty", 0),
                    record["status"],
                    record.get("created_ts", now), now,
                ),
            )

    def update_order_filled(self, order_id: str, qty: int) -> None:
        """成交回报回写订单进度 (2026-07-30, 600808 事件): filled_qty 累加,
        满量置已成(56), 未满置部成(55)。

        原实现订单状态只靠 QMT 订单状态回调刷新 — 回调缺失时 orders 表
        永远停在"已报/成交0" (成交已落 trades 表), 页面显示陈旧。此处以
        成交回报为硬事实直接回写, 与回调路径互补 (QMT 状态回调来后会
        以同样终态覆盖, 幂等无冲突)。

        2026-08-01 M4: 终态守卫 —— 已终态的订单 (部撤53/已撤54/已成56/废单57)
        跳过更新, 防成交回报乱序把"已成"回退为"部成"。
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "SELECT status FROM orders WHERE order_id = ?", (order_id,))
            row = cur.fetchone()
            if row and row[0] in (53, 54, 56, 57):
                return  # 终态不可逆
            self._conn.execute(
                """UPDATE orders SET
                       filled_qty = MIN(qty, filled_qty + ?),
                       status = CASE WHEN MIN(qty, filled_qty + ?) >= qty
                                     THEN 56 ELSE 55 END,
                       updated_ts = ?
                   WHERE order_id = ?""",
                (qty, qty, time.time(), order_id),
            )

    def load_open_orders(self) -> dict:
        """加载全部非终态委托 (2026-07-30: 重启恢复在途订单簿 —
        book.restore 用它重建 _orders, 撤单流水线重启后仍能找到要撤的单;
        不带终态 (部撤53/已撤54/已成56/废单57)。"""
        cur = self._conn.execute(
            "SELECT order_id, remark, code, direction, price, qty, filled_qty, "
            "status, created_ts FROM orders WHERE status NOT IN (53, 54, 56, 57)")
        return {r[0]: {"order_id": r[0], "remark": r[1], "code": r[2],
                       "direction": r[3], "price": r[4], "qty": r[5],
                       "filled_qty": r[6], "status": r[7], "created_ts": r[8]}
                for r in cur.fetchall()}

    def save_trade(self, record: dict) -> None:
        """落成交记录。traded_id 重复 → sqlite3.IntegrityError 上抛,
        调用方 (book 层) 负责先判幂等, 这里是最后一道物理约束。
        source: system=系统单 (回调链路, 默认) / manual=手工单 (对账认领,
        2026-07-30 —— 前端成交记录要区分"我手工卖的"和"系统卖的")。
        reason: 成交原因 (2026-07-31, 如 "阶梯止盈·档1"/"移动止盈"/"人工卖出"),
        由 trade_main._on_trade 从 executor fill context 组装; 无 ctx 时空串。"""
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO trades
                   (traded_id, order_id, code, direction, price, qty, amount, ts,
                    source, reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    record["traded_id"], record["order_id"], record["code"],
                    record["direction"], record["price"], record["qty"],
                    record.get("amount", record["price"] * record["qty"]),
                    record.get("ts", time.time()),
                    record.get("source", "system"),
                    record.get("reason", ""),
                ),
            )

    def save_tier_state(self, code: str, tiers: list[int] | set[int],
                        trade_date: str) -> None:
        """落某票当日已预埋的阶梯止盈档位 (乐观标记法)。
        审计C1修复: 必须带 trade_date —— 隔夜后昨日标记不得阻碍今日预埋。"""
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO tier_state (code, trade_date, tiers_json, updated_ts)
                   VALUES (?,?,?,?)
                   ON CONFLICT(code, trade_date) DO UPDATE SET
                    tiers_json=excluded.tiers_json,
                    updated_ts=excluded.updated_ts""",
                (code, trade_date, json.dumps(sorted(tiers)), time.time()),
            )

    def load_tier_states(self, trade_date: str) -> dict[str, list[int]]:
        """按当日过滤读回档位状态 (启动恢复用; 历史日期留痕不返回)。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT code, tiers_json FROM tier_state WHERE trade_date = ?",
                (trade_date,),
            ).fetchall()
        return {code: json.loads(tj) for code, tj in rows}

    def load_today_trade_ids(self, date_str: str) -> set[str]:
        """当日全部 traded_id (审计H4修复: 启动回填幂等集合,
        防重启后成交回报重推双扣持仓)。"""
        day_start = time.mktime(time.strptime(date_str, "%Y%m%d"))
        with self._lock:
            rows = self._conn.execute(
                "SELECT traded_id FROM trades WHERE ts >= ?", (day_start,),
            ).fetchall()
        return {r[0] for r in rows}

    def _migrate_tier_state(self) -> None:
        """审计C1修复: 旧表 (无 trade_date 列) 直接重建。
        data/trade/ 下是开发库, 旧标记可弃 —— 重建丢的是"已预埋记录",
        最坏后果是当日重复挂一轮预埋单, 券商端冻结兜底, 可接受。"""
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(tier_state)")]
        if cols and "trade_date" not in cols:
            with self._conn:
                self._conn.execute("DROP TABLE tier_state")
                self._conn.execute(_TIER_STATE_DDL)

    def _migrate_trades_source(self) -> None:
        """2026-07-30: trades 表加 source 列 (区分系统单/手工单)。
        ALTER ADD COLUMN 幂等演进, 历史行默认空串 (未知, 不回填 —
        手工/系统的判定依赖当时的认领上下文, 事后猜不如留白)。"""
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(trades)")]
        if cols and "source" not in cols:
            with self._conn:
                self._conn.execute(
                    "ALTER TABLE trades ADD COLUMN source TEXT NOT NULL DEFAULT ''")

    def _migrate_trades_reason(self) -> None:
        """2026-07-31: trades 表加 reason 列 (成交原因, 成交记录页展示)。
        同 source 的幂等演进, 历史行默认空串 (下单时的 fill context
        已随进程消散, 事后无从回填, 留白)。"""
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(trades)")]
        if cols and "reason" not in cols:
            with self._conn:
                self._conn.execute(
                    "ALTER TABLE trades ADD COLUMN reason TEXT NOT NULL DEFAULT ''")

    def write_audit(self, kind: str, message: str, detail: dict | None = None) -> None:
        """审计流水 (风控拒绝/对账告警等)。只增不改。"""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO audit (ts, kind, message, detail_json) VALUES (?,?,?,?)",
                (time.time(), kind, message, json.dumps(detail or {}, ensure_ascii=False)),
            )

    def set_kill_flag(self, active: bool, source: str = "") -> None:
        """写 DB 层急停标志 (三重态之一)。单行 upsert。"""
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO kill_flag (id, active, source, ts) VALUES (1,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                    active=excluded.active, source=excluded.source, ts=excluded.ts""",
                (1 if active else 0, source, time.time()),
            )

    def get_kill_flag(self) -> dict:
        """读 DB 层急停标志。无记录视为未激活。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT active, source, ts FROM kill_flag WHERE id = 1"
            ).fetchone()
        if row is None:
            return {"active": False, "source": "", "ts": 0.0}
        return {"active": bool(row[0]), "source": row[1], "ts": row[2]}

    def write_reconcile(self, level: str, code: str, expected: str,
                        actual: str, detail: dict | None = None) -> None:
        """对账结果落 reconcile_log。只告警留痕, 永不回写持仓 (铁律 1)。"""
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO reconcile_log
                   (ts, level, code, expected, actual, detail_json)
                   VALUES (?,?,?,?,?,?)""",
                (time.time(), level, code, expected, actual,
                 json.dumps(detail or {}, ensure_ascii=False)),
            )

    def save_position_snapshot(self, positions: dict | list) -> None:
        """EOD 归档 / 启动对账: 全量覆盖昨仓快照。
        positions: {code: {volume, can_use, avg_cost}} 或同形 dict 列表。"""
        items = positions.items() if isinstance(positions, dict) else (
            (p["code"], p) for p in positions)
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM position_snapshot")
            self._conn.executemany(
                """INSERT INTO position_snapshot (code, volume, can_use, avg_cost, ts)
                   VALUES (?,?,?,?,?)""",
                [(code, int(p["volume"]), int(p.get("can_use", p["volume"])),
                  float(p.get("avg_cost", 0.0)), time.time())
                 for code, p in items],
            )

    def load_position_snapshot(self) -> dict[str, dict]:
        """读昨仓快照 (对账 C 方基准)。含 ts —— M5 的 EOD 补偿
        需要判断快照是否是当日的。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT code, volume, can_use, avg_cost, ts FROM position_snapshot"
            ).fetchall()
        return {code: {"volume": v, "can_use": cu, "avg_cost": c, "ts": ts}
                for code, v, cu, c, ts in rows}

    def bought_codes_since(self, ts: float) -> set[str]:
        """某时刻以来的买入代码集合 (尾盘自动买入"今日已买过"判重)。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT code FROM trades WHERE ts >= ? AND direction = ?",
                (ts, DIRECTION_BUY),
            ).fetchall()
        return {r[0] for r in rows}

    def net_trades_since(self, ts: float) -> dict[str, int]:
        """某时刻以来的成交净额 (买正卖负), 对账 C 方还原用。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT code, direction, SUM(qty) FROM trades WHERE ts >= ? "
                "GROUP BY code, direction", (ts,),
            ).fetchall()
        net: dict[str, int] = {}
        for code, direction, qty in rows:
            net[code] = net.get(code, 0) + (
                qty if direction == DIRECTION_BUY else -qty)
        return net

    # ── 读接口 (Web/监控) ───────────────────────────────────────

    def open_readonly(self) -> sqlite3.Connection:
        """独立只读连接 (WAL 下与写连接并发不互堵), 供 Web 读快照。
        调用方负责 close。"""
        uri = f"file:{Path(self._db_path).as_posix()}?mode=ro"
        return sqlite3.connect(uri, uri=True)

    # ── 原始回报 ────────────────────────────────────────────────

    def append_raw(self, payload: dict) -> None:
        """JSONL append-only 落盘 + flush。
        审计M10修复(定位改写): 这是"先落盘再处理"的审计/复盘留痕,
        不是崩溃重放机制 —— 恢复走 QMT 全量对账 + 当日成交回填幂等集合,
        JSONL 不提供 replay。"""
        with self._lock:
            self._raw_fp.write(
                json.dumps(payload, ensure_ascii=False, default=str) + "\n"
            )
            self._raw_fp.flush()

    # ── 每日资产快照 (分析 Tab 净值曲线数据源) ──────────────

    def save_daily_asset(self, date: str, total_asset: float,
                         available: float, market_value: float) -> None:
        """EOD 日终资产快照。date=YYYY-MM-DD。幂等 (冲突覆盖)。"""
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO daily_asset (date, total_asset, available,
                   market_value, ts) VALUES (?,?,?,?,?)
                   ON CONFLICT(date) DO UPDATE SET
                    total_asset=excluded.total_asset,
                    available=excluded.available,
                    market_value=excluded.market_value,
                    ts=excluded.ts""",
                (date, total_asset, available, market_value, time.time()),
            )

    def get_daily_assets(self, start: str = "", end: str = "") -> list[dict]:
        """读日终资产序列 (YYYY-MM-DD)。缺省 start/end = 全部。"""
        with self._lock:
            if start and end:
                rows = self._conn.execute(
                    "SELECT date, total_asset, available, market_value, ts "
                    "FROM daily_asset WHERE date >= ? AND date <= ? "
                    "ORDER BY date ASC", (start, end),
                ).fetchall()
            elif start:
                rows = self._conn.execute(
                    "SELECT date, total_asset, available, market_value, ts "
                    "FROM daily_asset WHERE date >= ? ORDER BY date ASC",
                    (start,),
                ).fetchall()
            elif end:
                rows = self._conn.execute(
                    "SELECT date, total_asset, available, market_value, ts "
                    "FROM daily_asset WHERE date <= ? ORDER BY date ASC",
                    (end,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT date, total_asset, available, market_value, ts "
                    "FROM daily_asset ORDER BY date ASC",
                ).fetchall()
        return [{"date": r[0], "total_asset": r[1], "available": r[2],
                 "market_value": r[3], "ts": r[4]} for r in rows]

    def get_daily_asset_count(self) -> int:
        """daily_asset 行数 (判断有无历史数据)。"""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM daily_asset").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            finally:
                self._raw_fp.close()
