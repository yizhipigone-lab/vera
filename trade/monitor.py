"""trade/monitor.py — 盘中监控腿 (计划书 §5.2)。

设计意图:
    订阅为主、心跳探活、轮询兜底。动态状态规则 (移动止盈/硬止损/
    时间止损) 在这里评估 —— 静态价位规则不归我管 (executor 的预埋单)。
    行情数据缺失一律 fail-closed: 该票本轮跳过+WARN, 宁可不卖, 不可瞎卖
    (QP 教训: 不返回空持仓冒充"无命中")。
    本模块只做评估与转发, 卖出动作全部交给 executor。
"""

from __future__ import annotations

import datetime as _dt
import time
from typing import Callable

from scheduler.trading_calendar import is_trading_day as _cal_is_trading_day
from trade.book import is_etf, ladder_tier_qty
from trade.quote_stale import REASON_STALE, is_quote_stale
from utils.logger import get_logger

_logger = get_logger("trade.monitor")

# 时段文案 (status API / 前端共用, 全项目只此一份)
SESSION_NAMES = {"pre_open": "盘前", "auction": "集合竞价",
                 "continuous": "盘中", "lunch": "午休中", "closed": "已收盘"}

# D5 (2026-08-01): 节假日日历统一 —— trading_session 原只判周末
# (TODO P2), 现接 scheduler.trading_calendar (exchange_calendars 精确历,
# 缺失时降级内置 2026 假日表, 其自身松耦合不抛异常)。盘中热路径
# (scan 每轮都调) 按日 memoize; 日历万一异常回落周末判定
# (fail-open, 与 D5 前行为一致, 不让日历故障压制盘中规则)。
_TRADING_DAY_CACHE: dict[str, bool] = {}


def is_trading_day_cached(d: _dt.date | None = None) -> bool:
    """按日 memoize 的交易日判定 (D5)。trade_main 风险闸也经此取值,
    与 trading_session 同一口径、同一份缓存。"""
    day = (d or _dt.date.today())
    key = day.isoformat()
    hit = _TRADING_DAY_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        v = bool(_cal_is_trading_day(day))
    except Exception as e:  # 日历故障不阻断盘中判定
        _logger.warning("交易日历异常, 回落周末判定 (fail-open): %s", e)
        v = day.weekday() < 5
    _TRADING_DAY_CACHE[key] = v
    return v


def _ladder_tier_qty(volume: int, ratio: float) -> int | None:
    """阶梯兜底档的卖出数量, 口径对齐 executor.place_ladder:
    ratio<1 → 比例手数四舍五入 (0.5 边界向上, 计算单一真相源
    trade/book.ladder_tier_qty, 2026-09-16 P2-1 收口); ratio≥1 → 清仓档。
    返回 None = 卖全部可用 (execute_exit 的 qty=None 语义) —— 用于
    清仓档、比例档算不出整手、或 volume 未知 (<=0): 兜底语义宁可
    全卖不漏卖, 与 2026-08-06 前的旧行为一致。"""
    if ratio >= 1.0 or volume <= 0:
        return None
    qty = ladder_tier_qty(volume, ratio, int(volume / 100))
    return qty if qty > 0 else None


def trading_session(now: float | None = None) -> str:
    """A 股交易时段判定 (2026-07-27 ETF 误卖事件裁决①):
    自动规则只在连续竞价跑, 人工命令任何时段放行。

    返回: pre_open(<09:15) / auction(09:15-09:30) /
    continuous(09:30-11:30 ∪ 13:00-15:00) / lunch(11:30-13:00) /
    closed(≥15:00 及非交易日)。D5 (2026-08-01): 非交易日判定由
    "周末"升级为节假日日历 (is_trading_day_cached, 含法定节假日)。
    now 为 epoch 秒, 可注入便于测试; None 取当前。
    """
    t = time.localtime(now) if now is not None else time.localtime()
    if not is_trading_day_cached(_dt.date(t.tm_year, t.tm_mon, t.tm_mday)):
        return "closed"
    hm = t.tm_hour * 60 + t.tm_min
    if hm < 9 * 60 + 15:
        return "pre_open"
    if hm < 9 * 60 + 30:
        return "auction"
    if hm < 11 * 60 + 30:
        return "continuous"
    if hm < 13 * 60:
        return "lunch"
    if hm < 15 * 60:
        return "continuous"
    return "closed"


class Monitor:
    """行情缓存 + 健康检测 + 动态规则评估。公开接口:
    on_quote / quote_of / scan_once / pending_check / is_healthy / has_tick (6 个)。"""

    # 审计降噪 (2026-09-05 体检 P2-1): monitor_no_quote/stale_quote 逐轮刷库
    # (14 天 7070+133 条) 淹没真告警。同 code 同类 15 分钟最多落一条;
    # 首现立即写, 状态翻转/recover 等不走过道, 真信号不丢。
    _AUDIT_THROTTLE_SEC = 900.0

    def __init__(
        self,
        gateway,
        book,
        executor,
        store,
        config,
        hold_days: Callable[[str], int] | None = None,
        peak_px: Callable[[str], "float | None"] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._gw = gateway
        self._book = book
        self._executor = executor
        self._store = store
        self._cfg = config
        # 持有天数来源 (MVP 由 root 注入, 实盘来自 EOD 归档的建仓日)
        self._hold_days = hold_days or (lambda code: 0)
        # 持仓期历史峰值来源 (2026-08-06): None=取不到, 回退当日口径
        self._peak_px = peak_px or (lambda code: None)
        self._clock = clock
        self._quotes: dict[str, dict] = {}   # code -> {last, bid1, high, ts}
        self._last_tick_ts: float | None = None
        self._healthy = True
        self._triggered: set[str] = set()    # 当日已触发票, 防同票连环触发
        # 审计H2修复: _triggered 的日期戳, scan_once 跨日清空
        self._triggered_date = time.strftime("%Y%m%d", time.localtime(self._clock()))
        # 审计节流账: kind -> {key: 上次落库时间}
        self._audit_last: dict[str, dict[str, float]] = {}

    def apply(self, cfg) -> None:
        """热更契约 (治理III W2-1): 换配置引用。cfg 用时读属性, 换引用即热。"""
        self._cfg = cfg

    def clear_trigger(self, code: str) -> None:
        """解除当日触发标记 (2026-08-01 P0-3): executor pending 终态为
        废单/已撤且持仓仍在时回调, 该票下轮扫描重新评估。
        (2026-09-05 唯一下单口收口 T6: 转公开 —— 组合根接线不再摸
        _triggered 私有集合)"""
        self._triggered.discard(code)

    def _write_throttled(self, kind: str, key: str, message: str,
                         detail: dict | None = None) -> None:
        """按 (kind, key) 节流写审计: 首现即写, 窗口内同 key 跳过。

        供逐轮刷屏的 monitor_no_quote/monitor_stale_quote 用 —— 持续缺失/
        陈旧是稳态不是新事件, 每轮都落库只产生噪音; 恢复/触发等真状态
        翻转走直写不经过这里。"""
        bucket = self._audit_last.setdefault(kind, {})
        now = self._clock()
        last = bucket.get(key)
        if last is not None and (now - last) < self._AUDIT_THROTTLE_SEC:
            return
        bucket[key] = now
        self._store.write_audit(kind, message, detail or {})

    # ── 行情入口 ────────────────────────────────────────────────

    def on_quote(self, code: str, quote: dict,
                 event_ts: float | None = None) -> None:
        """quote 回调 (网关→事件→这里, 由 root 接线)。
        更新最新价/买一价缓存 + 最后 tick 时间 (心跳依据)。
        当日最高取 max(行情 high, 历史缓存) —— 轮询快照的 high 口径
        与 tick 推送可能不同源, 取大不取新。

        2026-08-01 H2/H3: _quotes 条目带日期戳, 跨日重置 high/prev_close
        —— 昨日最高不能当今日峰值, 昨日收盘不能当今日昨收。

        审计M4修复:
        - event_ts 是 tick 事件自带的生产时刻; 与本地时钟偏差 >30s
          的旧 tick 直接丢弃+WARN —— 断线重连后积压的tick不能盖成
          "消费时刻"冒充新鲜行情;
        - 心跳 (_last_tick_ts) 用事件自带 ts 而非消费时刻, 否则积压
          事件会把心跳误判成健康。
        """
        now = self._clock()
        ts = event_ts if event_ts is not None else now
        if abs(now - ts) > 30.0:
            self._store.write_audit(
                "monitor_stale_tick",
                f"{code} tick 事件过旧 (偏差 {now - ts:.0f}s), 丢弃",
                {"code": code, "event_ts": ts})
            return
        today = time.strftime("%Y%m%d", time.localtime(now))
        cur = self._quotes.get(code, {})
        # H2/H3: 跨日重置 —— 昨日 high/prev_close 不能污染今日
        if cur.get("_date") != today:
            cur = {}
            cur["_date"] = today
        high = max(float(quote.get("high") or 0.0), cur.get("high", 0.0))
        # 2026-07-31: 缓存昨收 (持仓页"当日涨跌幅/涨跌金额"的数据源)。
        # tick 缺 lastClose (盘前/个别快照) 时保留已有缓存值
        prev_close = (float(quote.get("prev_close") or 0.0)
                      or cur.get("prev_close", 0.0))
        # ts 三种来源: ① quote 显式带 ts (生产 tick, 可能是 None ——
        # 2026-07-27 裁决③: tick 缺时间戳时 gateway 不再兜底 time.time,
        # None 必须被视为陈旧, 否则半夜一条无戳 tick 就能驱动自动规则)
        # ② 无 ts 键 (测试/Fake 裸 dict) → 回退事件/本地时刻
        tick_ts_missing = ("ts" in quote) and (quote["ts"] is None)
        effective_ts = ts if "ts" not in quote or tick_ts_missing else quote["ts"]
        self._quotes[code] = {
            "last": float(quote.get("last") or 0.0),
            "bid1": float(quote.get("bid1") or 0.0),
            # ask1 也要缓存: 尾盘自动买入按卖一价定价 (2026-07-27 MVP);
            # 买单对手最优分支已于 2026-08-12 移除 (auto_buy.py), 丢了它
            # 买单无价可定
            "ask1": float(quote.get("ask1") or 0.0),
            "high": high,
            "prev_close": prev_close,
            "ts": effective_ts,
            "tick_ts_missing": tick_ts_missing,
            "_date": today,  # H2/H3: 跨日重置用日期戳
        }
        self._last_tick_ts = ts

    def quote_of(self, code: str) -> dict | None:
        """读某票最新快照 (executor 的买一价来源, root 接线用)。"""
        return self._quotes.get(code)

    # ── 健康检测 ────────────────────────────────────────────────

    def is_healthy(self) -> bool | None:
        """心跳健康三态: True 订阅健康 / False 降级轮询 /
        None 非连续竞价时段 (不适用)。2026-07-27 裁决②: 心跳只在
        连续竞价判定 —— 午休/收盘后无 tick 是常态不是断线,
        此前会把页面误报成 connected:false。"""
        if trading_session(self._clock()) != "continuous":
            return None
        return self._healthy

    def has_tick(self) -> bool:
        """是否收到过 tick。P0-③ 修复需要区分两种"不健康":
        启动后从未收到 tick (盘前静默, 正常) vs 盘中断流 (真断线)
        —— 只有后者该触发断线重连。"""
        return self._last_tick_ts is not None

    def _check_health(self) -> None:
        """开盘时段超 tick_heartbeat_sec 无 tick → 降级轮询兜底;
        恢复后自动切回。状态切换都写 audit (静默降级是事故温床)。"""
        stale = (self._last_tick_ts is None
                 or self._clock() - self._last_tick_ts > self._cfg.tick_heartbeat_sec)
        if stale and self._healthy:
            self._healthy = False
            self._store.write_audit(
                "monitor_degrade", "行情订阅心跳超时, 降级为轮询兜底", {})
            _logger.warning("行情订阅不健康, 降级轮询")
        elif not stale and not self._healthy:
            self._healthy = True
            self._store.write_audit(
                "monitor_recover", "行情订阅恢复, 切回订阅驱动", {})

    def _poll_quotes(self, codes: list[str]) -> None:
        """轮询兜底: 查快照后走与 tick 相同的 on_quote 路径
        (两条腿一个评估口径, 不搞双份规则)。
        P0-③ 实测确认: 断线时 query_quotes 抛异常或返回空 ——
        两者都在这里被吞, 扫描里逐票无价/陈旧 fail-closed,
        本轮不动作, 不会把"查询不可用"误判成"行情正常"。"""
        try:
            snapshots = self._gw.query_quotes(codes)
        except Exception:
            # 轮询也失败 = 行情全断, 本轮 fail-closed (扫描里逐票无价跳过)
            _logger.exception("轮询兜底查询失败, 本轮监控不动作")
            return
        for code, quote in snapshots.items():
            self.on_quote(code, quote)

    # ── 扫描评估 ────────────────────────────────────────────────

    def scan_once(self, now_hhmm: str | None = None) -> list[tuple[str, str]]:
        """遍历持仓评估动态规则, 命中调 executor.execute_exit。
        返回本论触发列表 [(code, reason)]。now_hhmm 供未成交升级判定。

        2026-07-27 ETF 误卖事件裁决①: 自动规则**仅在连续竞价时段**
        执行 —— 午休/收盘后/盘前直接跳过 (静默, 不每轮刷日志);
        人工命令不走路由此处, 任何时段放行。
        审计H2修复: _triggered 按日清理 —— 跨日先清空再评估,
        进程不重启时昨日触发的票今日规则照常生效。"""
        if trading_session(self._clock()) != "continuous":
            return []
        today = time.strftime("%Y%m%d", time.localtime(self._clock()))
        if today != self._triggered_date:
            self._triggered.clear()
            self._triggered_date = today
        self._check_health()
        positions = self._book.snapshot()["positions"]
        if not self._healthy:
            self._poll_quotes(sorted(positions))

        triggers: list[tuple[str, str]] = []
        for code, pos in sorted(positions.items()):
            if pos.volume <= 0 or code in self._triggered:
                continue
            quote = self._quotes.get(code)
            if not quote or quote["last"] <= 0:
                # 无价 fail-closed: 本轮跳过+WARN, 绝不按零价/无数据处理
                # (节流: 持续无行情是稳态, 同票 15 分钟只落一条, 体检 P2-1)
                self._write_throttled(
                    "monitor_no_quote", code, f"{code} 无行情快照, 本轮跳过",
                    {"code": code})
                continue
            # 审计M6修复: 陈旧快照与无快照同等 fail-closed —— 单票订阅
            # 丢失时全局心跳兜不住, 陈旧价驱动卖出比不卖更危险。
            # 判定统一走 trade/quote_stale.py (单一真相源): 无 ts / ts 为
            # None / tick_ts_missing / 超时 任一即陈旧 (2026-07-27 裁决③)。
            stale, reason = is_quote_stale(quote, self._clock(),
                                           self._cfg.quote_stale_sec)
            if stale:
                if reason == REASON_STALE:
                    msg = (f"{code} 行情快照陈旧 "
                           f"(>{self._cfg.quote_stale_sec}s), 本轮跳过")
                else:
                    # tick_ts_missing / no_ts / ts_none 同属"没可信时间戳"
                    msg = f"{code} tick 缺时间戳, 视为陈旧, 本轮跳过"
                # (节流: 持续陈旧是稳态, 同票 15 分钟只落一条, 体检 P2-1)
                self._write_throttled(
                    "monitor_stale_quote", code, msg,
                    {"code": code, "quote_ts": quote.get("ts")})
                continue
            result = self._evaluate(code, pos.avg_cost, quote, pos.volume)
            if result is None:
                continue
            reason, qty, tier = result
            # 审计H1修复: 执行成功才标记"已触发"。execute_exit 有大量
            # 合法 fail-closed 返回路径 (无买一价/可用为0/风控拒绝),
            # 先标记等于"一跳行情延迟换一整天无保护"
            if self._executor.execute_exit(code, reason, qty=qty):
                if tier is not None and qty is not None:
                    # 2026-08-06 阶梯兜底部分卖: 乐观标记该档 (对齐
                    # place_ladder "提交成功即标记, 废单也不重复卖"),
                    # 但不入 _triggered —— 剩余仓位继续受 trailing/
                    # cost_stop/高档位兜底保护 (预埋单世界的分工复原:
                    # 档已卖 = 已标记, 其余腿照常评估)
                    self._book.mark_tier(code, tier, today)
                    # 审计 P0-3 (2026-09-16): 补落 tier_state 表 —— 原只写
                    # 内存, 盘中重启后该档标记丢失, 同档可再次部分卖
                    # (双卖)。对齐 executor.place_ladder 的双写。
                    self._store.tier_state.save(
                        code, sorted(self._book.tier_done(code, today)), today)
                else:
                    self._triggered.add(code)
                triggers.append((code, reason))
            else:
                self._store.write_audit(
                    "exit_arm_fail",
                    f"{code} 触发 ({reason}) 但执行未成功, 未武装, 下轮再评",
                    {"code": code, "reason": reason})

        self._executor.pending_check(now_hhmm=now_hhmm)
        return triggers

    def pending_check(self, now_hhmm: str | None = None) -> None:
        """显式未成交检查 (扫描间隔外的独立调用口, 如更高频定时器)。
        升级动作同样只在连续竞价执行 (2026-07-27 裁决①)。"""
        if trading_session(self._clock()) != "continuous":
            return
        self._executor.pending_check(now_hhmm=now_hhmm)

    def _evaluate(self, code: str, avg_cost: float, quote: dict,
                  volume: int = 0) -> tuple[str, int | None, int | None] | None:
        """全规则评估, 复刻回测 ExitDispatcher 优先级语义
        [backtest/loop/exit_engine.py:46-53/99-139]。
        每条规则对齐回测单 bar 版本 (出处逐条标注), 数据缺失维持
        fail-closed。返回 (原因, 指定卖出数量, 阶梯档位) 或 None;
        数量/档位仅阶梯兜底部分卖时非 None, 其余规则恒 (reason, None, None)
        (None 数量 = 卖全部可用, execute_exit 口径)。

        与回测的已知口径差 (parity 测试头注同款):
        - bar low/close ≡ tick last; hi_pp 用当日行情 high;
        - trailing_first 的双触发 (ladder 部分卖 + trailing 全卖剩余)
          在实盘由 executor 预埋单承担部分卖 —— 本腿只对"未预埋档"
          兜底。2026-08-06 起兜底按档位比例部分卖 (不再一锅端):
          卖完标档不武装, 剩余仓位由其余规则继续保护。
        """
        stop = self._cfg.stop
        # 2026-08-16 卖出总开关: 关闭时整个监控腿不再评估任何卖出规则 (不新触发),
        # 已挂单子/持仓不动; 人工卖出走 dispatch_command → execute_exit(manual)
        # 不经本函数, 不受此开关限制。
        if not self._cfg.auto_sell_enabled:
            return None
        # 2026-07-27 ETF 误卖事件裁决③: ETF 不纳入自动管理,
        # 规则评估直接跳过 (持仓仍照常对账, 那是 reconciler 的事)
        if self._cfg.exclude_etf and is_etf(code):
            return None
        last = quote["last"]
        high = quote["high"]
        days = self._hold_days(code)
        # 峰值 = max(成本, 持仓期历史最高, 当日最高)。
        # 2026-08-06 P2 落地: 历史日高经 gateway 拉不复权日线 (与成本同口径),
        # 取不到时回退当日口径 (旧 MVP 行为), 与回测 peak-since-entry 对齐。
        try:
            hist_peak = self._peak_px(code) or 0.0
        except Exception:
            hist_peak = 0.0
        peak = max(avg_cost, high, hist_peak)
        today = time.strftime("%Y%m%d", time.localtime(self._clock()))

        # 优先级调度顺序 [exit_engine.py:46-53]: priority block 在前,
        # 公共尾部 time_stop → cond_time → first_day 恒在后
        _ORDER = {
            "stop_first": ("cost_stop", "ladder_tp", "trailing"),
            "ladder_tp_first": ("ladder_tp", "cost_stop", "trailing"),
            "trailing_first": ("ladder_tp", "trailing", "cost_stop"),
        }
        # 2026-07-31: 正文自然语言化 (成交记录"原因"列展示全文)。
        # 除法安全预计算 —— 字符串是无条件拼装的, 命中判定里的
        # avg_cost>0 守卫管不到这里
        peak_pct = (peak / avg_cost - 1) if avg_cost > 0 else 0.0
        high_pct = (high / avg_cost - 1) if avg_cost > 0 else 0.0
        dd_now = (1 - last / peak) if peak > 0 else 0.0
        checks = {
            "cost_stop": (lambda: self._hit_cost_stop(stop.cost_stop, avg_cost, last),
                          f"cost_stop: 现价 {last:.2f} 跌破止损线 "
                          f"{avg_cost * (1.0 + stop.cost_stop.threshold):.2f} "
                          f"(成本 {avg_cost:.2f} {stop.cost_stop.threshold:+.0%})"),
            "trailing": (lambda: self._hit_trailing(stop.trailing_stop, avg_cost,
                                                    peak, last),
                         f"trailing: 最高 {peak:.2f} (峰值涨幅 {peak_pct:+.1%}, "
                         f"过激活线 {stop.trailing_stop.activation:.1%}), "
                         f"现价 {last:.2f} 回撤 {dd_now:.2%} 触发 "
                         f"(阈值 {stop.trailing_stop.drawdown:.1%})"),
            "time_stop": (lambda: self._hit_time_stop(stop.time_stop, days),
                          f"time_stop: 持有 {days} 天达上限 "
                          f"{stop.time_stop.max_hold_days} 天"),
            "cond_time": (lambda: self._hit_cond_time(stop.cond_time_stop,
                                                      avg_cost, high, days),
                          f"cond_time: 持有 {days} 天, 当日最高涨幅 "
                          f"{high_pct:+.1%} 达门槛 {stop.cond_time_stop.profit:.0%}"),
            "first_day": (lambda: self._hit_first_day(stop.first_day,
                                                      avg_cost, high, days),
                          f"first_day: 首日最高涨幅 {high_pct:+.1%} "
                          f"未达 {stop.first_day.target:.0%}"),
        }
        for name in _ORDER[stop.priority] + ("time_stop", "cond_time",
                                             "first_day"):
            if name == "ladder_tp":
                tier = self._hit_ladder(stop.ladder_tp, avg_cost, high, today, code)
                if tier is not None:
                    profit, ratio = stop.ladder_tp.levels[tier]
                    return (f"ladder_tp: 最高 {high:.2f} 涨破档{tier + 1}线 "
                            f"{avg_cost * (1.0 + profit):.2f} "
                            f"(成本 {avg_cost:.2f} {profit:+.0%}, 未预埋兜底)",
                            _ladder_tier_qty(volume, ratio), tier)
                continue
            hit, reason = checks[name]
            if hit():
                return reason, None, None
        return None

    def _hit_cost_stop(self, c, avg_cost: float, last: float) -> bool:
        """[cost_stop.py:28] lo_pp ≤ threshold ≡ low ≤ ep×(1+threshold),
        threshold 负值口径 (回测 config 同)。"""
        return (c.enabled and avg_cost > 0
                and last <= avg_cost * (1.0 + c.threshold))

    def _hit_trailing(self, t, avg_cost: float, peak: float, last: float) -> bool:
        """[trailing.py:35-39] 先过 activation 激活线 (峰值涨幅),
        再判现价跌破 峰值×(1-drawdown) —— 2026-07-26 裁决前实盘缺激活线,
        现已补齐复刻。"""
        if not t.enabled or avg_cost <= 0:
            return False
        if (peak - avg_cost) / avg_cost < t.activation:
            return False
        return last <= peak * (1.0 - t.drawdown)

    def _hit_ladder(self, lv, avg_cost: float, high: float,
                    today: str, code: str) -> int | None:
        """[ladder_tp.py:37-53] High 涨破新档位即触发。实盘的档位执行在
        券商端 (executor 预埋限价单), 本腿只对"未预埋档"兜底 —— 已标记档
        券商自己会成交, 再触发就是双卖。返回档位序号或 None。"""
        if not lv.enabled or avg_cost <= 0:
            return None
        done = self._book.tier_done(code, today)
        for i, (profit, _ratio) in enumerate(lv.levels):
            if i in done:
                continue
            if high >= avg_cost * (1.0 + profit):
                return i
        return None

    def _hit_time_stop(self, t, days: int) -> bool:
        """[time_stop.py:23] 到点即走 (回测无收益门槛;
        旧 trade 私设的 min_gain 已随裁决①删除)。"""
        return t.enabled and days >= t.max_hold_days

    def _hit_cond_time(self, c, avg_cost: float, high: float, days: int) -> bool:
        """[cond_time.py:25] 持仓 ≥ days 且当日最高涨幅 ≥ profit。"""
        return (c.enabled and avg_cost > 0 and days >= c.days
                and (high - avg_cost) / avg_cost >= c.profit)

    def _hit_first_day(self, f, avg_cost: float, high: float, days: int) -> bool:
        """[first_day.py:28-40] 首个可交易日 (T+1 即 hold_days==1)
        日内最高涨幅 < target 即卖。回测在当日最后一根 bar 判定,
        实盘无 bar 收盘概念取"当日"粒度 (1d bpday=1 口径相同)。"""
        return (f.enabled and avg_cost > 0 and days == 1
                and (high - avg_cost) / avg_cost < f.target)
