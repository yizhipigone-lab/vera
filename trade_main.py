"""trade_main.py — 实盘交易唯一 composition root (计划书 §三"组装"行)。

设计意图:
    所有依赖关系看 TradeApp.__init__ 即知, 禁止模块互 import 单例。
    放项目根目录 (main.py/server.py 入口传统),
    因为它不是库代码, 是进程入口。

启动: python trade_main.py [--config path] [--fake] [--api-port 8081]
铁律: connect → 启动对账 → 通过才允许预埋/交易; 原始回报先落盘再入队;
断线 → 指数退避重连 → 重新订阅 → 全量对账。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

load_dotenv(_PROJECT_ROOT / ".env")  # 加载 FEISHU_WEBHOOK_URL 等环境变量

from core.stock_filter import get_cached_info  # noqa: E402
from trade.auto_buy import AutoBuyFeature  # noqa: E402
from trade.book import (  # noqa: E402
    DIRECTION_BUY,
    OS_JUNK,
    PRICE_TYPE_LIMIT,
    TERMINAL_STATUSES,
    Book,
    compute_remaining_map,
)
from trade.config import TradeConfig, load_trade_config  # noqa: E402
from trade.events import (  # noqa: E402
    EVENT_COMMAND,
    EVENT_CONNECTION_LOST,
    EVENT_EOD,
    EVENT_CANCEL_ERROR,
    EVENT_ORDER_ERROR,
    EVENT_ORDER_UPDATE,
    EVENT_QUOTE_SNAPSHOT,
    EVENT_RECONCILE,
    EVENT_ROTATION,
    EVENT_SIGNALS,
    EVENT_SYNC_REPORTS,
    EVENT_TICK,
    EVENT_TIMER_SCAN,
    EVENT_TRADE_FILL,
    Event,
    EventEngine,
)
from trade.executor import Executor  # noqa: E402
from trade.gateway import FakeGateway, RealGateway  # noqa: E402
from trade.monitor import SESSION_NAMES, Monitor, is_trading_day_cached, trading_session  # noqa: E402
from trade.notifier import FeishuNotifier  # noqa: E402
from trade.reconciler import Reconciler  # noqa: E402
from trade.risk import KillSwitch, OrderIntent, RiskContext, RiskGate  # noqa: E402
from trade.rotation import RotationFeature  # noqa: E402
from trade.store import TradeStore  # noqa: E402
from utils.logger import get_logger  # noqa: E402

_logger = get_logger("trade.main")

# 断线重连退避序列上限 (计划书 §5.5: 1s→2s→…→60s)
_RECONNECT_BACKOFF_MAX_SEC = 60.0


def _day_str(ts: float) -> str:
    """时间戳 → YYYYMMDD (本地时区)。"""
    return time.strftime("%Y%m%d", time.localtime(ts))


def _hhmm(ts: float) -> str:
    """时间戳 → HH:MM (本地时区)。"""
    return time.strftime("%H:%M", time.localtime(ts))


def _reason_from_ctx(ctx: dict) -> str:
    """fill context → 成交原因串 (trades.reason, 成交记录页"原因"列)。
    优先 detail (2026-07-31 起下单侧登记的自然语言全文, 如
    "移动止盈: 最高 12.00 (峰值涨幅 +20.0%, 过激活线 5%), 现价 11.30
    回撤 5.8% 触发 (阈值 1%)"); 旧格式回退 label+档位;
    无 ctx (买入/部成第二笔/手工认领单) 空串, 前端按来源兜底展示。"""
    detail = ctx.get("detail")
    if detail:
        return detail
    label = ctx.get("label") or ""
    tier = ctx.get("tier")
    if label and tier is not None:
        return f"{label}·档{int(tier) + 1}"
    return label


class _DailyTimer:
    """每日生命周期定时器。只 put 事件不干活 (铁律 2 同款纪律:
    定时器线程不是写者, 所有状态变更都在消费者线程)。"""

    def __init__(self, engine: EventEngine, config: TradeConfig,
                 clock=time.time, poll_sec: float = 1.0):
        self._engine = engine
        self._cfg = config
        self._clock = clock
        self._poll = poll_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fired: set[tuple[str, str]] = set()   # (标签, 日期) 当日不重复
        self._last_scan = 0.0
        self._last_sync = 0.0

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="trade-daily-timer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _fire_once(self, label: str, event: Event) -> None:
        day = _day_str(self._clock())
        if (label, day) in self._fired:
            return
        self._fired.add((label, day))
        self._engine.put(event)

    def _run(self) -> None:
        cur_day = ""
        while not self._stop.is_set():
            now = self._clock()
            hhmm = _hhmm(now)
            # 审计M5修复: _fired 跨日清理 —— 条目带日期不会误拦次日,
            # 但不清理会按日无界累积
            day = _day_str(now)
            if day != cur_day:
                self._fired.clear()
                cur_day = day
            # 监控腿扫描 (含 pending_check / 心跳 / force_market 判定)
            if now - self._last_scan >= self._cfg.monitor_scan_interval_sec:
                self._last_scan = now
                self._engine.put(Event(type=EVENT_TIMER_SCAN, data={"hhmm": hhmm}))
            # 增量同步时点 (2026-07-30: 成交补记 + 委托状态回写,
            # 回调丢失的主动补偿; 时段过滤在消费者侧 handler)
            if now - self._last_sync >= self._cfg.sync_interval_sec:
                self._last_sync = now
                self._engine.put(Event(type=EVENT_SYNC_REPORTS, data={"hhmm": hhmm}))
            # 定时对账时点
            if hhmm in self._cfg.reconcile_times:
                self._fire_once(f"reconcile-{hhmm}",
                                Event(type=EVENT_RECONCILE, data={"hhmm": hhmm}))
            # 09:15 预埋 (9:15–9:20 窗口一次性全档位挂出)
            if hhmm == "09:15":
                self._fire_once("ladder", Event(type=EVENT_COMMAND, data={
                    "action": "place_ladder",
                    "date_str": _day_str(now)}))
            # 尾盘自动选股买入 (2026-07-27 MVP): 到点发命令,
            # 选股本身在工作线程跑 (TDX 阻塞, 消费者线程禁入)
            if hhmm == self._cfg.auto_buy.time:
                self._fire_once("auto_buy", Event(type=EVENT_COMMAND, data={
                    "action": "auto_buy", "source": "scheduled"}))
            # 2026-08-14: ETF 轮动每日触发 (算信号 + 调仓, 一次做完)
            if hhmm == self._cfg.rotation.execute_time:
                self._fire_once("rotation", Event(type=EVENT_COMMAND, data={
                    "action": "rotation_run", "source": "scheduled"}))
            # 15:05 EOD 归档
            if hhmm == "15:05":
                self._fire_once("eod", Event(type=EVENT_EOD, data={"hhmm": hhmm}))
            self._stop.wait(self._poll)


class TradeApp:
    """组装 + 生命周期 + 事件接线。消费者线程的 handlers 全在这里,
    翻这一个文件即知全部订阅者 (静态接线的意义)。"""

    def __init__(self, config: TradeConfig, fake: bool | None = None,
                 clock=time.time, fake_gateway_kwargs: dict | None = None,
                 unhealthy_disconnect_scans: int = 2,
                 config_path: str | None = None,
                 selection_runner=None):
        self._cfg = config
        self._config_path = config_path   # 设置面板写回用 (None = 不落盘)
        self._clock = clock
        # 尾盘选股桥 (2026-07-27 MVP): 生产=真 TDX 桥, 测试=fake。
        # 延迟 import —— trade.signals 拉起 selection/TDX 依赖,
        # 无 TDX 环境跑测试不能顶层 import 它
        if selection_runner is None:
            from trade.signals import run_tail_selection
            selection_runner = run_tail_selection
        self._selection_runner = selection_runner
        # 批次4: auto_buy 状态全迁 AutoBuyFeature; 2026-08-01 委托缝已删,
        # 调用方直用 self._auto_buy 公开接口 (start/on_signals/last/running)
        # 连续 N 次扫描不健康 → 判断线 (P0-③ 心跳主触发源, 默认 2 次
        # ≈ 2×scan_interval, 给单次网络抖动留容错)
        self._unhealthy_threshold = unhealthy_disconnect_scans
        self._unhealthy_scans = 0
        # fake 优先级: 显式参数 > 环境变量 > 配置文件
        if fake is None:
            fake = (os.environ.get("VERA_TRADE_FAKE") == "1") or config.fake_sdk

        # ── 按依赖顺序构造 ──
        self.store = TradeStore(config.db_path, config.raw_log_path)
        self.book = Book()
        self.kill = KillSwitch(self.store, config.kill_flag_path)
        self.risk = RiskGate(self.kill, self.store, config.daily_loss_limit,
                             sizing=config.position_sizing)

        self._connected = False
        self._reconciled = False
        self._day_baseline: float | None = None
        # 重连状态 (2026-07-31 方案C): _on_connection_lost 不再 while True 占
        # 消费者, 改由 _on_scan 周期驱动 _try_reconnect (单次 ≤5s 不死锁)
        self._reconnect_pending = False
        self._reconnect_backoff = 1.0
        self._last_reconnect_attempt = 0.0

        # 网关回调 → 先落 JSONL 再入队 (铁律 5: 先落盘再处理)。
        # 审计M10修复(定位改写): JSONL 是审计/复盘留痕, 不是崩溃重放
        # 机制 —— 恢复走 QMT 全量对账 + 当日成交回填幂等集合。
        # 回调线程只许做这两件事 (铁律 2), 不接 xtquant 同步调用。
        def _wire(sink, event_type):
            def _cb(*args):
                payload = args[0] if len(args) == 1 else args
                self.store.append_raw({"kind": event_type, "data": payload,
                                       "ts": self._clock()})
                # 事件 ts 用注入时钟而非 time.time(): 生产无差别,
                # 测试里与 monitor/executor 时钟同源 (2026-07-27 时段
                # 感知后 e2e 锚定工作日盘中, 两处时钟必须一致)
                sink.put(Event(type=event_type, data=payload,
                               ts=self._clock()))
            return _cb

        self._engine = EventEngine(
            handlers={
                EVENT_ORDER_UPDATE: lambda e: self._on_order(e.data),
                EVENT_TRADE_FILL: lambda e: self._on_trade(e.data),
                EVENT_TICK: lambda e: self._on_quote_event(e.data, e.ts),
                EVENT_QUOTE_SNAPSHOT: lambda e: self._on_quote_event(e.data, e.ts),
                EVENT_TIMER_SCAN: lambda e: self._on_scan(e.data or {}),
                EVENT_RECONCILE: lambda e: self._on_reconcile(),
                EVENT_SYNC_REPORTS: lambda e: self._on_sync_reports(),
                EVENT_EOD: lambda e: self._on_eod(),
                EVENT_COMMAND: lambda e: self.dispatch_command(e.data or {}),
                # 批次4 接线①: 选股结果直接转发特性 (不在组合根落地逻辑)
                EVENT_SIGNALS: lambda e: self._auto_buy.on_signals(e.data or {}),
                # 2026-08-14 接线: 轮动信号结果转发轮动特性
                EVENT_ROTATION: lambda e: self._rotation.on_signals(e.data or {}),
                EVENT_CONNECTION_LOST: lambda e: self._on_connection_lost(e.data),
                EVENT_ORDER_ERROR: lambda e: self._on_order_error(e.data),
                EVENT_CANCEL_ERROR: lambda e: self._on_cancel_error(e.data),
            },
            audit_sink=self.store.write_audit,  # 审计M4: 队列满丢弃要留痕
        )
        gw_cls = FakeGateway if fake else RealGateway
        # FakeGateway 期初持仓/资金可注入 (e2e 测试接缝; 生产真网关不需要)
        if fake:
            gw_kwargs = dict(fake_gateway_kwargs or {})
            # 2026-08-14: 注入 app 时钟到 FakeGateway, 成交/委托 ts 与 TradeApp
            # 同源 —— 修掉周末跑测试时真实 time.time() 与注入时钟跨日的误拦
            gw_kwargs["clock"] = clock
        else:
            # 启动预检: 真网关缺账号/路径时给能看懂的报错, 而不是 QMT 的 rc=-1
            if not config.account_id or not config.qmt_path:
                raise RuntimeError(
                    "真网关需要 account_id 与 qmt_path(miniQMT 的 userdata_mini 目录), "
                    "请在 --config 指定的 yaml 中配置; 或用 --fake 跑测试模式")
            gw_kwargs = {"account_id": config.account_id,
                         "mini_qmt_path": config.qmt_path}
        # tick 闭包先建一次复用 —— on_quote 每 tick 都调, 不再每次重建 _wire
        tick_wire = _wire(self._engine, EVENT_TICK)
        self.gateway = gw_cls(
            on_order=_wire(self._engine, EVENT_ORDER_UPDATE),
            on_trade=_wire(self._engine, EVENT_TRADE_FILL),
            on_quote=lambda code, q: tick_wire({"code": code, **q}),
            on_disconnected=_wire(self._engine, EVENT_CONNECTION_LOST),
            on_order_error=_wire(self._engine, EVENT_ORDER_ERROR),
            on_cancel_error=_wire(self._engine, EVENT_CANCEL_ERROR),
            **gw_kwargs,
        )

        self.reconciler = Reconciler(
            self.gateway, self.book, self.store, self.kill,
            quote_price=lambda code: (q := self.monitor.quote_of(code)) and q["last"],
            # 2026-08-15 (审计 M4): 轮动卖单也纳入对账 in_flight 降级网 ——
            # 轮动卖单绕过 executor._pending, 回调丢失时若只查 executor 会漏
            # 掉这票差异 → 误判 CRITICAL 急停
            in_flight_sells=lambda: {**self.executor.in_flight_sells(),
                                     **self._rotation.in_flight_sells()},
            # 2026-07-31: 回调丢失走补记时同样读 fill ctx 落成交原因 +
            # 飞书通知。peek 不删 (部成多笔共享原因), 终态由
            # on_order_terminal 回收 (lambda 延迟取 self.executor —— 构造序在后)
            pop_fill_context=lambda oid: self.executor.peek_fill_context(oid) or {},
            reason_from_ctx=_reason_from_ctx,
            on_adopted_trade=self._on_adopted_trade,
            on_order_terminal=lambda oid: self.executor.discard_fill_context(oid),
        )
        self.executor = Executor(
            self.gateway, self.book, self.store, self.risk, config,
            build_risk_ctx=self._build_risk_ctx,
            get_quote=lambda code: self.monitor.quote_of(code),
            get_prev_close=self._prev_close,
            # 2026-08-01 A6 收尾: ST 判定接 TDX IsSTGP (与回测同口径,
            # get_cached_info 进程级缓存; TDX 不可用返回 {} → 非 ST,
            # 与此前默认行为一致, fail-safe)
            st_checker=lambda code: str(
                get_cached_info(code).get("IsSTGP", "0")) == "1",
            clock=clock,
        )
        # 2026-08-01 P0-1: hold_days 接线 —— 从 trades 表取首笔买入时间,
        # 经交易日历算持仓天数, 救活 Monitor 的三条时间类卖出规则
        # (time_stop/cond_time/first_day, 此前默认 lambda:0 永不会触发)。
        def _hold_days_for_code(code: str) -> int:
            try:
                ro = self.store.open_readonly()
                cur = ro.execute(
                    "SELECT MIN(ts) FROM trades WHERE code=? AND direction=?",
                    (code, DIRECTION_BUY))
                row = cur.fetchone()
                ro.close()
                if not row or not row[0]:
                    return 0
                from trade.api import _hold_days as _calc_hold_days
                days = _calc_hold_days(row[0])
                return days if days is not None else 0
            except Exception:
                return 0

        # 2026-08-06 P2 落地: 移动止盈峰值 = 持仓期最高价 (不再当日重置)。
        # 首笔买入日 → gateway 拉不复权日线 high 取 max; 按 (code, 当日)
        # 缓存 (历史日高日内不变, 当日高由 tick 流补充), 失败回退 None。
        _peak_cache: dict = {}

        def _episode_entry_ts(code: str):
            """当轮持仓的首笔买入 ts (2026-08-06 审计 P1 修复)。

            旧口径 MIN(ts) 取的是"有史以来"第一笔买入: 同票上一轮清仓后
            再次买入时, 历史峰值会包含空仓期间的高点, 可能导致买入后
            立刻误触发移动止盈。现按当前持仓量从最新成交往回推:
            卖单加回、买单扣减, 持仓量归零处的那笔买入即本轮起点。
            数据不全 (倒推不完) 时回退最早一笔买入 (保守: 峰值取大不取小,
            保护更紧)。hold_days 仍用旧口径 —— 多算天数只会让时间止损
            提前, 方向安全, 不动。"""
            try:
                ro = self.store.open_readonly()
                cur = ro.execute(
                    "SELECT ts, direction, qty FROM trades WHERE code=? "
                    "ORDER BY ts DESC", (code,))
                rows = cur.fetchall()
                ro.close()
            except Exception:
                return None
            if not rows:
                return None
            pos = self.book.snapshot()["positions"].get(code)
            remaining = float(pos.volume) if pos is not None else 0.0
            for ts, direction, qty in rows:
                qty = float(qty or 0)
                if direction == DIRECTION_BUY:
                    remaining -= qty
                    if remaining <= 0.0:
                        return ts
                else:
                    remaining += qty
            return rows[-1][0]  # 倒推不完, 回退最早一笔买入

        def _peak_for_code(code: str):
            today = time.strftime("%Y%m%d", time.localtime(clock()))
            key = (code, today)
            ent = _peak_cache.get(key)
            if ent is not None:
                val, fail_ts = ent
                # 2026-08-06 审计 P2: 失败结果只缓存 60s (防瞬时故障让
                # 历史峰值保护整天缺席), 成功值按天缓存 (历史日高日内不变)
                if val is not None or (clock() - fail_ts) < 60.0:
                    return val
            val = None
            try:
                entry_ts = _episode_entry_ts(code)
                if entry_ts:
                    start = time.strftime("%Y%m%d", time.localtime(entry_ts))
                    highs = self.gateway.query_daily_highs(code, start, today)
                    if highs:
                        val = max(highs)
            except Exception:
                val = None
            _peak_cache[key] = (val, None if val is not None else clock())
            return val

        self.monitor = Monitor(
            self.gateway, self.book, self.executor, self.store, config,
            hold_days=_hold_days_for_code,
            peak_px=_peak_for_code,
            clock=clock,
        )
        # 2026-08-01 P0-3 (H1): executor pending 终态废单/已撤时,
        # 通知 monitor 解除 _triggered —— 该票下轮扫描重新评估
        self.executor._on_pending_died = lambda code: self.monitor._triggered.discard(code)
        # 2026-08-01 批次4 瘦身: 尾盘自动买入特性 (实现全在 trade/auto_buy.py)。
        # cfg 传 getter 不传值 —— _apply_config 换 self._cfg 引用即热更穿透
        # (评审 ⚠ 点); build_risk_ctx/get_prev_close 读组合根状态, callable 注入。
        self._auto_buy = AutoBuyFeature(
            self._engine, lambda: self._cfg, self.store, self.gateway,
            self.book, self.monitor, self.risk, self.executor,
            selection_runner=self._selection_runner,
            build_risk_ctx=self._build_risk_ctx,
            get_prev_close=self._prev_close,
            # 2026-08-14: 双池预算帽 —— 轮动启用时选股系统买入被
            # 股票池预算 (S_target − 股票市值) 封顶, 不花 ETF 池的钱
            budget_provider=self._stock_budget,
            clock=clock)
        # 2026-08-14: ETF 轮动特性 (双池资金分配)。cfg 传 getter 热更穿透;
        # 信号在工作线程算, 调仓在消费者线程执行 (与 auto_buy 同纪律)。
        self._rotation = RotationFeature(
            self._engine, lambda: self._cfg, self.store, self.gateway,
            self.book, self.monitor, self.risk, self.executor,
            build_risk_ctx=self._build_risk_ctx, clock=clock)
        # 飞书通知器 (2026-07-31): 自带 worker 线程, 生产侧只入队裸 dict,
        # 消费者线程零阻塞 (铁律 3); URL 走环境变量, enabled 走 config 热关。
        self._notifier = FeishuNotifier(
            enabled_getter=lambda: self._cfg.feishu.enabled,
            webhook_getter=lambda: os.environ.get("FEISHU_WEBHOOK_URL"),
            clock=clock)
        self.timer = _DailyTimer(self._engine, config, clock=clock)

    # ═══════════════════════════════════════════════════════════
    # 生命周期
    # ═══════════════════════════════════════════════════════════

    def start(self, start_timers: bool = True) -> bool:
        """启动顺序铁律: connect → 冷启动灌仓 → 启动对账 → 通过才放行。
        返回启动对账是否通过 (不通过 = 已急停, 调用方应告警人工介入)。"""
        self.gateway.connect()
        self._connected = True
        today = _day_str(self._clock())
        # 冷启动三合一 (审计H4修复: 当日已成交 traded_id 回填幂等集合,
        # 防重启后 QMT 重推当日成交回报双扣持仓):
        # ① QMT 是持仓唯一真相源, 本地空账本只能从这里灌;
        # ② 档位标记只恢复当日 (审计C1: 昨日标记留痕不阻碍今日预埋);
        # ③ 当日成交回报幂等集合
        tiers_today = {(code, today): tiers
                       for code, tiers in self.store.load_tier_states(today).items()}
        self.book.restore(
            positions=self.gateway.query_positions(),
            tiers=tiers_today,
            seen_trades=self.store.load_today_trade_ids(today),
            orders=self.store.load_open_orders(),   # 2026-07-30: 恢复在途订单簿
        )
        # 盘前基准权益 (日亏软熔断的尺子)
        try:
            self._day_baseline = self.gateway.query_asset()["total_asset"]
        except Exception:
            self._day_baseline = None  # 缺失 → 风控 fail-safe 禁买
        # 订阅持仓行情 (预埋/监控都靠它)
        codes = sorted(self.book.snapshot()["positions"])
        if codes:
            self.gateway.subscribe_quotes(codes)
            # 2026-07-30: 启动即用快照预填报价 — 午休/盘前重启后 tick 未至,
            # 持仓不应显示"—"(应显示上午收盘/最近成交价, 用户决策)。
            # 与 monitor._poll_quotes 同一路径 (get_full_tick → on_quote),
            # 失败不阻断启动 (等 tick 自然补价)。
            try:
                for code, quote in self.gateway.query_quotes(codes).items():
                    self.monitor.on_quote(code, quote)
            except Exception as e:
                _logger.warning("启动快照预填报价失败 (等待 tick 补价): %s", e)

        self._engine.start()
        self._notifier.start()
        report = self.reconciler.reconcile(now_ts=self._clock())
        self._reconciled = report.passed
        if not report.passed:
            _logger.critical("启动对账未通过 (%s), 急停已激活, 需人工介入",
                             report.level)
        if start_timers:
            self.timer.start()
        if report.passed:
            self._startup_catchup(today)
        return self._reconciled

    def _startup_catchup(self, today: str) -> None:
        """审计M5修复: 定时任务补偿 —— 定时器精确匹配 hhmm, 进程错过
        时点就全天缺席。启动时对账通过后补一轮:
        - 已过 09:25 且在交易时段、当日未预埋 → 补偿预埋 (audit 留痕);
        - 已过轮动 execute_time 且当日未跑轮动 → 补偿轮动 (2026-08-15 审计 L4);
        - 15:05 后启动且当日无 EOD 快照 → 补 EOD (对账 C 方次日基准)。
        """
        hhmm = _hhmm(self._clock())
        tiers_today = self.store.load_tier_states(today)
        # 2026-08-10: enabled=false 不补预埋 (与 place_ladder / 盘中兜底同步关);
        # 时间窗下限 09:25→09:15 —— 覆盖"9:15 后启动错过 09:15 定时器"场景,
        # 9:15-09:25 挂的限价单参与开盘集合竞价撮合, 无副作用。
        if ("09:15" <= hhmm <= "15:00"
                and not tiers_today
                and self._cfg.stop.ladder_tp.enabled):
            self.store.write_audit(
                "ladder_catchup", f"启动已过 09:15 ({hhmm}) 且当日未预埋, 补偿预埋",
                {"hhmm": hhmm})
            self.executor.place_ladder(today)
        # 2026-08-15 (审计 L4): 已过 execute_time 且当日未跑轮动 → 补一轮。
        # 轮动 start 内部会再校验 enabled + 连续竞价时段 (fail-closed)。
        if (self._cfg.rotation.execute_time <= hhmm <= "15:00"
                and not self._rotation_ran_today(today)):
            self.store.write_audit(
                "rotation_catchup", f"启动已过 {self._cfg.rotation.execute_time} "
                f"({hhmm}) 且当日未跑轮动, 补偿一轮", {"hhmm": hhmm})
            self._rotation.start("scheduled")
        if hhmm >= "15:05":
            snap = self.store.load_position_snapshot()
            day_start = time.mktime(time.strptime(today, "%Y%m%d"))
            snap_ts = max((p.get("ts", 0.0) for p in snap.values()), default=0.0)
            if snap_ts < day_start:
                self.store.write_audit(
                    "eod_catchup", f"15:05 后启动 ({hhmm}) 且当日无 EOD, 补偿归档",
                    {"hhmm": hhmm})
                self._on_eod(notify_daily=False)  # M-功1: 补偿路径不发零盈亏日报

    def stop(self) -> None:
        """优雅退出: 先停事件源 (定时器), 再停消费者, 最后断网关/关库。"""
        self.timer.stop()
        self._engine.stop()
        self._notifier.stop()
        try:
            self.gateway.disconnect()
        finally:
            self.store.close()

    # ═══════════════════════════════════════════════════════════
    # 命令入口 (api/CLI → put 队列, HTTP 线程不碰交易状态)
    # ═══════════════════════════════════════════════════════════

    def submit_command(self, cmd: dict) -> None:
        self._engine.put(Event(type=EVENT_COMMAND, data=cmd))

    def dispatch_command(self, cmd: dict) -> None:
        """命令分发 (消费者线程)。加新命令 = 加分支, 不改 api 层。"""
        action = cmd.get("action", "")
        if action == "manual_buy":
            self._cmd_buy(cmd)
        elif action == "manual_sell":
            # 与监控腿触发同一路径 (撤单流水线), 不另起炉灶。
            # manual=True: 2026-07-27 裁决① 人工命令任何时段放行,
            # 买一价无戳/陈旧不拦 (用户当下意图)
            # 2026-07-30: 可选 qty — 指定卖部分量 (≤可用), 缺省卖全部可用
            self.executor.execute_exit(cmd["code"], "manual_sell: 人工卖出",
                                       manual=True, qty=cmd.get("qty"))
        elif action == "cancel":
            self.gateway.cancel(cmd["order_id"])
            self.store.write_audit("manual_cancel", f"人工撤单 {cmd['order_id']}", cmd)
        elif action == "kill_on":
            self.kill.activate(cmd.get("source", "manual_api"))
            self.store.write_audit("kill_on", "人工激活急停", cmd)
        elif action == "kill_off":
            # 只许人工解除: 能走到这里的一定是 api/CLI 的人工命令。
            # 解除后立刻补一次对账 —— 不补的话 reconciled 仍 False,
            # 闸 2 会把人工解除变成"解了也白解"
            self.kill.deactivate()
            self.store.write_audit("kill_off", "人工解除急停", cmd)
            self._on_reconcile()
        elif action == "place_ladder":
            self.executor.place_ladder(
                cmd.get("date_str") or _day_str(self._clock()))
        elif action == "auto_buy":
            # 批次4 接线②: 命令直接转发特性
            self._auto_buy.start(cmd.get("source", "manual"))
        elif action == "rotation_run":
            # 2026-08-14 接线: 轮动命令转发特性 (算信号 + 调仓)
            self._rotation.start(cmd.get("source", "manual"))
        elif action == "update_config":
            self._apply_config(cmd["config_obj"], cmd.get("changed", []))
        else:
            _logger.warning("未知命令已丢弃: %s", cmd)

    def _apply_config(self, new_cfg: TradeConfig, changed: list) -> None:
        """热替换配置 (消费者线程, 唯一写者)。运行时状态 (_triggered/
        tier 标记/pending) 全部保留 —— 只换配置对象, 不重置状态。

        热生效机制 (设置面板契约):
        - monitor/executor/timer 持有的 config 是**用时读属性**,
          换引用即热 (stop.*/扫描间隔/心跳/陈旧阈值/force_market_after);
        - RiskGate 是**构造时快照** (daily_loss_limit/sizing 标量拷贝),
          逐字段替换 (sizing 整对象是 frozen, 换引用);
        - 需重启字段: account_id/qmt_path/db_path/raw_log_path/
          kill_flag_path/fake_sdk —— 连接与存储路径热换无意义且危险,
          接受保存但重启才生效 (audit 里标注)。
        """
        from trade.config import save_trade_config
        if self._config_path:
            save_trade_config(new_cfg, self._config_path)
        self._cfg = new_cfg
        self.monitor._cfg = new_cfg
        self.executor._cfg = new_cfg
        self.timer._cfg = new_cfg
        self.risk._loss_limit = new_cfg.daily_loss_limit
        self.risk._sizing = new_cfg.position_sizing
        self.store.write_audit(
            "config_update", f"配置已热更新: {', '.join(changed) or '(无差异)'}",
            {"changed": changed,
             "restart_required_for": ["account_id", "qmt_path", "db_path",
                                      "raw_log_path", "kill_flag_path",
                                      "fake_sdk"]})

    # ═══════════════════════════════════════════════════════════
    # 消费者线程 handlers
    # ═══════════════════════════════════════════════════════════

    def _on_order(self, rec: dict) -> None:
        self.book.apply_order_update(
            rec["order_id"], rec["status"], code=rec.get("code", ""),
            direction=rec.get("direction", 0), price=rec.get("price", 0.0),
            qty=rec.get("qty", 0), filled_qty=rec.get("filled_qty"),
            remark=rec.get("remark", ""))
        self.store.save_order(rec)
        # 注意: fill ctx 不在这里按终态清理 —— 终态委托回报可能先于
        # 成交回报到达 (FakeGateway 双回报顺序实证), 先清会丢原因。
        # 回收点: _on_trade 订单满量后 + reconciler._sync_orders 同步腿。

    def _on_trade(self, rec: dict) -> None:
        # H2 (2026-08-06 审计 HIGH#1): 跨日成交拦截 — xtquant 断线重连会把
        # 昨日成交回报重推, traded_id 不在今日幂等集 (store WHERE ts>=day_start)
        # 会重复入账污染账本 (600127 双记账急停事件同类根因)。与 reconciler
        # 同口径: ts 非今日 → 记 warning 后 return; ts 缺失(None) 无法验证,
        # 放行但留 debug 痕 (不沿用 reconciler "if ts and" 隐式放行盲区写法,
        # 显式分支便于审计追溯)。
        ts = rec.get("ts")
        if ts is not None:
            today = _dt.datetime.fromtimestamp(self._clock()).strftime("%Y%m%d")
            if _dt.datetime.fromtimestamp(ts).strftime("%Y%m%d") != today:
                _logger.warning(
                    "跨日成交拦截 (HIGH#1): %s ts=%s 非今日(%s), 疑断线重连重推,"
                    " 已拦 apply_trade 防重复入账; traded_id=%s",
                    rec.get("code"), ts, today, rec.get("traded_id"))
                return
        else:
            _logger.debug("成交缺 ts 按无法验证放行 (HIGH#1): traded_id=%s",
                          rec.get("traded_id"))
        # H1 (2026-07-31 审计): apply_trade 清仓会把 avg_cost 清零,
        # 盈亏% 必须在 apply 前取成本快照, 传给 _notify_fill。
        pre_pos = self.book.snapshot()["positions"].get(rec["code"])
        pre_avg_cost = pre_pos.avg_cost if pre_pos else 0.0
        # 幂等: book 按 traded_id 判重; store 唯一约束是物理底线,
        # 重复回报插入炸 IntegrityError 属预期, 不是故障
        if not self.book.apply_trade(
                rec["traded_id"], rec["order_id"], rec["code"],
                rec["direction"], rec["price"], rec["qty"]):
            return
        # 2026-07-30 修复: 成交后把新代码并入行情订阅 — 原订阅只在启动时
        # 按当时持仓建一次, 启动后新买入的票永远收不到 tick → last/pnl
        # 恒 None, monitor 无价 fail-closed 跳过 (7-29 买入 20 只无盈亏、
        # 无盘中监控事件)。订阅失败不阻断成交落库。
        try:
            self.gateway.subscribe_quotes([rec["code"]])
        except Exception as e:
            _logger.warning("新买入订阅行情失败 (监控将无价跳过): %s: %s",
                            rec["code"], e)
        # 2026-07-31: 成交原因落库 (成交记录页"原因"列)。fill context
        # peek 不删 —— 部成多笔共享同一份原因, 订单终态才由
        # _on_order/_sync_orders 回收; 组装一次供 save_trade 和
        # _notify_fill 共用 —— 必须在 save_trade 前取。
        ctx = self.executor.peek_fill_context(rec["order_id"]) or {}
        rec["reason"] = _reason_from_ctx(ctx)
        # 2026-08-07: 卖出成交落盈亏金额 (盘后日报 realized_pnl/sell_details
        # 数据源, book 成本法, 与 _notify_fill 同口径)。买入不塞 (默认 0)。
        if rec["direction"] != DIRECTION_BUY:
            _pnl_amt, _pnl_pct = self._sell_pnl(
                float(rec["price"]), int(rec["qty"]), pre_avg_cost)
            rec["pnl_amount"] = _pnl_amt if _pnl_amt is not None else 0.0
            rec["pnl_pct"] = _pnl_pct if _pnl_pct is not None else 0.0
        try:
            self.store.save_trade(rec)
        except Exception as e:
            # M3 (2026-08-01): 落库失败不静默 —— WAL 写偶尔被 SQLite busy
            # 挡住, 重试一次 (0.1s 间隔, 消费者线程可接受)。仍失败则靠
            # sync_reports 补记路径自愈 (QMT 是真相源, 下次 sync 会兜底)。
            time.sleep(0.1)
            try:
                self.store.save_trade(rec)
            except Exception:
                _logger.error("成交落库异常 (重试仍失败, 待 sync_reports 补记): %s", e)
        # 2026-07-30 (600808 事件): 成交进度回写订单表 — 原实现只靠
        # QMT 订单状态回调, 回调缺失时页面永远"已报/成交0"。
        try:
            self.store.update_order_filled(rec["order_id"], rec["qty"])
        except Exception as e:
            _logger.error("订单进度回写异常: %s", e)
        # 飞书成交通知 (2026-07-31): 消费者线程只组装裸 dict 入队,
        # 拼卡+POST 在 notifier worker, 不阻塞唯一写者 (铁律 3)
        try:
            self._notify_fill(rec, ctx, avg_cost=pre_avg_cost)
        except Exception:
            _logger.debug("成交通知组装异常 (不影响交易)")
        # 2026-07-31: 订单满量 (apply_trade 已把 book 订单推入终态) →
        # 回收 fill ctx (peek 语义的配套; 回调全丢时由 _sync_orders 兜底)
        order = self.book.snapshot()["orders"].get(rec["order_id"])
        if order is not None and order.status in TERMINAL_STATUSES:
            self.executor.discard_fill_context(rec["order_id"])

    def _notify_fill(self, rec: dict, ctx: dict,
                    avg_cost: float | None = None) -> None:
        """组装成交通知裸 dict 入队 (消费者线程, 微秒级; 拼卡+POST 在 worker)。
        剩余股数取 apply_trade 之后的 book 快照; 盈亏%=成交价 vs 成本。
        avg_cost: apply_trade **之前**的成本快照 (H1: 清仓卖出 apply 会把
        avg_cost 清零, 必须由调用方传 apply 前成本)。None 时回退取 book。"""
        code = rec["code"]
        direction = rec["direction"]
        price = float(rec["price"])
        qty = int(rec["qty"])
        amount = rec.get("amount")
        if amount is None:
            amount = round(price * qty, 2)
        is_buy = direction == DIRECTION_BUY
        pos = self.book.snapshot()["positions"].get(code)
        payload: dict = {
            "code": code, "direction": direction,
            "price": price, "qty": qty, "amount": amount,
            "ts": rec.get("ts") or self._clock(),
            "label": ctx.get("label"),
        }
        if not is_buy:
            # H1: 优先用调用方传的 apply 前成本; 没传才回退 book (清仓时已 0)
            cost = avg_cost if avg_cost is not None else (
                pos.avg_cost if pos else 0.0)
            _amt, _pct = self._sell_pnl(price, qty, cost)
            if _pct is not None:
                payload["pnl_pct"] = _pct
                payload["pnl_amount"] = _amt
            payload["tier"] = ctx.get("tier")
            payload["sell_ratio"] = ctx.get("sell_ratio")
            remaining_vol = pos.volume if pos else 0
            payload["remaining_vol"] = remaining_vol
            payload["remaining_value"] = round(price * remaining_vol, 2)
        self._notifier.notify_fill(payload)

    @staticmethod
    def _sell_pnl(price: float, qty: int,
                  avg_cost: float) -> tuple[float | None, float | None]:
        """卖出盈亏 (2026-08-07 抽公共, _notify_fill 与 _on_trade 落库同口径)。
        返回 (pnl_amount, pnl_pct); avg_cost<=0 → (None, None) (成本无效不算)。
        口径 = book.apply_trade 前快照成本 (移动加权, 部分卖不清零/清仓清零),
        与成交通知卡同源; 比 deals 接口旧 SQL 现算 (不扣已卖部分) 更准。"""
        if avg_cost <= 0:
            return (None, None)
        return (round((price - avg_cost) * qty, 2),
                round((price / avg_cost - 1) * 100, 2))

    def _on_adopted_trade(self, trade_dict: dict, ctx: dict,
                          avg_cost: float | None = None) -> None:
        """补记成交也发飞书 (2026-07-31): QMT 成交回调常丢失, 补记是主路径。
        ctx 由 reconciler 一次 pop 得到 (系统单有 label/tier, 手工单空);
        avg_cost 由 reconciler 在 apply_trade 前快照传入 (H1 清仓盈亏%)。"""
        try:
            self._notify_fill(trade_dict, ctx, avg_cost=avg_cost)
        except Exception:
            _logger.debug("补记成交通知异常 (不影响交易)")

    def _on_quote_event(self, data: dict, event_ts: float | None = None) -> None:
        # 审计M4修复: 事件自带 ts 透传给 monitor (心跳用生产时刻,
        # 不用消费时刻; 过旧 tick 在 monitor 侧丢弃)
        self.monitor.on_quote(data["code"], data, event_ts=event_ts)

    def _on_scan(self, data: dict) -> None:
        # 2026-07-31 方案C: 断线重连挪出 _on_connection_lost 的阻塞循环,
        # 由 timer_scan 离散驱动 _try_reconnect (单次 connect ≤5s, 不死锁
        # 消费者)。重连进行中跳过监控评估 (断线无行情, 评估无意义)。
        if self._reconnect_pending and not self._connected:
            self._try_reconnect()
            return
        # 2026-07-27 ETF 误卖事件裁决②: 心跳/断线检测只在连续竞价
        # 时段进行 —— 午休/收盘后无 tick 是常态, 此前误报断连
        if trading_session(self._clock()) != "continuous":
            self._unhealthy_scans = 0
            return
        self.monitor.scan_once(now_hhmm=data.get("hhmm"))
        # P0-③ 实测 (2026-07-26, 关闭 miniQMT 客户端): on_disconnected
        # 9 分钟零事件 —— 回调触发链不可靠, 心跳才是断线检测主触发源;
        # on_disconnected 保留为辅助触发 (触发了更好, 不触发有这里兜底)。
        # 守卫: 从未收到 tick 的"不健康"是盘前静默, 不算断线。
        if (self._connected and self.monitor.has_tick()
                and self.monitor.is_healthy() is False):
            self._unhealthy_scans += 1
            if self._unhealthy_scans >= self._unhealthy_threshold:
                self._unhealthy_scans = 0
                self._on_connection_lost(
                    f"行情心跳连续 {self._unhealthy_threshold} 次扫描不健康 "
                    f"(P0-③: on_disconnected 不可靠, 心跳为主触发源)")
        else:
            self._unhealthy_scans = 0

    def _on_reconcile(self) -> None:
        report = self.reconciler.reconcile(now_ts=self._clock())
        # P0-③: UNKNOWN = 查询不可用 (疑似断线), 维持 reconciled 现状 —
        # 既不是"通过"也不是"不通过", 等下轮心跳/重连后再判
        if report.level == "UNKNOWN":
            return
        # 对账不通过不自动解禁 reconciled —— 急停只许人工解除,
        # reconciled 同理: 一旦 False 就要等人工 unkill 后才允许重置
        self._reconciled = report.passed and not self.kill.is_active()

    def _on_sync_reports(self) -> None:
        """增量同步 (2026-07-30): 定时把 QMT 成交/委托补记回写本地。
        只在盘中时段跑 (auction/continuous/lunch) —— 盘前/收盘后无新
        回报可补, 且启动/对账/重连路径已各自带同步 (reconcile 内部
        先走 sync_reports)。"""
        if not self._connected:
            return
        if trading_session(self._clock()) not in ("auction", "continuous", "lunch"):
            return
        self.reconciler.sync_reports(now=self._clock())

    def _on_eod(self, notify_daily: bool = True) -> None:
        # 2026-08-08: 非交易日(周末/节假日)不做 EOD —— 此前周六 15:05 也归档
        # daily_asset + 推日报, 非交易日快照混进权益曲线/日历 (用户发现)
        # 2026-08-14: 用注入时钟的日期判交易日 (原 is_trading_day_cached() 走
        # 真实 date.today(), 测试注入周五时钟、真实周六跑会误判非交易日)
        if not is_trading_day_cached(_dt.date.fromtimestamp(self._clock())):
            self.store.write_audit("eod_skip", "非交易日, 跳过 EOD 归档与日报", {})
            return
        # 持仓快照归档 = 对账 C 方的明日基准; tier_state 在乐观标记时
        # 已逐笔落库 (executor.place_ladder), 此处无需重复归档
        # 2026-08-01 P0-4 (H4): query_positions 空列表 = 查询不可用
        # (断线后 QMT 不抛异常, 静默返回空), 跳过归档+audit 留痕,
        # 不把"空"当成"零持仓"写入 C 方基准。
        # 飞书盘后日报独立: 它查的是 asset (与持仓查询不同 API),
        # 持仓查不到不意味着资产查不到 —— 照常推送。
        # 2026-08-07 盘后日报全明细: 覆盖前读昨仓 (CRITICAL: 覆盖后 load 返
        # 今仓, 仓位变动恒空) + sync_reports 补齐当日成交 (绕过 _on_sync_reports
        # 时段守卫, 15:05 后非盘中会被守卫拦) 供日报 realized_pnl/sell_details 读。fail-soft。
        prev_snapshot = self.store.load_position_snapshot()
        try:
            self.reconciler.sync_reports(now=self._clock())
        except Exception:
            _logger.debug("EOD sync_reports 失败 (日报用本地已有成交)")
        positions = self.gateway.query_positions()
        if positions:
            self.store.save_position_snapshot(positions)
            self.store.write_audit("eod", "EOD 持仓快照已归档", {})
        else:
            self.store.write_audit(
                "eod_skip", "query_positions 返回空, 跳过 EOD 归档 (查询不可用)", {})
        # 分析 Tab 数据源: 每日资产快照 (2026-08-03)
        try:
            asset = self.gateway.query_asset()
            total_asset = float(asset.get("total_asset", 0.0) or 0.0)
            # 2026-08-13 修复: 键名是 cash 不是 available —— 原写法恒读到默认 0,
            # daily_asset.available 全表为 0 (当日排查总资产异常时抓出)
            available = float(asset.get("cash", 0.0) or 0.0)
            market_value = float(asset.get("market_value", 0.0) or 0.0)
            if total_asset > 0:
                from datetime import datetime
                date_str = datetime.now().strftime("%Y-%m-%d")
                self.store.save_daily_asset(date_str, total_asset, available, market_value)
        except Exception:
            _logger.debug("EOD 资产快照写入失败 (分析 Tab 不受影响)")
        # 飞书盘后日报 (2026-07-31): 搭 15:05 EOD 的车; 查不到资产 fail-soft 不推。
        # notify_daily=False: 启动补偿路径 (15:05 后重启) 不发日报 —— baseline
        # 刚用当前 total_asset 设, 差值≈0, 是启动噪声非当日真实表现 (M-功1)。
        if not notify_daily:
            return
        try:
            self._notify_daily(prev_snapshot=prev_snapshot)
        except Exception:
            _logger.debug("盘后日报组装异常 (不影响交易)")

    def _notify_daily(self, prev_snapshot: dict | None = None) -> None:
        """盘后日报 (2026-08-07 全明细增强): 资产 + 盘前基准盈亏 + 当日交易摘要
        + 仓位变动 + 浮盈 + 卖出明细。查不到资产不推 (fail-soft)。
        prev_snapshot: _on_eod 在覆盖 position_snapshot 前读的昨仓 (CRITICAL:
        覆盖后 load 返今仓, 仓位变动恒空); None 时不出仓位变动。payload 同时落
        daily_report 表 (web 回看同源)。name 不入 payload —— 展示层 (飞书/web) 自解析。"""
        try:
            asset = self.gateway.query_asset()
        except Exception:
            return
        total_asset = float(asset.get("total_asset", 0.0) or 0.0)
        if total_asset <= 0:
            return
        cash = float(asset.get("cash", 0.0) or 0.0)
        market_value = float(asset.get("market_value", 0.0) or 0.0)
        # 当日盈亏基准 = 昨日日终资产 (2026-08-13 修复: 原用 _day_baseline 即
        # 进程启动快照, 盘中/午后重启过一次基准就含当日涨跌 → 差值≈0,
        # 08-11/12/13 三天日报当日盈亏全错)。昨日日终缺行 (首日运行) 才回落
        # 启动快照; 两者都无 → None (卡片显示 "—")。查询 fail-soft 同样回落。
        date_str = _dt.datetime.fromtimestamp(self._clock()).strftime("%Y-%m-%d")
        day_pnl = None
        day_pnl_pct = None
        baseline = None
        try:
            prev = self.store.load_prev_daily_asset(date_str)
            if prev:
                baseline = float(prev["total_asset"])
        except Exception:
            _logger.debug("昨日日终资产读取异常 (回落启动快照)")
        if baseline is None and self._day_baseline:
            baseline = float(self._day_baseline)
        if baseline:
            day_pnl = round(total_asset - baseline, 2)
            day_pnl_pct = round((total_asset / baseline - 1) * 100, 2)
        positions = self.book.snapshot()["positions"]
        pos_count = sum(1 for p in positions.values() if p.volume > 0)
        payload: dict = {
            "total_asset": total_asset, "cash": cash,
            "market_value": market_value,
            "day_pnl": day_pnl, "day_pnl_pct": day_pnl_pct,
            "position_count": pos_count, "ts": self._clock(),
        }
        # 当日交易摘要 + 卖出明细 (数据源 trades 表, 已含 pnl_amount)
        try:
            trades_detail = self.store.load_today_trades_detail(date_str)
        except Exception:
            trades_detail = []
            _logger.debug("load_today_trades_detail 异常 (日报交易段留空)")
        payload.update(self._build_trade_summary(trades_detail))
        # 浮盈 = 市值 − 持仓成本 (不含税费/已实现盈亏, 与 realized_pnl 分开)
        cost_basis = sum(p.volume * p.avg_cost for p in positions.values()
                         if p.volume > 0)
        payload["floating_pnl"] = round(market_value - cost_basis, 2)
        # 仓位变动 (仅当有昨仓基准且有变化)
        # 2026-08-07 审计 MEDIUM#1: truthy 检查 — 空 dict {} 也跳过 (load_position_snapshot
        # 表空时返 {},is not None 会误进 diff → 首次 EOD 把全部既有持仓报成"新进")
        if prev_snapshot:
            changes = self._diff_positions(prev_snapshot, positions)
            if any(changes.values()):
                payload["position_changes"] = changes
        # 2026-08-15: 轮动信号进日报 (飞书 AI 复盘 + web 回看用)。只读 last 里的
        # signal dict (state/ma20/回撤/现价), 不下单; 取不到 (轮动未跑) 就缺省。
        try:
            rot_last = self._rotation.last
            if rot_last and rot_last.get("signal"):
                payload["rotation"] = rot_last["signal"]
        except Exception:
            pass
        # 飞书 + 落库 (两路 fail-soft 互不影响, 不影响交易)
        level = getattr(self._cfg.feishu, "daily_report_level", "full")
        try:
            self._notifier.notify_daily(
                payload, level=level,
                ai_review=getattr(self._cfg.feishu, "ai_review", False))
        except Exception:
            _logger.debug("盘后日报推送异常 (不影响交易)")
        try:
            self.store.save_daily_report(date_str, payload)
        except Exception:
            _logger.debug("盘后日报落库异常 (web 回看该日将缺, 不影响交易)")

    def _build_trade_summary(self, trades_detail: list[dict]) -> dict:
        """从当日成交明细算交易摘要 + 卖出明细 + 交易明细混排。
        2026-08-08: 重放全历史 Book (compute_remaining_map) 给每笔补
        剩余股数/剩余市值/卖出比例(=本次卖出÷累计买入, 用户口径)。
        sell_count=0 → win_rate=None (卡片省略, 不除零); sell_details (web 回看用,
        不动) 按 ts 升序超 8 笔折叠; trade_details (飞书单列每条一块, 买卖混排)
        按 ts 升序超 12 笔折叠 (trade_details_folded 只汇总卖出盈亏)。"""
        buy_count = sell_count = 0
        turnover = 0.0
        realized_pnl = 0.0
        wins = 0
        sells: list[dict] = []
        details: list[dict] = []   # 买卖混排 (飞书交易明细单列每条一块; 2026-08-07)
        # 重放全历史 Book 算每笔剩余/卖出比例 (trades 表不存剩余, 2026-08-08)
        tgt_tids = {str(t.get("traded_id")) for t in trades_detail
                    if t.get("traded_id")}
        remain_map = (compute_remaining_map(self.store.load_all_trades(), tgt_tids)
                      if tgt_tids else {})
        for t in trades_detail:
            turnover += abs(float(t.get("amount", 0.0) or 0.0))
            tid = str(t.get("traded_id", ""))
            rm = remain_map.get(tid, {})
            rec: dict = {
                "code": t["code"], "direction": t.get("direction"),
                "price": float(t.get("price", 0.0) or 0.0),
                "qty": int(t.get("qty", 0) or 0),
                "amount": float(t.get("amount", 0.0) or 0.0),
                "reason": t.get("reason", ""), "ts": t.get("ts", 0.0),
                "remaining_vol": rm.get("remaining_vol"),
                "remaining_value": rm.get("remaining_value"),
            }
            if t.get("direction") == DIRECTION_BUY:
                buy_count += 1
                rec["pnl_amount"] = None
                rec["pnl_pct"] = None
            else:
                sell_count += 1
                pnl = float(t.get("pnl_amount", 0.0) or 0.0)
                realized_pnl += pnl
                if pnl > 0:
                    wins += 1
                rec["pnl_amount"] = pnl
                rec["pnl_pct"] = t.get("pnl_pct")
                rec["sell_ratio"] = rm.get("sell_ratio")
                sells.append({"code": t["code"], "reason": t.get("reason", ""),
                              "pnl_amount": pnl, "pnl_pct": t.get("pnl_pct"),
                              "ts": t.get("ts", 0.0)})
            details.append(rec)
        out: dict = {
            "buy_count": buy_count, "sell_count": sell_count,
            "turnover": round(turnover, 2),
            "realized_pnl": round(realized_pnl, 2),
            "win_rate": round(wins / sell_count, 4) if sell_count > 0 else None,
        }
        if sells:
            sells.sort(key=lambda s: s["ts"])
            if len(sells) > 8:
                folded = sells[8:]
                out["sell_details"] = sells[:8]
                out["sell_details_folded"] = {
                    "count": len(folded),
                    "sum_pnl_amount": round(sum(s["pnl_amount"] for s in folded), 2),
                }
            else:
                out["sell_details"] = sells
        # 交易明细混排 (飞书双列; 买卖按 ts 升序, 超 12 笔折叠, 折叠只汇总卖出盈亏)
        if details:
            details.sort(key=lambda x: x["ts"])
            cap = 12
            if len(details) > cap:
                folded_d = details[cap:]
                out["trade_details"] = details[:cap]
                out["trade_details_folded"] = {
                    "count": len(folded_d),
                    "sum_sell_pnl": round(
                        sum(d["pnl_amount"] for d in folded_d
                            if d["pnl_amount"] is not None), 2),
                }
            else:
                out["trade_details"] = details
        return out

    def _diff_positions(self, prev: dict, curr: dict) -> dict:
        """仓位变动 (2026-08-07): 按 (今 curr vs 昨 prev) 净 volume diff 分类。
        delta = curr_volume − prev_volume。prev={code:{volume,..}} (store 快照),
        curr={code:PositionView} (book)。return {new,closed,added,reduced}。"""
        prev_vols = {c: int(p.get("volume", 0)) for c, p in prev.items()}
        curr_vols = {c: int(getattr(p, "volume", 0)) for c, p in curr.items()}
        new, closed, added, reduced = [], [], [], []
        for code in set(prev_vols) | set(curr_vols):
            pv, cv = prev_vols.get(code, 0), curr_vols.get(code, 0)
            delta = cv - pv
            if pv == 0 and cv > 0:
                new.append({"code": code, "delta": cv})
            elif cv == 0 and pv > 0:
                closed.append({"code": code, "delta": -pv})
            elif delta > 0:
                added.append({"code": code, "delta": delta})
            elif delta < 0:
                reduced.append({"code": code, "delta": delta})
        return {"new": new, "closed": closed, "added": added, "reduced": reduced}

    def _on_order_error(self, rec: dict) -> None:
        """下单失败回报 (2026-08-07 接线, 0807 事件): xtquant order_stock
        只回本地请求序号, 不代表券商柜台受理; 未送达/被拒的真实原因
        (无权限/流量控制/参数错误) 只经 on_order_error 下发 —— 原文落
        audit, 不再黑盒 (10 笔限价单"查无此单"事件的教训)。

        2026-08-10 (0810 事件): 拒单回报同时把订单推进废单终态并回写
        orders 表 —— 此前只落 audit 不改状态, 未达柜台的单页面永远停
        "已报" (收盘后 15 笔买入 QMT 查无此单, 前端却全显示已报,
        废单原因只能翻 audit)。拒单原因原文进 status_msg, 页面
        "状态说明"列直接可见。"""
        self.store.write_audit(
            "order_error",
            f"下单失败: {rec.get('error_msg', '')} "
            f"(order_id={rec.get('order_id')}, error_id={rec.get('error_id')})",
            dict(rec))
        oid = str(rec.get("order_id") or "")
        if not oid:
            return
        old = self.book.snapshot()["orders"].get(oid)
        if old is None:
            return  # 非本系统订单的拒单回报 (如券商端手工单), 只留 audit
        if old.status in TERMINAL_STATUSES:
            return  # 终态不可逆 (状态回报已先到的竞态), 不盖棺
        ok = self.book.apply_order_update(oid, OS_JUNK)
        if ok:
            self.store.save_order({
                "order_id": oid, "remark": old.remark, "code": old.code,
                "direction": old.direction, "price": old.price,
                "qty": old.qty, "filled_qty": old.filled_qty,
                "status": OS_JUNK,
                "status_msg": str(rec.get("error_msg", "") or "")})

    def _on_cancel_error(self, rec: dict) -> None:
        """撤单失败回报 (2026-08-07 接线): 撤单流水线 "锁→撤→ack" 的
        失败分支原文落 audit (受理≠撤成, 失败原因不再黑盒)。"""
        self.store.write_audit(
            "cancel_error",
            f"撤单失败: {rec.get('error_msg', '')} "
            f"(order_id={rec.get('order_id')}, error_id={rec.get('error_id')})",
            dict(rec))

    def _on_connection_lost(self, reason) -> None:
        """断线守护 (2026-07-31 方案C 重构): 不再 while True 阻塞消费者线程。
        设状态 + 写 audit + 立即首次重连尝试; 失败由 _on_scan 周期接力
        (backoff 节流)。原实现 connect 持续失败时永久死锁消费者, 事件队列
        堆满、监控/止盈止损停摆 (0731 event_dropped 实证: disconnect 5 次
        仅 reconnect 1 次)。重连不碰 kill_switch (铁律: 急停只许人工解除),
        靠 connected=False + reconciled 挡交易, 恢复后自动对账解禁。"""
        self._connected = False
        self._reconnect_pending = True
        self._reconnect_backoff = 1.0
        self._last_reconnect_attempt = 0.0
        self.store.write_audit("disconnect", f"连接断开: {reason}", {})
        self._try_reconnect()   # 立即首次尝试; 失败由 _on_scan 接力

    def _try_reconnect(self) -> None:
        """单次重连尝试 (消费者线程, 由 _on_scan / _on_connection_lost 调)。
        backoff 节流: 距上次尝试不足 backoff 秒则跳过; connect 用网关自带
        5s 超时 (RealGateway._call_with_timeout), 单次最坏 5s 不死锁。"""
        now = self._clock()
        if now - self._last_reconnect_attempt < self._reconnect_backoff:
            return
        self._last_reconnect_attempt = now
        try:
            self.gateway.connect()
        except Exception as e:
            self._reconnect_backoff = min(self._reconnect_backoff * 2,
                                          _RECONNECT_BACKOFF_MAX_SEC)
            _logger.warning("重连失败, 下次 ≥%.0fs 后重试: %s",
                            self._reconnect_backoff, e)
            self.store.write_audit("reconnect_failed", f"重连失败: {e}",
                                   {"backoff": self._reconnect_backoff})
            return
        # 重连成功: 重新订阅 → 全量对账 (推送丢失靠对账自愈;
        # reconcile 内含 sync_reports: 成交补记 + 委托状态回写)
        self._connected = True
        self._reconnect_pending = False
        self._reconnect_backoff = 1.0
        codes = sorted(self.book.snapshot()["positions"])
        if codes:
            self.gateway.subscribe_quotes(codes)
        self.store.write_audit("reconnect", "重连成功, 已重新订阅", {})
        self._on_reconcile()

    # ═══════════════════════════════════════════════════════════
    # 内部
    # ═══════════════════════════════════════════════════════════

    def _cmd_buy(self, cmd: dict) -> None:
        """人工确认买入: 过风控闸门才下单 (MVP 买入的唯一入口)。"""
        code, qty = cmd["code"], int(cmd["qty"])
        price = cmd.get("price")
        if not price:
            quote = self.monitor.quote_of(code)
            if not quote or not quote.get("last"):
                self.store.write_audit(
                    "buy_fail_closed", f"{code} 无行情, 人工买入被拒", cmd)
                return
            price = quote["last"]
        intent = OrderIntent(code=code, direction=DIRECTION_BUY,
                             price=float(price), qty=qty, manual=True)
        ok, reason = self.risk.check(intent, self._build_risk_ctx())
        if not ok:
            _logger.warning("人工买入被风控拒绝: %s %s", code, reason)
            return  # 拒绝审计 risk 层已写
        # 审计M1修复: 人工买入 remark 也走 executor 的单调发号器,
        # 不再自造 V{mmdd}-B{qty} (同量两笔必重号, 破坏对账唯一性)
        remark = self.executor.next_remark("B")
        order_id = self.gateway.order(code, DIRECTION_BUY, float(price), qty,
                                      PRICE_TYPE_LIMIT, remark)
        self.executor.register_fill_context(order_id, {"label": "人工买入"})
        self.store.write_audit(
            "manual_buy", f"人工买入 {code} {qty}@{price}",
            {"code": code, "qty": qty, "price": price, "order_id": order_id})
        # 回报会经事件链自然入账, 这里只留人工动作痕迹

    def _stock_budget(self) -> float | None:
        """股票池买入预算帽 (2026-08-14, 轮动启用时生效)。

        返回股票池还能花的钱 = max(0, (1−etf_ratio)×总资产 − 股票市值)。
        轮动关闭返回 None (auto_buy 走原 cash×0.95, 行为不变)。
        查询失败返回 None (fail-open 回退原口径, 预算帽是软隔离非安全闸)。"""
        cfg = self._cfg.rotation
        if not cfg.enabled:
            return None
        try:
            asset = self.gateway.query_asset()
            total = float(asset.get("total_asset", 0.0) or 0.0)
        except Exception:
            return None
        if total <= 0:
            return None
        return max(0.0, (1.0 - cfg.etf_ratio) * total - self._stock_pool_value())

    def _stock_pool_value(self) -> float:
        """非轮动 ETF 的持仓市值 (股票池)。无行情回退成本价。"""
        cyb, gold = self._cfg.rotation.cyb_etf, self._cfg.rotation.gold_etf
        total = 0.0
        for code, pos in self.book.snapshot()["positions"].items():
            if code in (cyb, gold) or pos.volume <= 0:
                continue
            q = self.monitor.quote_of(code)
            last = q.get("last") if q else None
            if last and last > 0:
                total += float(last) * pos.volume
            elif pos.avg_cost > 0:
                total += pos.avg_cost * pos.volume
        return total

    def _rotation_ran_today(self, today: str) -> bool:
        """当日是否已跑过轮动 (启动补偿 L4 判重: 读 rotation.last 的 ts)。"""
        last = self._rotation.last
        if not last or not last.get("ts"):
            return False
        return _day_str(float(last["ts"])) == today

    def _build_risk_ctx(self) -> RiskContext:
        try:
            asset = self.gateway.query_asset()
            total_asset = asset["total_asset"]
        except Exception:
            total_asset = 0.0  # 查询失败 → 集中度闸算不出上限, 宁可拒
        return RiskContext(
            reconcile_passed=self._reconciled,
            total_asset=total_asset,
            positions=self.book.snapshot()["positions"],
            day_baseline_equity=self._day_baseline,
            current_equity=total_asset,
            # D5: 接节假日日历; 日期取 app 注入时钟 (而非真实今日),
            # 保测试可注入与跨日语义一致
            is_trading_day=is_trading_day_cached(
                _dt.date.fromtimestamp(self._clock())),
        )

    def _prev_close(self, code: str) -> float | None:
        """昨收来源: 优先行情快照 prev_close 字段, 无则查网关快照。
        TODO P0 spike: 接入 xtdata 日线昨收 (预埋单涨停判定的上游)。"""
        quote = self.monitor.quote_of(code)
        if quote and quote.get("prev_close"):
            return float(quote["prev_close"])
        snaps = self.gateway.query_quotes([code])
        if code in snaps and snaps[code].get("prev_close"):
            return float(snaps[code]["prev_close"])
        return None

    # ── api 层读取的状态 (只读, 不改写) ─────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def reconciled(self) -> bool:
        return self._reconciled

    @property
    def config(self) -> TradeConfig:
        """当前生效配置 (api GET 序列化用; 只读, 改写走 update_config 命令)。"""
        return self._cfg

    @property
    def session(self) -> str:
        """当前交易时段 (2026-07-27 裁决②, status API 展示用)。"""
        return trading_session(self._clock())

    @property
    def monitor_reason(self) -> str:
        """行情状态的人话解释 (状态卡片直出, 不再笼统红色"降级")。"""
        s = self.session
        if s != "continuous":
            return SESSION_NAMES[s]
        return "盘中·订阅正常" if self.monitor.is_healthy() else "盘中·轮询兜底"

    @property
    def auto_buy_last(self) -> dict | None:
        """最近一次尾盘自动买入运行结果 (批次4: 委托 AutoBuyFeature.last)。"""
        return self._auto_buy.last

    @property
    def rotation_last(self) -> dict | None:
        """最近一次 ETF 轮动信号+调仓结果 (2026-08-14)。"""
        return self._rotation.last


def main() -> None:
    parser = argparse.ArgumentParser(description="VERA 实盘交易系统")
    parser.add_argument("--config", default="config/trade.yaml",
                        help="交易配置 yaml (缺字段走 TradeConfig 默认值)")
    parser.add_argument("--fake", action="store_true",
                        help="强制使用 FakeGateway (等价 VERA_TRADE_FAKE=1)")
    parser.add_argument("--api-port", type=int, default=8081)
    parser.add_argument("--page-port", type=int, default=8080,
                        help="交易页所在回测服务器端口 (CORS 放行 origin 按它推导)")
    args = parser.parse_args()

    config = (load_trade_config(args.config) if Path(args.config).exists()
              else TradeConfig())
    app = TradeApp(config, fake=True if args.fake else None,
                   config_path=args.config)
    ok = app.start()
    _logger.info("交易系统已启动 (fake=%s, 启动对账=%s)",
                 args.fake or config.fake_sdk, "通过" if ok else "未通过")

    import uvicorn

    from trade.api import create_api_app
    try:
        # CORS 放行页面服务器 (回测服务器) 的 origin, 随 --page-port 推导
        origins = [f"http://127.0.0.1:{args.page_port}",
                   f"http://localhost:{args.page_port}"]
        uvicorn.run(create_api_app(app, allowed_origins=origins),
                    host="127.0.0.1", port=args.api_port, access_log=False)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        _logger.info("交易系统已退出")


if __name__ == "__main__":
    main()
