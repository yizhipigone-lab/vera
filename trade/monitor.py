"""trade/monitor.py — 盘中监控腿 (计划书 §5.2)。

设计意图:
    订阅为主、心跳探活、轮询兜底。动态状态规则 (移动止盈/硬止损/
    时间止损) 在这里评估 —— 静态价位规则不归我管 (executor 的预埋单)。
    行情数据缺失一律 fail-closed: 该票本轮跳过+WARN, 宁可不卖, 不可瞎卖
    (QP 教训: 不返回空持仓冒充"无命中")。
    本模块只做评估与转发, 卖出动作全部交给 executor。
"""

from __future__ import annotations

import time
from typing import Callable, Mapping

from trade.book import is_etf
from utils.logger import get_logger

_logger = get_logger("trade.monitor")

# 时段文案 (status API / 前端共用, 全项目只此一份)
SESSION_NAMES = {"pre_open": "盘前", "auction": "集合竞价",
                 "continuous": "盘中", "lunch": "午休中", "closed": "已收盘"}


def trading_session(now: float | None = None) -> str:
    """A 股交易时段判定 (2026-07-27 ETF 误卖事件裁决①):
    自动规则只在连续竞价跑, 人工命令任何时段放行。

    返回: pre_open(<09:15) / auction(09:15-09:30) /
    continuous(09:30-11:30 ∪ 13:00-15:00) / lunch(11:30-13:00) /
    closed(≥15:00 及周末)。TODO P2: 节假日日历 (当前只判周末+时刻)。
    now 为 epoch 秒, 可注入便于测试; None 取当前。
    """
    t = time.localtime(now) if now is not None else time.localtime()
    if t.tm_wday >= 5:
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
    on_quote / quote_of / scan_once / pending_check / is_healthy (5 个)。"""

    def __init__(
        self,
        gateway,
        book,
        executor,
        store,
        config,
        hold_days: Callable[[str], int] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._gw = gateway
        self._book = book
        self._executor = executor
        self._store = store
        self._cfg = config
        # 持有天数来源 (MVP 由 root 注入, 实盘来自 EOD 归档的建仓日)
        self._hold_days = hold_days or (lambda code: 0)
        self._clock = clock
        self._quotes: dict[str, dict] = {}   # code -> {last, bid1, high, ts}
        self._last_tick_ts: float | None = None
        self._healthy = True
        self._triggered: set[str] = set()    # 当日已触发票, 防同票连环触发
        # 审计H2修复: _triggered 的日期戳, scan_once 跨日清空
        self._triggered_date = time.strftime("%Y%m%d", time.localtime(self._clock()))

    # ── 行情入口 ────────────────────────────────────────────────

    def on_quote(self, code: str, quote: dict,
                 event_ts: float | None = None) -> None:
        """quote 回调 (网关→事件→这里, 由 root 接线)。
        更新最新价/买一价缓存 + 最后 tick 时间 (心跳依据)。
        当日最高取 max(行情 high, 历史缓存) —— 轮询快照的 high 口径
        与 tick 推送可能不同源, 取大不取新。

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
        cur = self._quotes.get(code, {})
        high = max(float(quote.get("high") or 0.0), cur.get("high", 0.0))
        # ts 三种来源: ① quote 显式带 ts (生产 tick, 可能是 None ——
        # 2026-07-27 裁决③: tick 缺时间戳时 gateway 不再兜底 time.time,
        # None 必须被视为陈旧, 否则半夜一条无戳 tick 就能驱动自动规则)
        # ② 无 ts 键 (测试/Fake 裸 dict) → 回退事件/本地时刻
        tick_ts_missing = ("ts" in quote) and (quote["ts"] is None)
        effective_ts = ts if "ts" not in quote or tick_ts_missing else quote["ts"]
        self._quotes[code] = {
            "last": float(quote.get("last") or 0.0),
            "bid1": float(quote.get("bid1") or 0.0),
            # ask1 也要缓存: 尾盘自动买入按卖一价定价 (2026-07-27 MVP),
            # 丢了它买单会全走对手最优分支
            "ask1": float(quote.get("ask1") or 0.0),
            "high": high,
            "ts": effective_ts,
            "tick_ts_missing": tick_ts_missing,
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
                self._store.write_audit(
                    "monitor_no_quote", f"{code} 无行情快照, 本轮跳过",
                    {"code": code})
                continue
            # 审计M6修复: 陈旧快照与无快照同等 fail-closed —— 单票订阅
            # 丢失时全局心跳兜不住, 陈旧价驱动卖出比不卖更危险。
            # 2026-07-27 裁决③: tick 显式缺时间戳 (ts=None) 也视为陈旧
            if quote.get("tick_ts_missing"):
                self._store.write_audit(
                    "monitor_stale_quote",
                    f"{code} tick 缺时间戳, 视为陈旧, 本轮跳过",
                    {"code": code})
                continue
            if (self._clock() - quote["ts"]) > self._cfg.quote_stale_sec:
                self._store.write_audit(
                    "monitor_stale_quote",
                    f"{code} 行情快照陈旧 (>{self._cfg.quote_stale_sec}s), 本轮跳过",
                    {"code": code, "quote_ts": quote["ts"]})
                continue
            reason = self._evaluate(code, pos.avg_cost, quote)
            if reason is None:
                continue
            # 审计H1修复: 执行成功才标记"已触发"。execute_exit 有大量
            # 合法 fail-closed 返回路径 (无买一价/可用为0/风控拒绝),
            # 先标记等于"一跳行情延迟换一整天无保护"
            if self._executor.execute_exit(code, reason):
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

    def _evaluate(self, code: str, avg_cost: float, quote: dict) -> str | None:
        """全规则评估, 复刻回测 ExitDispatcher 优先级语义
        [backtest/loop/exit_engine.py:46-53/99-139]。
        每条规则对齐回测单 bar 版本 (出处逐条标注), 数据缺失维持
        fail-closed。返回触发原因或 None。

        与回测的已知口径差 (parity 测试头注同款):
        - bar low/close ≡ tick last; hi_pp 用当日行情 high;
        - trailing_first 的双触发 (ladder 部分卖 + trailing 全卖剩余)
          在实盘由 executor 预埋单承担部分卖 —— 本腿只对"未预埋档"
          兜底, 首触发即返回, 无双触发路径。
        """
        stop = self._cfg.stop
        # 2026-07-27 ETF 误卖事件裁决③: ETF 不纳入自动管理,
        # 规则评估直接跳过 (持仓仍照常对账, 那是 reconciler 的事)
        if self._cfg.exclude_etf and is_etf(code):
            return None
        last = quote["last"]
        high = quote["high"]
        days = self._hold_days(code)
        # 峰值 = max(成本, 当日最高)。历史峰值 MVP 用成本价兜底 ——
        # TODO P2 接 K 线缓存取历史日高
        peak = max(avg_cost, high)
        today = time.strftime("%Y%m%d", time.localtime(self._clock()))

        def hit_cost_stop():
            # [cost_stop.py:28] lo_pp ≤ threshold ≡ low ≤ ep×(1+threshold),
            # threshold 负值口径 (回测 config 同)
            c = stop.cost_stop
            return (c.enabled and avg_cost > 0
                    and last <= avg_cost * (1.0 + c.threshold))

        def hit_trailing():
            # [trailing.py:35-39] 先过 activation 激活线 (峰值涨幅),
            # 再判现价跌破 峰值×(1-drawdown) —— 2026-07-26 裁决前
            # 实盘缺激活线, 现已补齐复刻
            t = stop.trailing_stop
            if not t.enabled or avg_cost <= 0:
                return False
            if (peak - avg_cost) / avg_cost < t.activation:
                return False
            return last <= peak * (1.0 - t.drawdown)

        def hit_ladder():
            # [ladder_tp.py:37-53] High 涨破新档位即触发。实盘的档位
            # 执行在券商端 (executor 预埋限价单), 本腿只对"未预埋档"
            # 兜底 —— 已标记档券商自己会成交, 再触发就是双卖
            lv = stop.ladder_tp
            if not lv.enabled or avg_cost <= 0:
                return None
            done = self._book.tier_done(code, today)
            for i, (profit, _ratio) in enumerate(lv.levels):
                if i in done:
                    continue
                if high >= avg_cost * (1.0 + profit):
                    return i
            return None

        def hit_time_stop():
            # [time_stop.py:23] 到点即走 (回测无收益门槛;
            # 旧 trade 私设的 min_gain 已随裁决①删除)
            t = stop.time_stop
            return t.enabled and days >= t.max_hold_days

        def hit_cond_time():
            # [cond_time.py:25] 持仓 ≥ days 且当日最高涨幅 ≥ profit
            c = stop.cond_time_stop
            return (c.enabled and avg_cost > 0 and days >= c.days
                    and (high - avg_cost) / avg_cost >= c.profit)

        def hit_first_day():
            # [first_day.py:28-40] 首个可交易日 (T+1 即 hold_days==1)
            # 日内最高涨幅 < target 即卖。回测在当日最后一根 bar 判定,
            # 实盘无 bar 收盘概念取"当日"粒度 (1d bpday=1 口径相同)
            f = stop.first_day
            return (f.enabled and avg_cost > 0 and days == 1
                    and (high - avg_cost) / avg_cost < f.target)

        # 优先级调度顺序 [exit_engine.py:46-53]: priority block 在前,
        # 公共尾部 time_stop → cond_time → first_day 恒在后
        _ORDER = {
            "stop_first": ("cost_stop", "ladder_tp", "trailing"),
            "ladder_tp_first": ("ladder_tp", "cost_stop", "trailing"),
            "trailing_first": ("ladder_tp", "trailing", "cost_stop"),
        }
        checks = {
            "cost_stop": (hit_cost_stop, f"cost_stop: 现价 {last} ≤ "
                          f"成本×(1{stop.cost_stop.threshold:+.0%})"),
            "trailing": (hit_trailing, f"trailing: 现价 {last} 跌破峰值 "
                         f"{peak}×(1-{stop.trailing_stop.drawdown:.0%})"),
            "time_stop": (hit_time_stop, f"time_stop: 持有 {days} 天 ≥ "
                          f"{stop.time_stop.max_hold_days} 天"),
            "cond_time": (hit_cond_time, f"cond_time: 持有 {days} 天且"
                          f" 涨幅达 {stop.cond_time_stop.profit:.0%}"),
            "first_day": (hit_first_day,
                          f"first_day: 首日最高涨幅未达 {stop.first_day.target:.0%}"),
        }
        for name in _ORDER[stop.priority] + ("time_stop", "cond_time",
                                             "first_day"):
            if name == "ladder_tp":
                tier = hit_ladder()
                if tier is not None:
                    return (f"ladder_tp: 最高 {high} 涨破档 {tier} "
                            f"({stop.ladder_tp.levels[tier][0]:.0%}, 未预埋兜底)")
                continue
            hit, reason = checks[name]
            if hit():
                return reason
        return None
