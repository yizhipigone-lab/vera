"""trade/store.py — SQLite (WAL) 持久化 + JSONL 原始回报落盘。

设计意图:
    两类存储各管一段: JSONL 是 append-only 原始回报留痕 (2026-08-06
    审计 HIGH#3 起异步落盘 —— 回调线程只入队, 专职 writer 线程批量写盘;
    M10 定位: 审计/复盘用, 不是崩溃重放机制, 恢复走 QMT 全量对账);
    SQLite 是结构化状态, WAL 模式下写连接 (消费者线程专用) 与只读连接
    (Web 读快照) 互不阻塞。
    替代 QP 的自研 WAL 叠 DuckDB —— 那是过度设计。
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import threading
import time
from pathlib import Path

from scheduler.trading_calendar import is_trading_day as _cal_is_trading_day
from trade.book import DIRECTION_BUY
from trade.decision_codes import ACTION_RANK, action_of as _action_of
from utils.logger import get_logger

_logger = get_logger("trade.store")

# 原始回报 JSONL 写盘线程已迁独立模块 trade/raw_log.py (治理III W4-f);
# re-export 保持既有 `from trade.store import _RawLogWriter` 引用零改动
from trade.raw_log import _RawLogWriter  # noqa: E402


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

# 当日决策台账 (2026-09-18): 一天 × 一条策略 × 一个对象 = 一行, 固定回答六件事
# —— 谁、哪天、做了什么、为什么 (原因码 + 大白话)、凭什么 (当时的数字)、关联哪几笔单。
# 主键 (日期, 策略, 对象) 天然幂等: 同一天定时 + 人工各跑一次只覆盖成最新, 不堆重复行。
# DDL 独立成常量是刻意的 —— 离线回填工具 (tools/backfill_daily_decision.py) 直接
# 引用同一份常量, 实时与回填不可能建出两张不一样的表 (单一真相源)。
DAILY_DECISION_DDL = """
CREATE TABLE IF NOT EXISTS daily_decision (
    trade_date    TEXT NOT NULL,   -- YYYY-MM-DD (与 daily_asset / daily_report 同口径)
    strategy      TEXT NOT NULL,   -- rotation | auto_buy | exit | ladder | system
    subject       TEXT NOT NULL,   -- '份1' | 标的代码 | 'pool' | 'all'
    action        TEXT NOT NULL,   -- BUY | SELL | HOLD | FAIL | INFO (5 种)
    reason_code   TEXT NOT NULL,   -- 枚举, 见 trade/decision_codes.py
    reason_text   TEXT NOT NULL,   -- 一句大白话 (策略当场写下, 绝不许事后编)
    evidence_json TEXT NOT NULL DEFAULT '{}',  -- 当时的数字 (事后复核用)
    trade_ids     TEXT NOT NULL DEFAULT '',    -- 逗号分隔的成交号/委托号
    source        TEXT NOT NULL,   -- live | backfill_exact | backfill_text | inferred
    updated_ts    REAL NOT NULL,
    PRIMARY KEY (trade_date, strategy, subject)
);
CREATE INDEX IF NOT EXISTS idx_daily_decision_date ON daily_decision(trade_date);
"""

_SCHEMA = _TIER_STATE_DDL + DAILY_DECISION_DDL + """
CREATE TABLE IF NOT EXISTS daily_asset (
    date        TEXT PRIMARY KEY,   -- YYYY-MM-DD
    total_asset REAL NOT NULL,      -- 总资产
    available   REAL NOT NULL,      -- 可用资金
    market_value REAL NOT NULL,     -- 持仓市值
    ts          REAL NOT NULL,      -- 写入时间戳
    source      TEXT NOT NULL DEFAULT 'eod'
                -- 2026-09-10 停机日补算: 'eod'=QMT 实测日终 / 'derived'=推算
                -- (程序没开机那天由 trade/asset_gapfill 用收盘价推出来的)
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
    status_msg  TEXT NOT NULL DEFAULT '', -- 2026-08-07: 委托状态描述 (废单原因, XtOrder.status_msg)
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
    reason      TEXT NOT NULL DEFAULT '', -- 2026-07-31: 成交原因 (阶梯止盈·档1/移动止盈/
                                          -- 硬止损/人工卖出...), 来自下单侧 fill context;
                                          -- 无 ctx (部成第二笔/手工单/买入) 空串
    pnl_amount  REAL NOT NULL DEFAULT 0,  -- 2026-08-07: 卖出盈亏金额 (book 成本法, 见计划书
                                          -- §四-9); 买入/历史行默认 0 = 不计盈亏 (上线日起算)
    pnl_pct     REAL NOT NULL DEFAULT 0   -- 卖出盈亏% (×100); 买入/历史行默认 0
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
-- 盘后日报全明细 payload (2026-08-07, 飞书/web 同源): date 主键幂等,
-- payload_json 存组装好的全明细 (资产/交易/仓位变动/卖出明细)
CREATE TABLE IF NOT EXISTS daily_report (
    date         TEXT PRIMARY KEY,       -- YYYY-MM-DD (与 daily_asset 同口径, 前端直传)
    payload_json TEXT NOT NULL,
    ts           REAL NOT NULL
);
-- ETF 轮动最近一次信号 (2026-08-14, UI 回看 + 冷启动恢复; 单行表)。
-- 只做展示/留痕, 不驱动交易 —— 交易信号每次运行时重算 (不依赖持久化)
CREATE TABLE IF NOT EXISTS rotation_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    signal_json TEXT NOT NULL DEFAULT '{}',
    updated_ts  REAL NOT NULL
);
-- ETF 轮动份内虚拟持仓簿记 (2026-09-16 三份错峰改造, 计划书 D2):
-- QMT 账户级同码持仓合并, 各份 (tranche) 的归属只记在这里;
-- entry_high = 该份该腿的移动止损基准 (持仓期最高价)。只告警不改账 (铁律1)。
CREATE TABLE IF NOT EXISTS rotation_lots (
    tranche    INTEGER NOT NULL,
    code       TEXT NOT NULL,
    qty        INTEGER NOT NULL,
    entry_high REAL NOT NULL DEFAULT 0,
    updated_ts REAL NOT NULL,
    PRIMARY KEY (tranche, code)
);
-- ETF 轮动逐份最近信号 (2026-09-16 三份错峰, 计划书 D5): 每份一行,
-- 取代单行 rotation_state (旧表停写保留, 供迁移继承 entry_high)
CREATE TABLE IF NOT EXISTS rotation_tranche_state (
    tranche     INTEGER PRIMARY KEY,
    signal_json TEXT NOT NULL DEFAULT '{}',
    updated_ts  REAL NOT NULL
);
-- ETF 轮动元数据 (2026-09-16, 计划书 D4): 「已初始化」等一次性标志,
-- 防 lots 表误删后重启静默重排份归属
CREATE TABLE IF NOT EXISTS rotation_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class DailyAssetStore:
    """每日资产快照 (分析 Tab 净值曲线数据源)。

    2026-08-16 按表域内聚 (M7 重新论证): 从 TradeStore 抽出的独立小类,
    拥有 daily_asset 表的全部 SQL 与读语义。共享父 TradeStore 的同一
    `_conn`/`_lock` —— 不各自建连接、不各自跑迁移 (DDL 仍由父 `_SCHEMA`
    统一负责)。
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock):
        self._conn = conn
        self._lock = lock

    def save(self, date: str, total_asset: float,
             available: float, market_value: float,
             source: str = "eod") -> bool:
        """日终资产快照。date=YYYY-MM-DD。幂等 (冲突覆盖)。

        source: 'eod'=QMT 实测 (默认, 老调用方零行为变化) /
        'derived'=停机日推算 (trade/asset_gapfill 写)。
        返回 False = 拒绝写入 —— **实测行 (eod) 永不被推算值覆盖**
        (2026-09-10: 存储层的最后一道保险, 不只靠上层自律)。
        反向允许: 同日先有推算行、后来真归档了 → 实测值覆盖推算值。
        """
        with self._lock, self._conn:
            if source != "eod":
                row = self._conn.execute(
                    "SELECT source FROM daily_asset WHERE date=?", (date,)
                ).fetchone()
                if row and (row[0] or "eod") == "eod":
                    return False
            self._conn.execute(
                """INSERT INTO daily_asset (date, total_asset, available,
                   market_value, ts, source) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(date) DO UPDATE SET
                    total_asset=excluded.total_asset,
                    available=excluded.available,
                    market_value=excluded.market_value,
                    ts=excluded.ts,
                    source=excluded.source""",
                (date, total_asset, available, market_value, time.time(), source),
            )
        return True

    _COLS = "date, total_asset, available, market_value, ts, source"

    def get(self, start: str = "", end: str = "") -> list[dict]:
        """读日终资产序列 (YYYY-MM-DD)。缺省 start/end = 全部。

        source: 'eod'=实测 / 'derived'=推算 (老库迁移后老行回填 'eod')。
        """
        def _q(where: str = "", args: tuple = ()):
            with self._lock:
                return self._conn.execute(
                    f"SELECT {self._COLS} FROM daily_asset {where} "
                    "ORDER BY date ASC", args).fetchall()
        if start and end:
            rows = _q("WHERE date >= ? AND date <= ?", (start, end))
        elif start:
            rows = _q("WHERE date >= ?", (start,))
        elif end:
            rows = _q("WHERE date <= ?", (end,))
        else:
            rows = _q()
        return [{"date": r[0], "total_asset": r[1], "available": r[2],
                 "market_value": r[3], "ts": r[4],
                 "source": r[5] or "eod"} for r in rows]

    def load_prev(self, before_date: str) -> dict | None:
        """before_date 之前最近一日的日终资产 (当日盈亏的基准)。
        date 是 YYYY-MM-DD 文本, 字典序即日期序。无历史行 / total_asset 非正
        → None (首日运行等情况, 调用方走启动快照兜底)。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT date, total_asset FROM daily_asset "
                "WHERE date < ? AND total_asset > 0 "
                "ORDER BY date DESC LIMIT 1", (before_date,),
            ).fetchone()
        if not row:
            return None
        return {"date": row[0], "total_asset": float(row[1])}


class DailyReportStore:
    """盘后日报全明细 payload (飞书/web 同源)。

    2026-08-16 按表域内聚 (M7 重新论证): 从 TradeStore 抽出的独立小类,
    拥有 daily_report 表的全部 SQL 与 fail-soft 读语义 (json.loads 损坏
    返 None 不 500)。共享父 TradeStore 的同一 `_conn`/`_lock`。
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock):
        self._conn = conn
        self._lock = lock

    def save(self, date: str, payload: dict) -> None:
        """盘后日报全明细 payload 落库 (2026-08-07, web 回看 + 飞书同源)。
        date=YYYY-MM-DD。幂等 (UPSERT, 当日重跑覆盖)。"""
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO daily_report (date, payload_json, ts)
                   VALUES (?,?,?)
                   ON CONFLICT(date) DO UPDATE SET
                     payload_json=excluded.payload_json, ts=excluded.ts""",
                (date, json.dumps(payload, ensure_ascii=False), time.time()),
            )

    def load(self, date: str) -> dict | None:
        """读某日日报 payload, 无记录返 None。date=YYYY-MM-DD。
        2026-08-07 审计 MEDIUM#2: payload_json 损坏返 None (与 load_latest 同 fail-soft,
        不让 /daily_report 端点因坏数据 500)。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM daily_report WHERE date = ?", (date,)
            ).fetchone()
        try:
            return json.loads(row[0]) if row else None
        except (ValueError, TypeError):
            return None

    def load_latest(self) -> dict | None:
        """最近一份日报 payload (date DESC 首行)。无记录返 None。
        日历缺省查询 / 日报接口缺省日期时用。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM daily_report ORDER BY date DESC LIMIT 1"
            ).fetchone()
        try:
            return json.loads(row[0]) if row else None
        except (ValueError, TypeError):
            return None


class RotationSignalStore:
    """ETF 轮动最近一次信号 (UI 回看 + 冷启动恢复, 单行表)。

    2026-08-16 按表域内聚 (M7 重新论证): 从 TradeStore 抽出的独立小类,
    拥有 rotation_state 表的全部 SQL 与 fail-soft 读语义。只做展示/留痕,
    不驱动交易。共享父 TradeStore 的同一 `_conn`/`_lock`。
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock):
        self._conn = conn
        self._lock = lock

    def save(self, signal: dict) -> None:
        """落最近一次轮动信号 (含信号明细 + 调仓时间/来源)。单行 upsert,
        只做 UI 回看/冷启动恢复, 不驱动交易 (交易每次运行时重算信号)。"""
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO rotation_state (id, signal_json, updated_ts)
                   VALUES (1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     signal_json=excluded.signal_json,
                     updated_ts=excluded.updated_ts""",
                (json.dumps(signal, ensure_ascii=False), time.time()),
            )

    def load(self) -> dict | None:
        """读最近一次轮动信号, 无记录/损坏返 None (fail-soft, 不影响交易)。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT signal_json FROM rotation_state WHERE id = 1").fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            return None


class RotationLotsStore:
    """ETF 轮动份内虚拟持仓簿记 (2026-09-16 三份错峰, 计划书 D2)。

    拥有 rotation_lots 表的全部 SQL。{(tranche, code): {qty, entry_high}}
    整表快照式读写 —— 行数 ≤ 份数×4, 每次执行 pass 结束整体替换,
    共享父 TradeStore 的同一 `_conn`/`_lock`。唯一写者 = rotation 的消费者
    线程路径 (铁律 3); 与 QMT 漂移只告警不回写 (铁律 1)。
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock):
        self._conn = conn
        self._lock = lock

    def load_all(self) -> dict:
        """读全表 → {(tranche, code): {"qty": int, "entry_high": float}}。
        损坏行跳过 (fail-soft)。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT tranche, code, qty, entry_high FROM rotation_lots"
            ).fetchall()
        out = {}
        for tranche, code, qty, eh in rows:
            try:
                out[(int(tranche), str(code))] = {
                    "qty": int(qty), "entry_high": float(eh or 0.0)}
            except (ValueError, TypeError):
                continue
        return out

    def replace_all(self, lots: dict) -> None:
        """整表替换 (qty ≤ 0 的行不落库 = 删除)。单事务, 要么全成要么全败。"""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM rotation_lots")
            self._conn.executemany(
                "INSERT INTO rotation_lots (tranche, code, qty, entry_high,"
                " updated_ts) VALUES (?,?,?,?,?)",
                [(int(t), str(c), int(v["qty"]), float(v.get("entry_high") or 0.0),
                  time.time())
                 for (t, c), v in lots.items() if int(v.get("qty", 0)) > 0])


class RotationTrancheStateStore:
    """ETF 轮动逐份最近信号 (2026-09-16 三份错峰, 计划书 D5)。

    拥有 rotation_tranche_state 表的全部 SQL。每份一行 (tranche 主键),
    结构同旧 rotation_state 的 signal blob (含 pending_target/entry_high/
    has_target 注入键)。fail-soft 读语义, 共享父 `_conn`/`_lock`。
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock):
        self._conn = conn
        self._lock = lock

    def save(self, tranche: int, record: dict) -> None:
        """落某份的最近信号记录 (含 ts/source/signal)。单行 upsert。"""
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO rotation_tranche_state
                   (tranche, signal_json, updated_ts) VALUES (?,?,?)
                   ON CONFLICT(tranche) DO UPDATE SET
                     signal_json=excluded.signal_json,
                     updated_ts=excluded.updated_ts""",
                (int(tranche), json.dumps(record, ensure_ascii=False),
                 time.time()),
            )

    def load_all(self) -> dict:
        """读全部份记录 → {tranche: record}; 损坏行跳过 (fail-soft)。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT tranche, signal_json FROM rotation_tranche_state"
            ).fetchall()
        out = {}
        for tranche, sj in rows:
            try:
                out[int(tranche)] = json.loads(sj)
            except (ValueError, TypeError):
                continue
        return out


class RotationMetaStore:
    """ETF 轮动元数据键值 (2026-09-16, 计划书 D4「已初始化」标志闸)。

    拥有 rotation_meta 表的全部 SQL。通用 key-value, 防误删重迁移用。
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock):
        self._conn = conn
        self._lock = lock

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM rotation_meta WHERE key=?", (key,)
            ).fetchone()
        return str(row[0]) if row else None

    def set(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO rotation_meta (key, value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, str(value)),
            )


class TierStateStore:
    """每日预埋阶梯止盈档位 (表域内聚续作, 治理III W4-f)。

    写方唯一 (executor 预埋落档), 读方唯一 (trade_main 启动恢复), 无 api
    裸 SQL 直查 —— 与 DailyAssetStore 同构的独立表域。共享父 TradeStore 的
    同一 `_conn`/`_lock`; DDL 仍由父 _SCHEMA 统一建 (migration 在父侧)。
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock):
        self._conn = conn
        self._lock = lock

    def save(self, code: str, tiers: list[int] | set[int],
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

    def load(self, trade_date: str) -> dict[str, list[int]]:
        """按当日过滤读回档位状态 (启动恢复用; 历史日期留痕不返回)。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT code, tiers_json FROM tier_state WHERE trade_date = ?",
                (trade_date,),
            ).fetchall()
        return {code: json.loads(tj) for code, tj in rows}


def _normalize_date(raw) -> str | None:
    """把写入方给的日期收敛成台账唯一的 ``YYYY-MM-DD`` 口径。

    为什么必须收敛 (2026-09-18 补测试时发现): 台账的键是
    ``(trade_date, strategy, subject)`` 里的**日期的字符串本身**。写入方要是递进来
    ``"20260916"`` (项目的 trades 表就是这个口径), ``date.fromisoformat`` 会"好心"
    收下它, 然后那行就以 ``"20260916"`` 为键落库 —— 后果是同一天在表里存在两个键
    (``20260916`` 与 ``2026-09-16``), ``load_day("2026-09-16")`` 和决策日历都
    找不到它。页面表现是"这天明明跑过却没记录", 而且不报错, 极难排查。

    所以在这里**显式**只认两种写法 (不依赖 ``fromisoformat`` 的宽松语法, 免得
    ``2026-W37-3`` 之类被意外接受), 结果统一成带横线的规范串:
      - ``YYYY-MM-DD`` —— 台账/``daily_asset`` 的既有口径;
      - ``YYYYMMDD``   —— ``trades``/``orders`` 的口径, 宽容收下并转换。
    其余一律返回 ``None`` (调用方据此拒写)。
    """
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            return _dt.date(int(s[0:4]), int(s[5:7]), int(s[8:10])).isoformat()
        if len(s) == 8 and s.isdigit():
            return _dt.date(int(s[0:4]), int(s[4:6]), int(s[6:8])).isoformat()
    except (TypeError, ValueError):
        return None
    return None


# 台账行的来源可信度 (数字越大越可信)。规则: **低可信度的来源不许覆盖更高的** ——
# 当场记录的行 (live) 永远不会被历史回填 (backfill_*) 改写; 反过来, 回填过的日子
# 后来真跑起来了, 实时写入可以正常覆盖回填行 (那是"更好的证据来了")。
# 注意不能简化成"是 live 就不许覆盖": 那会把**实时路径自己重跑**也拦掉
# (同一天定时一次 + 人工触发一次是常见情形, 必须允许覆盖成最新)。2026-09-18 冒烟实测抓到。
_SOURCE_RANK = {
    "live": 3,            # 当场记录 (策略当时写下的)
    "backfill_exact": 2,  # 历史复原 (从审计的结构化明细翻出来, 数字齐全)
    "backfill_text": 1,   # 历史原文 (只能照抄审计文字, 分不出枚举)
    "inferred": 0,        # 推断 (例如"当天没开机")
}


def _dumps(evidence) -> str:
    """把"凭什么"那包数字序列化成 JSON 文本。已经是字符串就原样用。"""
    if evidence is None:
        return "{}"
    if isinstance(evidence, str):
        return evidence or "{}"
    try:
        return json.dumps(evidence, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "{}"


#: 一行台账的明细里最多留几条"当天还发生过"。到顶就不再涨 —— 实时监控是每 10 秒
#: 重试一次, 一天最多能试上千次; 不封顶的话这一格的 JSON 会越写越长, 而每次重试
#: 都要把整个 JSON 重写一遍, 写入量按平方级膨胀。
_EVENTS_MAX = 50


def _ev_dict(raw) -> dict:
    """evidence (JSON 字符串或 dict) → dict。坏了给空字典, 不让页面因此打不开。"""
    if isinstance(raw, dict):
        return raw
    try:
        ev = json.loads(raw or "{}")
    except (ValueError, TypeError):
        ev = {}
    return ev if isinstance(ev, dict) else {}


def _ev_list(ev: dict) -> list:
    items = ev.get("events")
    return items if isinstance(items, list) else []


def _same_event(a, b) -> bool:
    """两条事件是不是"同一件事" (动作 + 原因码 + 原话三者全同)。"""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    return (str(a.get("action") or "") == str(b.get("action") or "")
            and str(a.get("reason_code") or "") == str(b.get("reason_code") or "")
            and str(a.get("reason_text") or "") == str(b.get("reason_text") or ""))


def _append_event(evidence_json: str, event: dict) -> str:
    """把一条"动作更弱"的事件追加进 evidence 的 events 列表里。

    大白话: 主结论只显示最强的那条动作, 但弱的那条不能被抹掉 —— 它被塞进
    明细的 events 里, 页面展开就能看到"当天其实还发生过这件事"。

    两条护栏:
      - **一样的已经有了就不再加** (监控重试时同一句话会重复写上几百遍);
      - 到 ``_EVENTS_MAX`` 就不再涨 (保住最早那批 —— 故事的开头最有用)。
    """
    ev = _ev_dict(evidence_json)
    events = list(_ev_list(ev))
    if any(_same_event(event, e) for e in events):
        return evidence_json
    if len(events) >= _EVENTS_MAX:
        return evidence_json
    events.append(event)
    ev["events"] = events
    return _dumps(ev)


def _merge_events(old_evidence_json: str, new_evidence_json: str) -> str:
    """把旧行的 events **接到新行前面**, 再带上新行自己的 (同一条只留一次)。

    为什么非做不可 (2026-09-18 实测钓出来的缺陷): 写入口碰到"同一强度、同一个
    对象、又来一条"时是**整份覆盖** evidence 的 —— 那一刻新行手里没有 events,
    于是之前攒的轨迹被清空一次。结果就是"留下几条"完全取决于写入顺序:
    先成交后失败, 14 条失败全留下; 先失败后成交, 只剩最后 1 条。

    真实影响不是理论上的: 2026 年 8 月 4 日 002039.SZ 那天监控试了
    12 种不同的失败情形才成交, 台账里只留了 1 条; 而 7 月 30 日 600808.SH
    那 14 条之所以留住了, 纯粹因为那天的成交恰好写在前面。

    顺序: 旧事件 → 新事件。旧的一定更早, 读起来就是一条时间线。
    """
    old, new = _ev_dict(old_evidence_json), _ev_dict(new_evidence_json)
    out = dict(new)
    merged: list = []
    for e in _ev_list(old) + _ev_list(new):
        if not any(_same_event(e, x) for x in merged):
            merged.append(e)
    if merged:
        out["events"] = merged[:_EVENTS_MAX]
    return _dumps(out)


class DailyDecisionStore:
    """当日决策台账 (2026-09-18): 「今天为什么动 / 为什么没动」的结构化答案。

    一天 × 一条策略 × 一个对象 = 一行。主键 ``(日期, 策略, 对象)`` 天然幂等 ——
    同一天同一条策略同一个对象重跑 (定时一次 + 人工触发一次) 只覆盖成最新,
    不会堆重复行。

    **唯一写入口 ``log()`` 一个人管三件事**, 四个写入点 (ETF 轮动 / 尾盘选股 /
    止盈止损 / 预埋单) 不必各自记牢:

    1. **非交易日拒写** —— 休市日本就不该有"决策", 写了反而是在台账里塞一行假数据;
    2. **动作强弱合并** —— 有成交的那条永远压得住没成交的那条 (SELL > BUY > FAIL
       > INFO > HOLD); 更弱的那次不会丢, 它被追加进 ``evidence_json.events``
       里可查 (**无论先后顺序都是这样**: 先失败后成交、先成交后失败, 两次都留,
       结果一致); 同一强度重复发生时, 只有"原话不一样"的那几次才留, 且最多
       ``_EVENTS_MAX`` 条 (监控每 10 秒重试, 一天能试上千次);
    3. **出错吞掉 + 补写审计** —— 台账写不进去绝不能影响交易, 最多留一条
       ``decision_log_fail`` 审计等人来看;
    4. **时间戳口径统一** —— ``updated_ts`` 记的是"主结论那条动作**什么时候**
       发生的", 历史回填由审计表的原始 ``ts`` 带进来 (见 ``event_ts``), 因此
       8 月的台账行显示的是 8 月的时间, 而不是"回填跑的那天"。被合并进
       ``events`` 的弱动作**不改** ``updated_ts`` (时间线不能自相矛盾)。

    ``source`` 是给用户看的可信度 (卡片右上角一个小角标):
    ``live``=当场记录 / ``backfill_exact``=历史复原 / ``backfill_text``=历史原文
    / ``inferred``=推断。
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock):
        self._conn = conn
        self._lock = lock

    # ── 写 ──────────────────────────────────────────────────────

    def log(self, rows: list[dict] | None, *,
            overwrite_live: bool = False) -> int:
        """落一批决策行。返回成功写入的行数 (被拒/被合并不计)。

        每行必需键: ``trade_date`` / ``strategy`` / ``subject`` /
        ``reason_code`` / ``reason_text``; 可选 ``action`` (缺省按原因码的默认动作)、
        ``evidence`` (dict)、``trade_ids`` (可迭代 → 自动逗号连接)、``source``
        (缺省 ``live``)、``event_ts`` (这件事发生的时刻, 秒级时间戳)。

        ``trade_date`` 收两种写法并统一收敛成 ``YYYY-MM-DD`` 落库 (见
        ``_normalize_date``): 台账口径 ``2026-09-16`` 与 trades 口径 ``20260916``
        都收, 其余 (含空/坏值) 拒写 —— 不收敛的话同一天会分裂成两个主键,
        页面上表现为"这天跑过却没记录"。

        ``event_ts`` **不传就等于"写库那一刻"**, 实时写入的四个点都不需要传
        (写的时候就是刚发生的时候)。只有历史回填要传 —— 它从审计表里带出当年
        那一刻的真实时间, 否则 7 月 30 日那 587 次卖出重试会全被标成回填日期。
        这一行的 ``updated_ts`` 与明细 ``events[].ts`` 都用它。

        ``overwrite_live=False`` (缺省) 时按**来源可信度**保护: 低可信度的行不许
        覆盖更高的 —— 历史回填 (``backfill_*`` / ``inferred``) 永远改不动当场记录
        的 ``live`` 行; 而"实时写实时""回填过的日子后来真跑了、实时覆盖回填行"都
        照常允许。``overwrite_live=True`` 只在回填工具"修数据"这种场合才用。
        """
        rows = [r for r in (rows or []) if r]
        if not rows:
            return 0
        last_exc: Exception | None = None
        for attempt in (0, 1):
            try:
                return self._log_once(rows, overwrite_live=overwrite_live)
            except Exception as e:  # noqa: BLE001 —— 台账失败绝不能影响交易
                last_exc = e
                if attempt == 0:
                    time.sleep(0.1)   # WAL 写偶尔被 SQLite busy 挡住, 重试一次
        _logger.error("决策台账落库失败 (重试仍失败, 不影响交易): %s", last_exc)
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO audit (ts, kind, message, detail_json) "
                    "VALUES (?,?,?,?)",
                    (time.time(), "decision_log_fail",
                     f"决策台账落库失败: {last_exc}", "{}"))
        except Exception:
            _logger.debug("决策台账失败审计也没写进去 (不影响交易)")
        return 0

    def _log_once(self, rows: list[dict], *, overwrite_live: bool) -> int:
        now = time.time()
        written = 0
        with self._lock, self._conn:
            for r in rows:
                # 日期先收敛成唯一口径 (YYYY-MM-DD) 再判交易日 —— 顺序不能反:
                # 用没收敛的原串去判, "20260916" 会被当成合法日期放行, 然后以
                # 原串为键落库, 变成日历和 load_day 都找不到的孤儿行。
                day = _normalize_date(r.get("trade_date"))
                if day is None or not self._is_writable_day(day):
                    continue
                # 浅拷贝: 归一化后的日期要进主键, 但不去改调用方自己那份 dict
                row = dict(r, trade_date=day)
                if self._upsert_one(row, overwrite_live=overwrite_live, now=now):
                    written += 1
        return written

    @staticmethod
    def _is_writable_day(date_str) -> bool:
        """非交易日拒写 (守卫收在唯一写入口上)。

        为什么在这里拦而不是指望四个写入点各自判断: 2026-09-18 复核发现尾盘选股的
        定时入口当时只判了开关、**没判交易日** (同日已修), 说明"每个点都记得判断"
        这件事靠不住。收到这里一处, 四个写入点自动全受保护。

        传进来的 ``date_str`` 必须已经是 ``_normalize_date`` 收敛过的规范口径
        (``YYYY-MM-DD``); 用的是**这一行自己的日期**, 所以历史回填同样受保护 ——
        回填到周末照样被拦, 不会往台账里塞非交易日的行。
        """
        try:
            d = _dt.date.fromisoformat(str(date_str))
        except (TypeError, ValueError):
            return False
        return bool(_cal_is_trading_day(d))

    def _upsert_one(self, r: dict, *, overwrite_live: bool, now: float) -> bool:
        """写/合并一行。返回 True = 这一行对台账产生了影响 (新增或覆盖)。"""
        key = (str(r["trade_date"]), str(r["strategy"]), str(r["subject"]))
        # 没传 action 就按原因码的默认动作, 而不是拍脑袋写 HOLD —— 见 log() 的
        # 契约。默认成 HOLD 的坑: 将来哪个写入点忘了传 action, 一笔真卖出
        # (EXIT_TRIGGERED) 会被记成"没动", 页面上直接看错, 而且不报错。
        action = str(r.get("action") or _action_of(str(r.get("reason_code") or ""))
                     or "HOLD")
        trade_ids = r.get("trade_ids") or ""
        if not isinstance(trade_ids, str):
            trade_ids = ",".join(str(x) for x in trade_ids)
        # "这一行的时间"优先取这件事**当时**发生的时刻 (历史回填从审计表带过来的
        # event_ts), 没带才退回写库那一刻。实时写入两条路径等价 (写的时候就是刚
        # 发生的时候), 只有回填会不同 —— 见 trade/decision_backfill.py 的设计
        # 决定 4。
        try:
            ts = float(r["event_ts"]) if r.get("event_ts") is not None else now
        except (TypeError, ValueError):
            ts = now
        row = (
            r["trade_date"], r["strategy"], r["subject"], action,
            str(r.get("reason_code") or ""), str(r.get("reason_text") or ""),
            _dumps(r.get("evidence")), trade_ids,
            str(r.get("source") or "live"), ts,
        )
        old = self._conn.execute(
            "SELECT action, evidence_json, source, reason_code, reason_text, "
            "updated_ts FROM daily_decision "
            "WHERE trade_date=? AND strategy=? AND subject=?", key).fetchone()
        if old is None:
            self._conn.execute(
                """INSERT INTO daily_decision
                   (trade_date, strategy, subject, action, reason_code,
                    reason_text, evidence_json, trade_ids, source, updated_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""", row)
            return True
        (old_action, old_evidence, old_source,
         old_code, old_text, old_ts) = old
        if not overwrite_live and (
                _SOURCE_RANK.get(row[8], 0) < _SOURCE_RANK.get(old_source, 0)):
            # 可信度更低的来源不许覆盖更高的 —— 当场记录的行永不被历史回填改写 (E9)
            return False
        new_rank = ACTION_RANK.get(action, 0)
        old_rank = ACTION_RANK.get(old_action, 0)
        if new_rank >= old_rank:
            # 第一步: 把旧行攒下的轨迹接到新行前面 —— 这一步保证"留下几条"
            # 不再取决于写入顺序 (先成交后失败 / 先失败后成交 结果一样)。
            new_evidence = _merge_events(old_evidence, row[6])
            # 第二步: 旧的主结论本身也要留 —— 它是"更早 / 更弱的那一次"。
            # 这是台账里信息量最大的一类事件: 上午"想卖没卖成"(可用为 0 /
            # 跌停), 下午真卖成了 —— 只看主结论会以为一路顺利, 而那次失败
            # 恰恰是用户最需要知道的 (要不要人工处理)。
            # 2026-09-18 修正: 原来只有"先强后弱"的顺序才会进 events,
            # 而真实顺序几乎总是"先失败后成交", 于是失败信息全被丢掉;
            # 注释早就写着要保留, 实现漏了另一半。
            # 同强度重复 (两次都是"没卖成") 时, 只有"原话不一样"才留 ——
            # 完全一样的重试由 _append_event 自己去重, 不留噪音。
            if old_code and (new_rank > old_rank
                             or str(old_code) != row[4]
                             or str(old_text or "") != row[5]):
                new_evidence = _append_event(new_evidence, {
                    "ts": old_ts, "action": old_action,
                    "reason_code": old_code, "reason_text": old_text})
            self._conn.execute(
                """UPDATE daily_decision SET
                     action=?, reason_code=?, reason_text=?, evidence_json=?,
                     trade_ids=?, source=?, updated_ts=?
                   WHERE trade_date=? AND strategy=? AND subject=?""",
                (row[3], row[4], row[5], new_evidence, row[7], row[8], row[9],
                 key[0], key[1], key[2]))
        else:
            # 新动作更弱: 主结论不动, 把这件事追加进 events —— 信息不丢。
            # **故意不动 updated_ts**: 它记的是"主结论那条动作什么时候发生的",
            # 被一条更弱的支线事件改写会让时间线自相矛盾 (页面上可能显示
            # "15:05 卖出成功" 而这一行的时间却是 15:06 的那次重试)。
            # 每条 events 自带 ts, 完整时间线照样看得到。
            self._conn.execute(
                "UPDATE daily_decision SET evidence_json=? "
                "WHERE trade_date=? AND strategy=? AND subject=?",
                (_append_event(old_evidence, {
                    "ts": ts, "action": action,
                    "reason_code": row[4], "reason_text": row[5]}),
                 key[0], key[1], key[2]))
        return True

    # ── 读 ──────────────────────────────────────────────────────

    _COLS = ("trade_date, strategy, subject, action, reason_code, reason_text, "
             "evidence_json, trade_ids, source, updated_ts")

    def load_day(self, date: str, conn: sqlite3.Connection | None = None) -> list[dict]:
        """读某一天的台账行 (``date``=YYYY-MM-DD)。

        ``conn`` 传了就用它 (Web 端点用只读连接读, WAL 下与写连接不互堵);
        不传则用本 store 的共享写连接 (需持锁, 只有消费者线程该这么用)。
        明细 JSON 坏了给空字典, **不让页面因此打不开** (fail-soft)。
        """
        return self._query(conn,
                           "WHERE trade_date = ? ORDER BY strategy, subject",
                           (date,))

    def load_range(self, start: str, end: str,
                   conn: sqlite3.Connection | None = None) -> list[dict]:
        """读一段日期区间的台账行 (含两端, YYYY-MM-DD), 供决策日历按月取数。"""
        return self._query(
            conn,
            "WHERE trade_date >= ? AND trade_date <= ? "
            "ORDER BY trade_date, strategy, subject",
            (start, end))

    def _query(self, conn, where: str, args: tuple) -> list[dict]:
        if conn is not None:
            rows = conn.execute(
                f"SELECT {self._COLS} FROM daily_decision {where}", args).fetchall()
        else:
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT {self._COLS} FROM daily_decision {where}",
                    args).fetchall()
        out = []
        for r in rows:
            try:
                evidence = json.loads(r[6] or "{}")
            except (ValueError, TypeError):
                evidence = {}
            if not isinstance(evidence, dict):
                evidence = {}
            out.append({
                "trade_date": r[0], "strategy": r[1], "subject": r[2],
                "action": r[3], "reason_code": r[4], "reason_text": r[5],
                "evidence": evidence, "trade_ids": r[7], "source": r[8],
                "updated_ts": r[9],
            })
        return out


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
        self._migrate_orders_status_msg()
        self._migrate_trades_pnl()
        self._migrate_daily_asset_source()
        self._lock = threading.Lock()

        # 分析/展示数据快照关切 (2026-08-16 M7 重新论证): 各子 store 拥有
        # 自己表的 SQL + fail-soft 读语义, 共享本连接同一 `_conn`/`_lock` ——
        # 不各自建连接、不各自跑迁移 (DDL 仍由上方 `_SCHEMA` + `_migrate_*` 负责)。
        self.daily_asset = DailyAssetStore(self._conn, self._lock)
        self.daily_report = DailyReportStore(self._conn, self._lock)
        self.rotation_signal = RotationSignalStore(self._conn, self._lock)
        # 2026-09-16 三份错峰: 份内簿记 + 逐份状态 + 元数据标志 (计划书 D2/D4/D5)
        self.rotation_lots = RotationLotsStore(self._conn, self._lock)
        self.rotation_tranche = RotationTrancheStateStore(self._conn, self._lock)
        self.rotation_meta = RotationMetaStore(self._conn, self._lock)
        self.tier_state = TierStateStore(self._conn, self._lock)  # 治理III W4-f
        # 2026-09-18 当日决策台账: 「今天为什么动/没动」的结构化答案 (唯一写入口
        # 内含非交易日守卫 + 动作合并 + 出错兜底)
        self.decision = DailyDecisionStore(self._conn, self._lock)

        raw_path = Path(raw_log_path)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        self._raw_fp = open(raw_path, "a", encoding="utf-8")
        # 2026-08-06 审计 HIGH#3: 异步写盘 —— fp 由 writer 线程独占,
        # append_raw 不再持 _lock 做磁盘 IO (铁律 2)
        self._raw_writer = _RawLogWriter(self._raw_fp)

    # ── 写接口 (消费者线程) ─────────────────────────────────────

    def rebind_order(self, old_id: str, new_id: str) -> bool:
        """占位号重绑 (2026-09-07 T5): orders.order_id 是主键 ——
        读旧行 → 删旧 → 按新主键重插 (业务字段与时间戳全保留);
        trades.order_id 旧值级联改绑 (成交归因对齐)。新主键已存在
        或旧行不存在 → False, 不猜测不覆盖。"""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT order_id, remark, code, direction, price, qty, "
                "filled_qty, status, status_msg, created_ts, updated_ts "
                "FROM orders WHERE order_id=?", (old_id,)).fetchone()
            if row is None:
                return False
            if self._conn.execute("SELECT 1 FROM orders WHERE order_id=?",
                                  (new_id,)).fetchone():
                return False
            self._conn.execute("DELETE FROM orders WHERE order_id=?",
                                (old_id,))
            self._conn.execute(
                """INSERT INTO orders
                   (order_id, remark, code, direction, price, qty,
                    filled_qty, status, status_msg, created_ts, updated_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (new_id, row[1], row[2], row[3], row[4], row[5], row[6],
                 row[7], row[8], row[9], row[10]))
            self._conn.execute(
                "UPDATE trades SET order_id=? WHERE order_id=?",
                (new_id, old_id))
        return True

    def save_order(self, record: dict) -> None:
        """按 order_id upsert 委托记录 (回报乱序/重复都以最新状态覆盖)。
        2026-08-03 修复: QMT order_id 跨会话复用 (同 order_id 可能先分配给
        000721, 下次分配给 300158), ON CONFLICT 必须更新全部业务字段,
        否则新订单静默合并进旧记录的 code/price/qty。

        2026-08-10 (泰山石油事件): order_id 复用时 created_ts 也不能留旧值
        —— 672137218 今天的新单继承了 07-27 旧单的创建时间, 页面时间列
        显示成 07-27 01:14:42。规则: 冲突时仅当调用方**显式提供**
        created_ts (下单路径的本地时刻 / reconciler 的 QMT order_time)
        才覆盖; 纯状态回写 (_on_order_error 等不传) 不动原值。"""
        now = time.time()
        created = record.get("created_ts")
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO orders
                   (order_id, remark, code, direction, price, qty,
                    filled_qty, status, status_msg, created_ts, updated_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(order_id) DO UPDATE SET
                    remark=excluded.remark,
                    code=excluded.code,
                    direction=excluded.direction,
                    price=excluded.price,
                    qty=excluded.qty,
                    filled_qty=excluded.filled_qty,
                    status=excluded.status,
                    status_msg=excluded.status_msg,
                    created_ts=CASE WHEN ? THEN excluded.created_ts
                                    ELSE orders.created_ts END,
                    updated_ts=excluded.updated_ts""",
                (
                    record["order_id"], record.get("remark", ""),
                    record["code"], record["direction"], record["price"],
                    record["qty"], record.get("filled_qty", 0),
                    record["status"], record.get("status_msg", ""),
                    created or now, now,
                    1 if created else 0,
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
        # 2026-09-16 P2-3: 共享连接读取补 _lock (对齐同类方法写法,
        # 原裸读与写线程并发时游标可读到中间态)
        with self._lock:
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
                    source, reason, pnl_amount, pnl_pct)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record["traded_id"], record["order_id"], record["code"],
                    record["direction"], record["price"], record["qty"],
                    record.get("amount", record["price"] * record["qty"]),
                    record.get("ts", time.time()),
                    record.get("source", "system"),
                    record.get("reason", ""),
                    record.get("pnl_amount", 0.0),
                    record.get("pnl_pct", 0.0),
                ),
            )

    def load_today_trade_ids(self, date_str: str) -> set[str]:
        """当日全部 traded_id (审计H4修复: 启动回填幂等集合,
        防重启后成交回报重推双扣持仓)。"""
        day_start = time.mktime(time.strptime(date_str, "%Y%m%d"))
        with self._lock:
            rows = self._conn.execute(
                "SELECT traded_id FROM trades WHERE ts >= ?", (day_start,),
            ).fetchall()
        return {r[0] for r in rows}

    def load_today_trades_detail(self, date_str: str) -> list[dict]:
        """当日成交明细 (2026-08-07 盘后日报全明细数据源)。
        date_str=YYYY-MM-DD (与 daily_asset 同口径, 前端/web 直传不转换)。
        返回 [{traded_id,code,direction,price,qty,amount,reason,pnl_amount,ts}, ...]
        按 ts 升序 (与成交记录页一致; 卖出明细折叠也按此序)。"""
        start = time.mktime(time.strptime(date_str, "%Y-%m-%d"))
        end = start + 86400.0
        with self._lock:
            rows = self._conn.execute(
                "SELECT traded_id, code, direction, price, qty, amount, "
                "reason, pnl_amount, pnl_pct, ts FROM trades "
                "WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
                (start, end),
            ).fetchall()
        return [{"traded_id": r[0], "code": r[1], "direction": r[2],
                 "price": r[3], "qty": r[4], "amount": r[5], "reason": r[6],
                 "pnl_amount": r[7], "pnl_pct": r[8], "ts": r[9]} for r in rows]

    def load_all_trades(self) -> list[dict]:
        """全历史成交 (按 ts 升序), 盘后日报重放算每笔剩余/卖出比例的基数
        (2026-08-08)。返回 [{traded_id, order_id, code, direction, price, qty, ts}]。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT traded_id, order_id, code, direction, price, qty, ts "
                "FROM trades ORDER BY ts ASC"
            ).fetchall()
        return [{"traded_id": r[0], "order_id": r[1], "code": r[2],
                 "direction": r[3], "price": r[4], "qty": r[5], "ts": r[6]}
                for r in rows]

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

    def _migrate_trades_pnl(self) -> None:
        """2026-08-07: trades 表加 pnl_amount/pnl_pct 列 (卖出盈亏, book 成本法)。
        幂等演进, 历史行默认 0 = 不计盈亏 (pnl 依赖当时成本快照, 事后无从回填,
        从本功能上线日起算)。"""
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(trades)")]
        with self._conn:
            if cols and "pnl_amount" not in cols:
                self._conn.execute(
                    "ALTER TABLE trades ADD COLUMN pnl_amount REAL NOT NULL DEFAULT 0")
            if cols and "pnl_pct" not in cols:
                self._conn.execute(
                    "ALTER TABLE trades ADD COLUMN pnl_pct REAL NOT NULL DEFAULT 0")

    def _migrate_orders_status_msg(self) -> None:
        """2026-08-07: orders 表加 status_msg 列 (委托状态描述/废单原因,
        XtOrder.status_msg, 0807 废单事件)。幂等演进, 历史行空串留白。"""
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(orders)")]
        if cols and "status_msg" not in cols:
            with self._conn:
                self._conn.execute(
                    "ALTER TABLE orders ADD COLUMN status_msg TEXT NOT NULL DEFAULT ''")

    def _migrate_daily_asset_source(self) -> None:
        """2026-09-10: daily_asset 表加 source 列 (实测/推算之分)。
        幂等演进, 历史行默认 'eod' —— 9/9 停机事件前写的行都是 QMT 实测,
        回填 'eod' 即正确口径, 零行为变化。"""
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(daily_asset)")]
        if cols and "source" not in cols:
            with self._conn:
                self._conn.execute(
                    "ALTER TABLE daily_asset ADD COLUMN source TEXT NOT NULL DEFAULT 'eod'")

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
        """JSONL append-only 异步落盘 (2026-08-06 审计 HIGH#3): 调用线程
        (网关回调) 只 dumps + 入队, 微秒级无锁无 IO; 专职 writer 线程
        批量写盘 + 批量 flush。
        审计M10修复(定位改写): 这是审计/复盘留痕, 不是崩溃重放机制 ——
        恢复走 QMT 全量对账 + 当日成交回填幂等集合, JSONL 不提供 replay。
        崩溃丢失窗口 ≤0.5s 尾部批次, 可接受; 正常退出经 close() drain。"""
        self._raw_writer.put(
            json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        )

    def flush_raw(self, timeout: float = 5.0) -> bool:
        """阻塞至 raw 日志队列清空且落盘 (测试接缝 / 关事前 drain)。"""
        return self._raw_writer.flush(timeout)

    def close(self) -> None:
        # 先停 raw writer (drain 队列 + 终 flush), 再关 fp —— 正常退出不丢日志
        self._raw_writer.shutdown()
        with self._lock:
            try:
                self._conn.close()
            finally:
                self._raw_fp.close()
