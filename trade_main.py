"""trade_main.py — 实盘交易唯一 composition root (计划书 §三"组装"行)。

设计意图:
    所有依赖关系看 TradeApp.__init__ 即知, 禁止模块互 import 单例。
    放项目根目录 (main.py/server.py/preprocessor.py 入口传统),
    因为它不是库代码, 是进程入口。

启动: python trade_main.py [--config path] [--fake] [--api-port 8081]
铁律: connect → 启动对账 → 通过才允许预埋/交易; 原始回报先落盘再入队;
断线 → 指数退避重连 → 重新订阅 → 全量对账。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from trade.book import (  # noqa: E402
    DIRECTION_BUY,
    OS_REPORTED,
    OS_SUCCEEDED,
    PRICE_TYPE_LIMIT,
    PRICE_TYPE_MARKET_PEER_FIRST,
    TERMINAL_STATUSES,
    Book,
    is_etf,
)
from trade.config import TradeConfig, load_trade_config  # noqa: E402
from trade.events import (  # noqa: E402
    EVENT_COMMAND,
    EVENT_CONNECTION_LOST,
    EVENT_EOD,
    EVENT_ORDER_UPDATE,
    EVENT_QUOTE_SNAPSHOT,
    EVENT_RECONCILE,
    EVENT_SIGNALS,
    EVENT_TICK,
    EVENT_TIMER_SCAN,
    EVENT_TRADE_FILL,
    Event,
    EventEngine,
)
from trade.executor import Executor, limit_ratio, round_price  # noqa: E402
from trade.gateway import FakeGateway, RealGateway  # noqa: E402
from trade.monitor import Monitor, SESSION_NAMES, trading_session  # noqa: E402
from trade.reconciler import Reconciler  # noqa: E402
from trade.risk import KillSwitch, OrderIntent, RiskContext, RiskGate  # noqa: E402
from trade.store import TradeStore  # noqa: E402
from utils.logger import get_logger  # noqa: E402

_logger = get_logger("trade.main")

# 断线重连退避序列上限 (计划书 §5.5: 1s→2s→…→60s)
_RECONNECT_BACKOFF_MAX_SEC = 60.0


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
        day = time.strftime("%Y%m%d", time.localtime(self._clock()))
        if (label, day) in self._fired:
            return
        self._fired.add((label, day))
        self._engine.put(event)

    def _run(self) -> None:
        cur_day = ""
        while not self._stop.is_set():
            now = self._clock()
            hhmm = time.strftime("%H:%M", time.localtime(now))
            # 审计M5修复: _fired 跨日清理 —— 条目带日期不会误拦次日,
            # 但不清理会按日无界累积
            day = time.strftime("%Y%m%d", time.localtime(now))
            if day != cur_day:
                self._fired.clear()
                cur_day = day
            # 监控腿扫描 (含 pending_check / 心跳 / force_market 判定)
            if now - self._last_scan >= self._cfg.monitor_scan_interval_sec:
                self._last_scan = now
                self._engine.put(Event(type=EVENT_TIMER_SCAN, data={"hhmm": hhmm}))
            # 定时对账时点
            if hhmm in self._cfg.reconcile_times:
                self._fire_once(f"reconcile-{hhmm}",
                                Event(type=EVENT_RECONCILE, data={"hhmm": hhmm}))
            # 09:15 预埋 (9:15–9:20 窗口一次性全档位挂出)
            if hhmm == "09:15":
                self._fire_once("ladder", Event(type=EVENT_COMMAND, data={
                    "action": "place_ladder",
                    "date_str": time.strftime("%Y%m%d", time.localtime(now))}))
            # 尾盘自动选股买入 (2026-07-27 MVP): 到点发命令,
            # 选股本身在工作线程跑 (TDX 阻塞, 消费者线程禁入)
            if hhmm == self._cfg.auto_buy.time:
                self._fire_once("auto_buy", Event(type=EVENT_COMMAND, data={
                    "action": "auto_buy", "source": "scheduled"}))
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
        self._auto_buy_running = False      # 选股工作线程在跑 (防重入)
        self._auto_buy_last: dict | None = None   # 最近一次运行 (页面展示)
        self._auto_buy_placed: tuple = ("", set())  # (日期, 当日已下单代码)
        # disposition 终态轮询参数 (实例属性, 测试注入小值; 生产
        # 2s×~10s, 尾盘窗口可接受 —— 会短暂阻塞消费者线程, 注释即契约)
        self._await_interval = 2.0
        self._await_timeout = 10.0
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
                EVENT_EOD: lambda e: self._on_eod(),
                EVENT_COMMAND: lambda e: self.dispatch_command(e.data or {}),
                EVENT_SIGNALS: lambda e: self._on_signals(e.data or {}),
                EVENT_CONNECTION_LOST: lambda e: self._on_connection_lost(e.data),
            },
            audit_sink=self.store.write_audit,  # 审计M4: 队列满丢弃要留痕
        )
        gw_cls = FakeGateway if fake else RealGateway
        # FakeGateway 期初持仓/资金可注入 (e2e 测试接缝; 生产真网关不需要)
        if fake:
            gw_kwargs = dict(fake_gateway_kwargs or {})
        else:
            # 启动预检: 真网关缺账号/路径时给能看懂的报错, 而不是 QMT 的 rc=-1
            if not config.account_id or not config.qmt_path:
                raise RuntimeError(
                    "真网关需要 account_id 与 qmt_path(miniQMT 的 userdata_mini 目录), "
                    "请在 --config 指定的 yaml 中配置; 或用 --fake 跑测试模式")
            gw_kwargs = {"account_id": config.account_id,
                         "mini_qmt_path": config.qmt_path}
        self.gateway = gw_cls(
            on_order=_wire(self._engine, EVENT_ORDER_UPDATE),
            on_trade=_wire(self._engine, EVENT_TRADE_FILL),
            on_quote=lambda code, q: _wire(self._engine, EVENT_TICK)(
                {"code": code, **q}),
            on_disconnected=_wire(self._engine, EVENT_CONNECTION_LOST),
            **gw_kwargs,
        )

        self.reconciler = Reconciler(
            self.gateway, self.book, self.store, self.kill,
            quote_price=lambda code: (q := self.monitor.quote_of(code)) and q["last"],
            in_flight_sells=lambda: self.executor.in_flight_sells(),
        )
        self.executor = Executor(
            self.gateway, self.book, self.store, self.risk, config,
            build_risk_ctx=self._build_risk_ctx,
            get_quote=lambda code: self.monitor.quote_of(code),
            get_prev_close=self._prev_close,
            clock=clock,
        )
        self.monitor = Monitor(
            self.gateway, self.book, self.executor, self.store, config,
            clock=clock,
        )
        self.timer = _DailyTimer(self._engine, config, clock=clock)

    # ═══════════════════════════════════════════════════════════
    # 生命周期
    # ═══════════════════════════════════════════════════════════

    def start(self, start_timers: bool = True) -> bool:
        """启动顺序铁律: connect → 冷启动灌仓 → 启动对账 → 通过才放行。
        返回启动对账是否通过 (不通过 = 已急停, 调用方应告警人工介入)。"""
        self.gateway.connect()
        self._connected = True
        today = time.strftime("%Y%m%d", time.localtime(self._clock()))
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
        report = self.reconciler.reconcile()
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
        - 15:05 后启动且当日无 EOD 快照 → 补 EOD (对账 C 方次日基准)。
        """
        hhmm = time.strftime("%H:%M", time.localtime(self._clock()))
        tiers_today = self.store.load_tier_states(today)
        if "09:25" <= hhmm <= "15:00" and not tiers_today:
            self.store.write_audit(
                "ladder_catchup", f"启动已过 09:25 ({hhmm}) 且当日未预埋, 补偿预埋",
                {"hhmm": hhmm})
            self.executor.place_ladder(today)
        if hhmm >= "15:05":
            snap = self.store.load_position_snapshot()
            day_start = time.mktime(time.strptime(today, "%Y%m%d"))
            snap_ts = max((p.get("ts", 0.0) for p in snap.values()), default=0.0)
            if snap_ts < day_start:
                self.store.write_audit(
                    "eod_catchup", f"15:05 后启动 ({hhmm}) 且当日无 EOD, 补偿归档",
                    {"hhmm": hhmm})
                self._on_eod()

    def stop(self) -> None:
        """优雅退出: 先停事件源 (定时器), 再停消费者, 最后断网关/关库。"""
        self.timer.stop()
        self._engine.stop()
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
            self.executor.execute_exit(cmd["code"], "manual_sell: 人工卖出",
                                       manual=True)
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
                cmd.get("date_str") or time.strftime("%Y%m%d", time.localtime(self._clock())))
        elif action == "auto_buy":
            self._start_auto_buy(cmd.get("source", "manual"))
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

    def _on_trade(self, rec: dict) -> None:
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
        try:
            self.store.save_trade(rec)
        except Exception as e:
            _logger.error("成交落库异常 (账本已更新): %s", e)

    def _on_quote_event(self, data: dict, event_ts: float | None = None) -> None:
        # 审计M4修复: 事件自带 ts 透传给 monitor (心跳用生产时刻,
        # 不用消费时刻; 过旧 tick 在 monitor 侧丢弃)
        self.monitor.on_quote(data["code"], data, event_ts=event_ts)

    def _on_scan(self, data: dict) -> None:
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
        report = self.reconciler.reconcile()
        # P0-③: UNKNOWN = 查询不可用 (疑似断线), 维持 reconciled 现状 —
        # 既不是"通过"也不是"不通过", 等下轮心跳/重连后再判
        if report.level == "UNKNOWN":
            return
        # 对账不通过不自动解禁 reconciled —— 急停只许人工解除,
        # reconciled 同理: 一旦 False 就要等人工 unkill 后才允许重置
        self._reconciled = report.passed and not self.kill.is_active()

    def _on_eod(self) -> None:
        # 持仓快照归档 = 对账 C 方的明日基准; tier_state 在乐观标记时
        # 已逐笔落库 (executor.place_ladder), 此处无需重复归档
        self.store.save_position_snapshot(self.gateway.query_positions())
        self.store.write_audit("eod", "EOD 持仓快照已归档", {})

    def _on_connection_lost(self, reason) -> None:
        """断线守护: 指数退避重连 → 重新订阅 → 全量对账 (计划书 §5.5)。
        在消费者线程内跑 —— 断线期间事件排队不消费, 本来就不能交易。"""
        self._connected = False
        self.store.write_audit("disconnect", f"连接断开: {reason}", {})
        backoff = 1.0
        while True:
            try:
                self.gateway.connect()
                break
            except Exception as e:
                _logger.warning("重连失败, %.0fs 后重试: %s", backoff, e)
                time.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX_SEC)
        # 重连成功: 重新订阅 → 全量对账 (推送丢失靠对账自愈)
        self._connected = True
        codes = sorted(self.book.snapshot()["positions"])
        if codes:
            self.gateway.subscribe_quotes(codes)
        self.store.write_audit("reconnect", "重连成功, 已重新订阅", {})
        self._on_reconcile()

    # ═══════════════════════════════════════════════════════════
    # 尾盘自动选股买入 (2026-07-27 MVP)
    # ═══════════════════════════════════════════════════════════

    def _start_auto_buy(self, source: str) -> None:
        """发起一次尾盘选股 (消费者线程内只开线程, 绝不自己跑 TDX)。
        scheduled 要求 enabled; manual (api 立即执行) 任何时段放行 —
        2026-07-27 裁决①同款语义: 人工命令不受时段/开关约束。"""
        if source == "scheduled" and not self._cfg.auto_buy.enabled:
            return  # 未启用: 定时事件静默丢弃 (面板里有关闭语义)
        if self._auto_buy_running:
            self.store.write_audit(
                "auto_buy_skip", "上一次选股仍在运行, 本次忽略",
                {"source": source})
            return
        self._auto_buy_running = True
        threading.Thread(target=self._auto_buy_worker, args=(source,),
                         name="auto-buy-selection", daemon=True).start()
        self.store.write_audit(
            "auto_buy_start", f"尾盘选股已发起 ({source})", {"source": source})

    def _auto_buy_worker(self, source: str) -> None:
        """工作线程: TDX 选股阻塞可达 60s, 绝不能跑在消费者线程
        (唯一写者卡死 = 全系统停摆)。跑完只 put 事件, 不碰任何状态。"""
        cfg = self._cfg.auto_buy
        try:
            signals = self._selection_runner(
                cfg.formula_name, cfg.formula_arg, dict(cfg.universe))
            self._engine.put(Event(type=EVENT_SIGNALS, ts=self._clock(),
                                   data={"signals": signals, "source": source}))
        except Exception as e:
            _logger.exception("尾盘选股工作线程异常")
            self._engine.put(Event(type=EVENT_SIGNALS, ts=self._clock(),
                                   data={"signals": None, "error": str(e),
                                         "source": source}))

    def _on_signals(self, data: dict) -> None:
        """选股结果处理 (消费者线程): 错误记 audit 页面可见;
        正常结果逐票过滤执行。"""
        self._auto_buy_running = False
        if data.get("signals") is None:
            self._auto_buy_last = {
                "ts": self._clock(), "source": data.get("source"),
                "error": data.get("error", "未知错误"), "dispositions": [],
            }
            self.store.write_audit(
                "auto_buy_error", f"尾盘选股失败: {data.get('error')}",
                {"source": data.get("source")})
            return
        self._execute_auto_buys(data["signals"], data.get("source", "?"))

    def _execute_auto_buys(self, signals: list[dict], source: str) -> None:
        """逐票过滤执行 (消费者线程)。过滤顺序 = 便宜到贵:
        上限 → 已持仓 → ETF → 今日已买过 → 涨停 → 现金/数量 → 风控。"""
        cfg = self._cfg.auto_buy
        today = time.strftime("%Y%m%d", time.localtime(self._clock()))
        hhmm = time.strftime("%H:%M", time.localtime(self._clock()))
        # 当日已下单代码 (跨批次防重; 批次内也靠它) — 按日重置
        if self._auto_buy_placed[0] != today:
            self._auto_buy_placed = (today, set())
        placed_codes = self._auto_buy_placed[1]
        day_start = time.mktime(time.strptime(today, "%Y%m%d"))
        bought_codes = self.store.bought_codes_since(day_start)
        try:
            cash = self.gateway.query_asset()["cash"]
        except Exception:
            self.store.write_audit(
                "auto_buy_error", "查询资金失败, 本轮自动买入中止", {})
            return

        positions = self.book.snapshot()["positions"]
        # 2026-07-27 首次实跑修复: 候选票批量取行情。
        # monitor 缓存只覆盖持仓/订阅票, 信号票从未查过 → 61/61 全"无行情"。
        # 批量查一次注入, 缓存兜底(持仓票)。
        quotes: dict = {}
        codes = [s["code"] for s in signals]
        try:
            quotes = self.gateway.query_quotes(codes) or {}
        except Exception:
            self.store.write_audit(
                "auto_buy_error", f"批量查询行情失败({len(codes)}只), 本轮自动买入中止", {})
            return
        dispositions: list[dict] = []
        bought = 0
        for sig in signals:
            code = sig["code"]
            if bought >= cfg.max_buys_per_day:
                dispositions.append({"code": code, "action": "skip",
                                     "reason": "达每日上限"})
                continue
            pos = positions.get(code)
            if pos is not None and pos.volume > 0:
                dispositions.append({"code": code, "action": "skip",
                                     "reason": "已持仓"})
                continue
            if self._cfg.exclude_etf and is_etf(code):
                dispositions.append({"code": code, "action": "skip",
                                     "reason": "ETF不管理"})
                continue
            if code in placed_codes or code in bought_codes:
                dispositions.append({"code": code, "action": "skip",
                                     "reason": "今日已买过"})
                continue
            # 涨停拒买 + 定价: 都需要行情。无价/无昨收 fail-closed —
            # 尾盘买入不是救火, 宁可不买不可瞎买
            quote = quotes.get(code) or self.monitor.quote_of(code)
            if not quote or not quote.get("last"):
                dispositions.append({"code": code, "action": "skip",
                                     "reason": "无行情"})
                continue
            prev_close = quote.get("prev_close") or self._prev_close(code)
            if not prev_close:
                dispositions.append({"code": code, "action": "skip",
                                     "reason": "无昨收无法判涨停"})
                continue
            limit_up = prev_close * (1 + limit_ratio(code))
            if quote["last"] >= limit_up:
                dispositions.append({"code": code, "action": "skip",
                                     "reason": "涨停拒买"})
                continue
            price = quote.get("ask1") or quote["last"]
            amount = min(cfg.amount_per_stock, cash * 0.95)
            qty = int(amount / price / 100) * 100
            if qty < 100:
                dispositions.append({"code": code, "action": "skip",
                                     "reason": "现金不足一手"})
                continue
            intent = OrderIntent(code=code, direction=DIRECTION_BUY,
                                 price=price, qty=qty)
            ok, why = self.risk.check(intent, self._build_risk_ctx())
            if not ok:
                dispositions.append({"code": code, "action": "skip",
                                     "reason": f"风控拒: {why}"})
                continue
            # 定价 (2026-07-27 实测驱动, 市场感知):
            # - 正常时段: 卖一价限价;
            # - ≥force_market_after 的 .SZ: 限价@涨停价 —— 深市收盘
            #   集合竞价只收限价单 (当日实测五张深市"对手最优"全废单),
            #   单一价格撮合, 挂涨停=最大成交优先权, 成交价仍是收盘价;
            # - ≥force_market_after 的 .SH / 取不到卖一: 对手最优
            #   (沪市连续竞价到 15:00 可市价单)
            force = hhmm >= self._cfg.force_market_after
            if force and code.split(".")[-1].upper() == "SZ":
                order_price = round_price(limit_up)
                order_type = PRICE_TYPE_LIMIT
                price_kind = "涨停价限价(收盘竞价)"
            elif force or not quote.get("ask1"):
                order_price = 0.0
                order_type = PRICE_TYPE_MARKET_PEER_FIRST
                price_kind = "对手最优"
            else:
                order_price = price
                order_type = PRICE_TYPE_LIMIT
                price_kind = "卖一价"
            remark = self.executor.next_remark("B")
            order_id = self.gateway.order(
                code, DIRECTION_BUY, order_price, qty, order_type, remark)
            self.book.apply_order_update(
                order_id, OS_REPORTED, code=code, direction=DIRECTION_BUY,
                price=price, qty=qty, remark=remark)
            self.store.save_order({
                "order_id": order_id, "remark": remark, "code": code,
                "direction": DIRECTION_BUY, "price": price, "qty": qty,
                "status": OS_REPORTED})
            placed_codes.add(code)
            cash -= qty * price
            bought += 1
            dispositions.append({"code": code, "action": "buy",
                                 "price": order_price or price, "qty": qty,
                                 "order_id": order_id, "reason": price_kind})
            self.store.write_audit(
                "auto_buy",
                f"尾盘买入 {code} {qty}@{order_price or price} ({price_kind})",
                {"code": code, "qty": qty, "price": order_price or price,
                 "order_id": order_id, "source": source,
                 "price_kind": price_kind})

        self._await_and_fill_dispositions(dispositions)
        summary = {
            "ts": self._clock(), "source": source,
            "selected": len(signals), "bought": bought,
            "dispositions": dispositions,
        }
        self._auto_buy_last = summary
        # 汇总落 audit (结构化 detail_json 即持久化, 页面可读)
        self.store.write_audit(
            "auto_buy_summary",
            f"尾盘自动买入: 选中 {len(signals)} / 买入 {bought} ({source})",
            {"selected": len(signals), "bought": bought,
             "dispositions": dispositions})

    def _await_and_fill_dispositions(self, dispositions: list[dict]) -> None:
        """下单后轮询一次, 把每张单的最终状态补进 disposition
        (2026-07-27 实测驱动: 五张废单就是这么发现的 —— "下单即记 buy"
        会掩盖废单)。终态语义: 已成@均价 / 废单(状态码) / 在途。
        注意这会阻塞消费者线程最多 ~10s —— 14:52 尾盘窗口可接受
        (监控腿下一轮扫描照常), 白天其他时段只有人工触发才走到。"""
        buys = [d for d in dispositions if d["action"] == "buy"]
        if not buys:
            return
        deadline = time.time() + self._await_timeout
        pending_ids = {d["order_id"] for d in buys}
        while pending_ids and time.time() < deadline:
            orders = {o["order_id"]: o for o in self.gateway.query_orders()}
            pending_ids = {oid for oid in pending_ids
                           if orders.get(oid, {}).get("status")
                           not in TERMINAL_STATUSES}
            if not pending_ids:
                break
            time.sleep(self._await_interval)
        trades = {t["order_id"]: t for t in self.gateway.query_trades()}
        orders = {o["order_id"]: o for o in self.gateway.query_orders()}
        for d in buys:
            o = orders.get(d["order_id"], {})
            status = o.get("status")
            if status == OS_SUCCEEDED:
                fill_px = trades.get(d["order_id"], {}).get("price",
                                                            d["price"])
                d["status"] = f"已成@{fill_px}"
            elif status in TERMINAL_STATUSES:
                d["status"] = f"废单(状态{status})"
            else:
                d["status"] = "在途"

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
                             price=float(price), qty=qty)
        ok, reason = self.risk.check(intent, self._build_risk_ctx())
        if not ok:
            _logger.warning("人工买入被风控拒绝: %s %s", code, reason)
            return  # 拒绝审计 risk 层已写
        # 审计M1修复: 人工买入 remark 也走 executor 的单调发号器,
        # 不再自造 V{mmdd}-B{qty} (同量两笔必重号, 破坏对账唯一性)
        remark = self.executor.next_remark("B")
        order_id = self.gateway.order(code, DIRECTION_BUY, float(price), qty,
                                      PRICE_TYPE_LIMIT, remark)
        self.store.write_audit(
            "manual_buy", f"人工买入 {code} {qty}@{price}",
            {"code": code, "qty": qty, "price": price, "order_id": order_id})
        # 回报会经事件链自然入账, 这里只留人工动作痕迹

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
            is_trading_day=True,  # TODO P2: 接交易日历
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
        """最近一次尾盘自动买入运行结果 (页面展示, 只读)。"""
        return self._auto_buy_last


def main() -> None:
    parser = argparse.ArgumentParser(description="VERA 实盘交易系统")
    parser.add_argument("--config", default="config/trade.yaml",
                        help="交易配置 yaml (缺字段走 TradeConfig 默认值)")
    parser.add_argument("--fake", action="store_true",
                        help="强制使用 FakeGateway (等价 VERA_TRADE_FAKE=1)")
    parser.add_argument("--api-port", type=int, default=8081)
    args = parser.parse_args()

    config = (load_trade_config(args.config) if Path(args.config).exists()
              else TradeConfig())
    app = TradeApp(config, fake=True if args.fake else None,
                   config_path=args.config)
    ok = app.start()
    _logger.info("交易系统已启动 (fake=%s, 启动对账=%s)",
                 args.fake or config.fake_sdk, "通过" if ok else "未通过")

    from trade.api import create_api_app
    import uvicorn
    try:
        uvicorn.run(create_api_app(app), host="127.0.0.1",
                    port=args.api_port, access_log=False)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        _logger.info("交易系统已退出")


if __name__ == "__main__":
    main()
