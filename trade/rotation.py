"""trade/rotation.py — ETF 轮动 + 双池资金分配 (2026-08-14; 2026-08-20 动量改造)。

设计意图 (照 trade/auto_buy.py 骨架):
    在选股系统之外新增一个 ETF 轮动系统, 两个系统同时跑, 资金按
    etf_ratio 分两半, 靠"预算帽"软隔离。信号计算在工作线程 (拉
    两只风险腿日线收盘价, 阻塞可达数秒), 调仓执行在消费者线程 (唯一写者)。

规则 (用户手册, 唯一真相; 2026-08-20 由 MA20 三态改造为动量):
    - 两只风险腿 (cyb_etf + risk_etf2) 各算 4 周动量 m = 末收盘/N日前收盘 − 1,
      择动量最高 且 > 0 的腿满仓; 两腿都 ≤ 0 → 满仓避险篮子 (默认单黄金,
      "空仓买黄金")。
    - 日频移动止损: 持仓风险腿期间, 每日更新「持仓期最高收盘 H」, 当日收盘
      < H×(1−trailing_stop_pct) → 当日切避险篮子 (H 换腿时重置)。
    - 周频信号: 每周最后一个交易日 (signal_day 锚定, 节假日前移) 重算动量并
      存 pending_target, 次日 (下一交易日) 14:56 执行。
    "满仓" = ETF 池的 100% = etf_ratio × 总资产 (不是整个账户)。

调仓 (模型 B: 预算帽 + 自然回笼 + 双向, 用户 2026-08-14 拍板):
    每日算目标市值 = 目标腿 100% 池 或 避险篮子 100% 池, 与当前持仓对比:
    - 目标腿变了 → 换档 (先卖超出的 ETF, 回款后买目标 ETF)
    - 目标没变 → 只补仓 (低配的 ETF 用自由现金买, 绝不主动卖来凑比例)
    当前持仓从 QMT 真实持仓派生 (自愈: 换档半途失败, 次日按市值差额自然补齐,
    不依赖"持久化状态"与真实持仓同步)。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from trade.book import (
    DIRECTION_BUY,
    DIRECTION_SELL,
    OS_REPORTED,
    PRICE_TYPE_LIMIT,
    TERMINAL_STATUSES,
)
from scheduler.trading_calendar import next_trading_day
from trade.events import EVENT_ROTATION, Event
from trade.monitor import is_trading_day_cached, trading_session
from trade.quote_stale import is_quote_stale
from trade.risk import OrderIntent
from utils.logger import get_logger

_logger = get_logger("trade.rotation")

# ══════════════════════════════════════════════════════════════════
# 旧 MA20 三态 (2026-08-20 动量改造后已弃用, 生产路径不再调用)。
# 保留仅为 research/*.py 历史回测脚本仍 import 它们 (改名必改引用,
# 一次性清理这些脚本属独立任务)。compute_signal / STATE_RATIOS /
# target_values / _derive_state / 三态常量 全部标 deprecated。
# ══════════════════════════════════════════════════════════════════
STATE_FULL_CYB = "full_cyb"    # 满仓创业板50ETF (deprecated)
STATE_HALF = "half"            # 半仓 (创业板 + 避险腿各半) (deprecated)
STATE_FULL_GOLD = "full_gold"  # 满仓避险腿 (deprecated)

# 三态 → (主腿比例, 避险腿总比例)。避险腿内部再按 hedge_ratio 拆两只 (见 target_values)。
STATE_RATIOS = {
    STATE_FULL_CYB: (1.0, 0.0),
    STATE_HALF: (0.5, 0.5),
    STATE_FULL_GOLD: (0.0, 1.0),
}

# 周频信号日锚定 weekday (config.signal_day → datetime.weekday() 0=周一)。
_SIGNAL_DAY_WEEKDAY = {"monday": 0, "tuesday": 1, "wednesday": 2,
                       "thursday": 3, "friday": 4}


def _is_signal_day(d, signal_day: str) -> bool:
    """d 是否周频信号日 (计划书自审 §八.3): 信号日 = 每周「最后一个交易日
    ≤ 配置锚定 weekday」, 节假日休市时前移到最后一个能交易的 weekday。
    执行日 = 信号日的下一交易日 (next_trading_day 自然跳过节假日)。"""
    if not is_trading_day_cached(d):
        return False
    anchor = _SIGNAL_DAY_WEEKDAY.get(signal_day, 4)
    if d.weekday() > anchor:          # 已过锚定日 (如 signal_day=monday 而今天周二)
        return False
    nxt = next_trading_day(d)
    # d 之后到锚定日之间若还有交易日, 则 d 不是最后一个 ≤ 锚定日的交易日
    if nxt.isocalendar()[:2] == d.isocalendar()[:2] and nxt.weekday() <= anchor:
        return False
    return True


_LOT = 100  # ETF 一手 = 100 份


def round_price_etf(x: float) -> float:
    """ETF 场内基金最小报价单位 0.001 元, 用千分位四舍五入。
    不能复用 executor.round_price(股票 0.01 档): 2.004 → 2.00 会挂在不成交价
    (审计 HIGH#1)。"""
    return int(x * 1000 + 0.5) / 1000


def compute_signal(closes: list[float], ma_window: int = 20,
                   high_window: int = 250,
                   drawdown_threshold: float = 0.20) -> dict:
    """(deprecated, 2026-08-20) 旧 MA20 三态信号纯函数。生产已切动量规则
    compute_momentum_signal, 此函数仅 research/*.py 历史回测脚本仍调用。

    返回 state (三态之一) 或 None (数据不足, fail-closed 不动作);
    其余字段为明细 (ma20 方向/250日高点/回撤率/最新收盘), 供 UI 展示。
    """
    need = max(high_window, ma_window + 1)
    if not closes or len(closes) < need:
        return {
            "state": None,
            "reason": f"数据不足: 需要 {need} 根, 实际 {len(closes)} 根",
            "ma20_today": None, "ma20_yesterday": None,
            "ma20_direction": None, "high_250": None,
            "drawdown": None, "close": None,
        }
    ma20_today = sum(closes[-ma_window:]) / ma_window
    ma20_yesterday = sum(closes[-ma_window - 1:-1]) / ma_window
    # 审计 L2: 手册只定义今>昨/今<昨, 未定义今==昨; 用严格 > , 相等归 down
    # (→ 满仓黄金, 风险回避方向, 保守 fail-safe)。浮点均值精确相等属测度零。
    direction = "up" if ma20_today > ma20_yesterday else "down"
    high_250 = max(closes[-high_window:])
    close = closes[-1]
    drawdown = (close / high_250 - 1.0) if high_250 > 0 else 0.0
    if direction == "up":
        state = STATE_FULL_CYB if drawdown >= -drawdown_threshold else STATE_HALF
    else:
        state = STATE_FULL_GOLD
    return {
        "state": state,
        "ma20_today": round(ma20_today, 4),
        "ma20_yesterday": round(ma20_yesterday, 4),
        "ma20_direction": direction,
        "high_250": round(high_250, 4),
        "drawdown": round(drawdown, 4),
        "close": round(close, 4),
    }


def compute_momentum_signal(closes_by_leg: dict, momentum_window: int = 20) -> dict:
    """纯函数: {腿代码: 收盘序列(交易日升序)} → 动量择腿信号 dict (2026-08-20 动量改造)。

    逐腿算动量 m = 末收盘 / momentum_window 日前收盘 − 1，取动量最高 且 > 0 的腿为目标；
    两腿都 ≤ 0 → target=None (切避险篮子, "空仓买黄金")。
    数据不足的腿 (closes 长度 ≤ momentum_window 或 N日前收盘 ≤ 0) 不参与择腿;
    两腿都不足 → target=None + reason (fail-safe 切避险, 同 compute_signal 口径)。
    """
    momentum: dict = {}
    for code, closes in closes_by_leg.items():
        if (closes and len(closes) > momentum_window
                and closes[-1 - momentum_window] > 0):
            momentum[code] = round(closes[-1] / closes[-1 - momentum_window] - 1.0, 4)
        else:
            momentum[code] = None
    valid = {c: m for c, m in momentum.items() if m is not None}
    if not valid:
        return {"target": None, "insufficient": True,
                "reason": f"数据不足: 无腿可算动量 (需要 > {momentum_window} 根)",
                "momentum": momentum}
    best = max(valid, key=lambda c: valid[c])
    target = best if valid[best] > 0 else None
    return {"target": target, "insufficient": False,
            "momentum": momentum,
            "reason": "" if target else "两腿动量均 ≤ 0"}


def momentum_target_values(cyb_etf: str, risk_etf2: str, gold_etf: str,
                           hedge_etf2: str, hedge_ratio: float,
                           target_code: str | None,
                           pool: float) -> list[tuple[str, float]]:
    """动量择腿 → [(代码, 目标市值)] (风险腿在前, 避险腿在后)。

    target_code 非空 = 持有该风险腿 (100% 池); target_code=None = 避险篮子
    (黄金 = hedge_ratio × 池, 避险ETF2 = (1-hedge_ratio) × 池; hedge_etf2 空则单黄金)。
    risk_etf2 空 = 单风险腿退化。"""
    risk_legs = [cyb_etf] + ([risk_etf2] if risk_etf2 else [])
    legs: list[tuple[str, float]] = []
    if target_code:
        for c in risk_legs:
            legs.append((c, pool if c == target_code else 0.0))
        legs.append((gold_etf, 0.0))
        if hedge_etf2:
            legs.append((hedge_etf2, 0.0))
    else:
        for c in risk_legs:
            legs.append((c, 0.0))
        ratio = hedge_ratio if hedge_etf2 else 1.0
        legs.append((gold_etf, ratio * pool))
        if hedge_etf2:
            legs.append((hedge_etf2, (1.0 - ratio) * pool))
    return legs


def _derive_state(v_cyb: float, v_hedge: float) -> str | None:
    """从主腿市值 + 避险腿总市值派生当前状态 (自愈: 换档半途失败次日自然补齐)。
    None = 主腿与避险腿都空仓 (首日未建仓)。阈值 90%/10% 容差防碎仓误判。"""
    total = v_cyb + v_hedge
    if total <= 0:
        return None
    ratio = v_cyb / total
    if ratio >= 0.9:
        return STATE_FULL_CYB
    if ratio <= 0.1:
        return STATE_FULL_GOLD
    return STATE_HALF


def target_values(cyb_etf: str, gold_etf: str, hedge_etf2: str,
                  hedge_ratio: float, target_state: str,
                  pool: float) -> list[tuple[str, float]]:
    """三态 + 避险两腿配置 → [(代码, 目标市值)] (主腿在前, 避险腿在后)。

    避险腿总权重 = STATE_RATIOS[state] 第二项; 再按 hedge_ratio 拆两只:
      黄金 = hedge_ratio × 避险总,  避险ETF2 = (1-hedge_ratio) × 避险总。
    hedge_etf2 为空时 hedge_ratio 视为 1.0 (单避险, 与旧口径逐字节一致)。
    """
    cyb_w, hedge_w = STATE_RATIOS[target_state]
    ratio = hedge_ratio if hedge_etf2 else 1.0
    hedge_pool = hedge_w * pool
    legs = [(cyb_etf, cyb_w * pool), (gold_etf, ratio * hedge_pool)]
    if hedge_etf2:
        legs.append((hedge_etf2, (1.0 - ratio) * hedge_pool))
    return legs


class RotationFeature:
    """ETF 轮动特性。公开接口 (照 AutoBuyFeature): start / on_signals /
    last / running。依赖全注入, cfg 传 getter (热更穿透)。"""

    def __init__(self, engine, cfg_getter, store, gateway, book, monitor,
                 risk, executor, *, build_risk_ctx, clock=time.time):
        self._engine = engine
        self._cfg_getter = cfg_getter
        self._store = store
        self._gateway = gateway
        self._book = book
        self._monitor = monitor
        self._risk = risk
        self._executor = executor
        self._build_ctx = build_risk_ctx
        self._clock = clock
        self._running = False
        self._last: dict | None = None
        # 换档卖单等成交参数 (实例属性, 测试直注小值; 生产 3s —— 流动性好的
        # ETF 限价@买一秒级成交, 3s 覆盖正常延迟; 审计 M3 缩短原 10s 的
        # 唯一写者阻塞, 等不到也放行由次日自愈)
        self._wait_timeout = 3.0
        self._wait_interval = 0.1
        # 当日已挂出的在途卖单 {code: {order_id, qty}} —— 对账 in_flight 降级网用
        # (审计 M4: 轮动卖单绕过 executor._pending, 回调丢失时对账误判 CRITICAL)
        self._pending_sells: dict[str, dict] = {}
        # 日频移动止损基准: 当前持仓风险腿的「持仓期最高收盘」{代码: 最高价}
        # (2026-08-20 动量改造; 随 signal 一起落 rotation_state, 重启不丢)
        self._entry_high: dict[str, float] = {}
        # 周频信号持久化的「待执行目标」: signal_day 算好存这里, 次日执行
        # (2026-08-20 动量改造; 随 signal 落 rotation_state, 重启不丢)
        self._pending_target: str | None = None
        # 是否已算出过周频信号 (区分 pending_target=None 是"避险"还是"没算过")
        self._has_target = False
        # 冷启动恢复最近一次信号 (UI 展示 + 恢复 entry_high/pending_target)
        try:
            self._last = store.rotation_signal.load()
            if isinstance(self._last, dict):
                sig = self._last.get("signal") or {}
                self._entry_high = dict(sig.get("entry_high") or {})
                self._pending_target = sig.get("pending_target")
                self._has_target = "pending_target" in sig
        except Exception:
            self._last = None

    # ── 公开接口 ────────────────────────────────────────────────

    @property
    def last(self) -> dict | None:
        """最近一次信号+调仓结果 (页面展示, 只读)。"""
        return self._last

    @property
    def running(self) -> bool:
        """信号工作线程是否在跑 (防重入, 只读)。"""
        return self._running

    def in_flight_sells(self) -> dict[str, int]:
        """当日已挂出的在途卖单 {code: qty} (对账 in_flight 降级网用, 审计 M4)。"""
        return {code: int(p["qty"]) for code, p in self._pending_sells.items()}

    def start(self, source: str) -> None:
        """发起一次「算信号 + 调仓」(消费者线程内只开线程)。
        enabled 是总开关: 关闭时 scheduled 与 manual 都不运行 (审计 L3, 2026-08-15)。"""
        cfg = self._cfg_getter().rotation
        if not cfg.enabled:
            return
        if source == "scheduled" and not is_trading_day_cached(
                datetime.fromtimestamp(self._clock()).date()):
            return
        if self._running:
            self._store.write_audit(
                "rotation_skip", "上一次信号计算仍在运行, 本次忽略",
                {"source": source})
            return
        self._running = True
        try:
            threading.Thread(target=self._worker, args=(source,),
                             name="rotation-signal", daemon=True).start()
        except Exception as e:
            # 审计 L8: 线程创建失败时复位 _running, 否则后续触发恒被"上一次
            # 仍在运行"拦下、轮动永久静默停摆 (极罕见, 但活性要保证)
            self._running = False
            self._store.write_audit("rotation_error", f"信号线程创建失败: {e}",
                                    {"source": source})
            return
        self._store.write_audit(
            "rotation_start", f"ETF 轮动已发起 ({source})", {"source": source})

    def on_signals(self, data: dict) -> None:
        """信号结果处理 (消费者线程)。四种数据形态:
          - signal=None         → 错误 (记 audit)
          - signal_only=True    → 周频信号日/跳过: 只存 pending_target, 不执行
          - insufficient        → 数据不足 (fail-safe, 不动作)
          - 正常                → 执行调仓 (_execute, 含日频移动止损)
        """
        self._running = False
        source = data.get("source")
        signal = data.get("signal")
        signal_only = bool(data.get("signal_only"))
        try:
            if signal is None:
                self._last = {"ts": self._clock(), "source": source,
                              "error": data.get("error", "未知错误")}
                self._store.write_audit(
                    "rotation_error", f"轮动信号失败: {data.get('error')}",
                    {"source": source})
                return
            self._last = {"ts": self._clock(), "source": source, "signal": signal}
            if signal_only:
                # 周频信号日: 算好 target 存 pending_target, 次日才执行
                if "pending_target" in signal:
                    self._pending_target = signal.get("pending_target")
                    self._has_target = True
                    self._store.write_audit(
                        "rotation_signal",
                        f"周频信号已算, 待次日执行: {self._pending_target or '避险'}",
                        dict(signal))
                else:
                    self._store.write_audit(
                        "rotation_skip", "跳过 (非交易日或首信号未算)", dict(signal))
                return
            if signal.get("insufficient"):
                self._store.write_audit(
                    "rotation_skip", f"轮动信号数据不足: {signal.get('reason')}",
                    dict(signal))
                return
            self._execute(signal)
        finally:
            # 审计 L10: 失败/异常路径也落库, 重启后 last 不回退到旧成功信号
            try:
                self._store.rotation_signal.save(dict(self._last))
            except Exception:
                _logger.debug("轮动信号落库异常 (不影响交易)")

    # ── 内部 ────────────────────────────────────────────────────

    def _worker(self, source: str) -> None:
        """工作线程: 周频信号日算动量存 pending_target; 非信号日取 pending_target
        执行 (含日频移动止损)。只 put 事件, 不碰交易写者。
        取数口径 (2026-08-16 拍板): 收盘后 (≥15:05) 用今日完整日线; 盘中 (尾盘
        14:56) 用实时价当今日收盘价; 无实时价/非交易日回退昨日 —— 无未来函数。"""
        try:
            now = datetime.fromtimestamp(self._clock())
            today = now.date()
            if not is_trading_day_cached(today):
                self._engine.put(Event(
                    type=EVENT_ROTATION, ts=self._clock(),
                    data={"signal": {"note": "非交易日, 不动作"},
                          "source": source, "signal_only": True}))
                return
            cfg = self._cfg_getter().rotation
            if _is_signal_day(today, cfg.signal_day):
                # 周频信号日: 算动量 → 存 pending_target (次日执行)
                signal = self._compute_momentum(now)
                signal["pending_target"] = signal.get("target")
                self._engine.put(Event(
                    type=EVENT_ROTATION, ts=self._clock(),
                    data={"signal": signal, "source": source, "signal_only": True}))
            elif not self._has_target:
                # 冷启动后首个周频信号还没算: 不动作, 等信号日 (避免先买黄金再换腿)
                self._engine.put(Event(
                    type=EVENT_ROTATION, ts=self._clock(),
                    data={"signal": {"note": "首个周频信号未算, 等信号日"},
                          "source": source, "signal_only": True}))
            else:
                # 非信号日: 用持久化 pending_target 执行 (含日频移动止损)
                signal = {"target": self._pending_target, "insufficient": False,
                          "date": now.strftime("%Y%m%d"),
                          "note": "日频执行 (持久化 pending_target + 移动止损)"}
                self._engine.put(Event(
                    type=EVENT_ROTATION, ts=self._clock(),
                    data={"signal": signal, "source": source}))
        except Exception as e:
            _logger.exception("ETF 轮动信号工作线程异常")
            self._engine.put(Event(type=EVENT_ROTATION, ts=self._clock(),
                                   data={"signal": None, "error": str(e),
                                         "source": source}))

    def _compute_momentum(self, now) -> dict:
        """工作线程: 拉两只风险腿收盘 → 算动量信号 (纯取数+计算, 不执行)。"""
        cfg = self._cfg_getter().rotation
        after_close = now.hour > 15 or (now.hour == 15 and now.minute >= 5)
        need = cfg.momentum_window + 30          # 动量窗口 + 余量
        min_bars = cfg.momentum_window + 1
        risk_legs = [cfg.cyb_etf] + ([cfg.risk_etf2] if cfg.risk_etf2 else [])
        closes_by_leg: dict = {}
        data_sources: list[str] = []
        for code in risk_legs:
            closes, src = self._fetch_closes(code, need, min_bars)
            if after_close:
                closes_by_leg[code] = closes
            else:
                # 盘中: 实时价当今日收盘
                today_price = None
                try:
                    q = (self._gateway.query_quotes([code]) or {}).get(code) or {}
                    today_price = q.get("last") or 0.0
                except Exception:
                    today_price = None
                if (today_price and float(today_price) > 0
                        and is_trading_day_cached(now.date())):
                    closes_by_leg[code] = list(closes) + [float(today_price)]
                else:
                    closes_by_leg[code] = closes
            data_sources.append(src)
        signal = compute_momentum_signal(closes_by_leg, cfg.momentum_window)
        signal["date"] = now.strftime("%Y%m%d")
        signal["risk_legs"] = risk_legs
        signal["data_source"] = "/".join(sorted(set(data_sources)))
        # 注 (审计 L6): 本线程读 cfg 算信号, 消费者线程 _execute 会再读 cfg
        # 取 etf_ratio/代码。若两步之间热改配置, 信号按旧窗口算、调仓按新
        # 配置执行; 窗口极小(秒级)且次日按持仓派生自愈, 接受此边界。
        return signal

    # ── 指数日线取数降级链 (2026-08-17: QMT → TDX → 腾讯; 东财限连已剔除) ──

    def _fetch_closes(self, code: str, count: int, min_bars: int) -> tuple[list[float], str]:
        """取指数日线收盘价, 三级降级: QMT(主) → TDX → 腾讯。
        每级判空(根数 ≥ min_bars)才算成功, 否则降级下一级; 全挂返回 ([], "none")
        → compute_momentum_signal 判数据不足 → fail-closed 不动作。返回 (closes, 来源名)。"""
        # 1) QMT 主源
        try:
            closes = self._gateway.query_daily_closes(code, count=count)
            if closes and len(closes) >= min_bars:
                return [float(c) for c in closes], "QMT"
            _logger.warning("轮动信号 QMT 取数不足(%s 根), 降级 TDX", len(closes or []))
        except Exception as e:
            _logger.warning("轮动信号 QMT 取数失败, 降级 TDX: %s", e)
        # 2) TDX (core/data_fetcher)
        try:
            closes = self._fetch_tdx_closes(code, count)
            if closes and len(closes) >= min_bars:
                return closes, "TDX"
            _logger.warning("轮动信号 TDX 取数不足(%s 根), 降级腾讯", len(closes or []))
        except Exception as e:
            _logger.warning("轮动信号 TDX 取数失败, 降级腾讯: %s", e)
        # 3) 腾讯 (akshare)
        try:
            closes = self._fetch_tencent_closes(code, count)
            if closes and len(closes) >= min_bars:
                return closes, "腾讯"
            _logger.warning("轮动信号腾讯取数不足(%s 根)", len(closes or []))
        except Exception as e:
            _logger.warning("轮动信号腾讯取数失败: %s", e)
        return [], "none"

    def _fetch_tdx_closes(self, code: str, count: int) -> list[float]:
        """TDX (core/data_fetcher) 取指数日线收盘价, 返回最近 count 根。"""
        import datetime as _dt
        from core.data_fetcher import DataFetcher
        # count 根交易日 ≈ 1.5×count 自然日 (含周末/节假日), 再留 60 天余量
        end = _dt.date.today().strftime("%Y%m%d")
        start = (_dt.date.today() - _dt.timedelta(days=count * 2 + 60)).strftime("%Y%m%d")
        kl = DataFetcher.get_kline([code], start, end, period="1d",
                                   dividend_type="front", use_cache=True)
        s = (kl or {}).get("Close")
        if s is None or code not in s.columns:
            return []
        s = s[code].dropna()
        if s.empty:
            return []
        return [float(x) for x in s.tolist()[-count:]]

    def _fetch_tencent_closes(self, code: str, count: int) -> list[float]:
        """腾讯 (akshare stock_zh_index_daily_tx) 取指数日线收盘价, 返回最近 count 根。"""
        import akshare as ak
        num, ex = code.split(".")
        sym = f"{ex.lower()}{num}"   # 399673.SZ → sz399673
        df = ak.stock_zh_index_daily_tx(symbol=sym)
        if df is None or df.empty or "close" not in df.columns:
            return []
        return [float(x) for x in df["close"].tolist()[-count:]]

    def _execute(self, signal: dict) -> None:
        """调仓执行 (消费者线程)。fail-closed: 任何一步拿不到数据就不动作。"""
        cfg = self._cfg_getter().rotation
        if trading_session(self._clock()) != "continuous":
            self._store.write_audit("rotation_skip", "非连续竞价时段, 跳过调仓", {})
            return
        target_code = signal.get("target")   # 风险腿代码, 或 None (避险篮子)
        try:
            asset = self._gateway.query_asset()
            total = float(asset.get("total_asset", 0.0) or 0.0)
        except Exception as e:
            self._store.write_audit("rotation_error", f"查资产失败, 跳过调仓: {e}", {})
            return
        if total <= 0:
            self._store.write_audit("rotation_error", "总资产为 0, 跳过调仓", {})
            return
        pool = cfg.etf_ratio * total

        def _mk_legs(tcode):
            return momentum_target_values(cfg.cyb_etf, cfg.risk_etf2, cfg.gold_etf,
                                          cfg.hedge_etf2, cfg.hedge_ratio, tcode, pool)

        legs = _mk_legs(target_code)
        codes = [c for c, _ in legs]
        risk_legs = [cfg.cyb_etf] + ([cfg.risk_etf2] if cfg.risk_etf2 else [])

        # 用 QMT 真实持仓 + can_use 回填 (2026-08-19 修复: 昨尾盘买入 can_use 陈旧)
        qmt_pos0 = {p["code"]: p for p in self._gateway.query_positions()}
        for c in codes:
            p = qmt_pos0.get(c)
            if p is not None:
                self._book.set_can_use(c, int(p.get("can_use", 0) or 0))

        def _vol(pos, code):
            return int((pos.get(code) or {}).get("volume", 0) or 0)

        def _can(pos, code):
            return int((pos.get(code) or {}).get("can_use", 0) or 0)

        cur = {c: self._etf_value(c, _vol(qmt_pos0, c)) for c in codes}
        quotes = self._fetch_quotes(codes)

        # 日频移动止损: 当前唯一持仓风险腿从「持仓期最高收盘」回撤超阈值 → 切避险
        held = [c for c in risk_legs if _vol(qmt_pos0, c) > 0]
        stop_off = False
        if len(held) == 1:
            h = held[0]
            q = quotes.get(h)
            last = q.get("last") if q else None
            if last and last > 0:
                self._entry_high[h] = max(self._entry_high.get(h, last), last)
                if last < self._entry_high[h] * (1.0 - cfg.trailing_stop_pct):
                    stop_off = True
        if stop_off:
            target_code = None
            legs = _mk_legs(None)
            self._pending_target = None   # §八.4: 止损触发清空 pending_target, 后续守避险
            self._store.write_audit(
                "rotation_stop", "日频移动止损触发, 切避险篮子",
                {"entry_high": dict(self._entry_high)})

        # 卖超目标腿
        self._pending_sells.clear()
        sell_ids: list[str] = []
        for code, tval in legs:
            oid = self._sell_to(code, cur[code], tval, quotes.get(code),
                                _can(qmt_pos0, code))
            if oid:
                sell_ids.append(oid)
        self._wait_fills(sell_ids)

        # 卖后重读 QMT 持仓, 算真实 ETF 池值; 池级预算帽防超买 (审计 CRITICAL#2)
        qmt_pos = {p["code"]: p for p in self._gateway.query_positions()}
        cur2 = {c: self._etf_value(c, _vol(qmt_pos, c)) for c in codes}
        try:
            cash = float(self._gateway.query_asset().get("cash", 0.0) or 0.0)
        except Exception:
            cash = 0.0
        pool_gap = max(0.0, pool - sum(cur2.values()))
        cash = min(cash, pool_gap)
        for code, tval in legs:
            cash = self._buy_to(code, max(0.0, tval - cur2[code]), cash,
                                quotes.get(code))

        # 收尾: 移动止损基准对齐当前目标 (切腿清旧、同腿保最高), 随 signal 落库
        if target_code in risk_legs:
            q = quotes.get(target_code)
            last = q.get("last") if q else None
            self._entry_high = {target_code:
                                max(self._entry_high.get(target_code, 0.0),
                                    last if last and last > 0 else 0.0)}
        else:
            self._entry_high = {}
        signal["entry_high"] = dict(self._entry_high)
        signal["pending_target"] = self._pending_target
        self._store.write_audit(
            "rotation_summary",
            f"ETF 轮动: 目标 {target_code or '避险'} (池 {pool:.0f}), "
            f"现值 {'/'.join(f'{cur[c]:.0f}' for c in codes)}",
            {"target": target_code, "pool": round(pool, 2),
             "values": {c: round(cur[c], 2) for c in codes}, "signal": signal})

    def _etf_value(self, code: str, volume: int) -> float:
        """持仓市值 = 股数 × 最新价; 无行情回退成本价 (与对账同口径)。"""
        if volume <= 0:
            return 0.0
        q = self._monitor.quote_of(code)
        last = q.get("last") if q else None
        if last and last > 0:
            return float(last) * volume
        pos = self._book.snapshot()["positions"].get(code)
        cost = getattr(pos, "avg_cost", 0.0) if pos else 0.0
        return cost * volume if cost > 0 else 0.0

    def _fetch_quotes(self, codes: list[str]) -> dict:
        """两只 ETF 的行情: 订阅 + 缓存兜底 + 轮询补查 (单条腿一个口径)。
        陈旧/无戳的行情视为无价 (fail-closed, 审计 M2: 与 monitor/executor
        同口径 —— 宁可不卖, 不可按陈旧价下单)。"""
        try:
            self._gateway.subscribe_quotes(codes)
        except Exception:
            pass
        quotes: dict = {}
        for c in codes:
            q = self._monitor.quote_of(c)
            if q and q.get("last") and not self._quote_stale(q):
                quotes[c] = q
        missing = [c for c in codes if c not in quotes]
        if missing:
            try:
                for c, q in (self._gateway.query_quotes(missing) or {}).items():
                    if not self._quote_stale(q):
                        quotes[c] = q
            except Exception:
                _logger.warning("轮动行情补查失败 (维持缓存): %s", missing)
        return quotes

    def _quote_stale(self, q: dict | None) -> bool:
        """行情是否陈旧。判定统一走 trade/quote_stale.py (单一真相源):
        无 ts / ts 为 None / tick_ts_missing / 超时 任一即陈旧 (fail-closed)。"""
        return is_quote_stale(q, self._clock(),
                              self._cfg_getter().quote_stale_sec)[0]

    @staticmethod
    def _side_price(quote: dict | None, sell: bool) -> float | None:
        """下单侧价格: 卖锚定买一(bid1), 买锚定卖一(ask1); 缺失回退最新价。"""
        if not quote:
            return None
        key = "bid1" if sell else "ask1"
        p = quote.get(key)
        if p and float(p) > 0:
            return float(p)
        last = quote.get("last")
        return float(last) if last and float(last) > 0 else None

    def _sell_to(self, code: str, cur_val: float, target_val: float,
                 quote: dict | None, can_use: int) -> str | None:
        """卖到目标市值 (仅换档时调用)。全清 (target≤0) 允许卖残余非整手。
        返回 order_id (None=本轮未卖)。"""
        if cur_val <= target_val or can_use <= 0:
            return None
        price = self._side_price(quote, sell=True)
        if not price:
            self._store.write_audit(
                "rotation_fail_closed", f"{code} 无买一价, 本轮不卖", {"code": code})
            return None
        if target_val <= 0:
            qty = can_use  # 全清, 允许残余碎股
        else:
            excess = cur_val - target_val
            qty = int(excess / price / _LOT) * _LOT
            qty = min(qty, can_use)
        if qty <= 0:
            return None
        return self._place_order(code, DIRECTION_SELL, price, qty, "ETF轮动卖出")

    def _buy_to(self, code: str, gap: float, cash: float,
                quote: dict | None) -> float:
        """按缺口买入 (gap=目标市值−现值), 花掉的部分从 cash 扣掉并返回剩余。"""
        if gap <= 0 or cash <= 0:
            return cash
        price = self._side_price(quote, sell=False)
        if not price:
            self._store.write_audit(
                "rotation_fail_closed", f"{code} 无卖一价, 本轮不买", {"code": code})
            return cash
        budget = min(gap, cash)
        # 注 (审计 L9): int() 向下取整到 100 份, 每单留 <1 手现金缓冲覆盖
        # ETF 免印花税的微小佣金, 不会超支; 未显式预留费用/滑点, 若券商有
        # 最低佣金需后续核对。
        qty = int(budget / price / _LOT) * _LOT
        if qty < _LOT:
            return cash
        if self._place_order(code, DIRECTION_BUY, price, qty, "ETF轮动买入"):
            return cash - round(price * qty, 2)
        return cash

    def _wait_fills(self, order_ids: list[str]) -> None:
        """等卖单到终态 (换档卖后回款再买)。流动性好的 ETF 限价@买一秒级
        成交; 等不到 (废单/卡单) 也放行 —— 买入按真实可用现金, 不足的
        次日补 (状态派生自持仓, 自愈)。墙钟等待, 不用注入时钟 (真实世界)。"""
        if not order_ids:
            return
        import time as _time
        deadline = _time.monotonic() + self._wait_timeout
        pending = set(order_ids)
        while pending and _time.monotonic() < deadline:
            orders = {o["order_id"]: o for o in self._gateway.query_orders()}
            pending = {oid for oid in pending
                       if orders.get(oid, {}).get("status")
                       not in TERMINAL_STATUSES}
            if not pending:
                break
            _time.sleep(self._wait_interval)

    def _place_order(self, code: str, direction: int, price: float, qty: int,
                     reason: str) -> str | None:
        """过风控 → 下单 → 入账 (book/store) → audit。轮动买入 rotation=True
        绕过单笔金额/持仓数上限; 急停/对账/日亏/T+1 四道闸照常。
        返回 order_id (None=风控拒)。"""
        price = round_price_etf(price)
        intent = OrderIntent(code=code, direction=direction, price=price,
                             qty=qty, rotation=True)
        ok, why = self._risk.check(intent, self._build_ctx())
        if not ok:
            self._store.write_audit(
                "rotation_risk_reject",
                f"{code} {'买' if direction == DIRECTION_BUY else '卖'}被风控拒: {why}",
                {"code": code, "qty": qty, "price": price, "reason": why})
            return None
        remark = self._executor.next_remark("R")
        order_id = self._gateway.order(code, direction, price, qty,
                                       PRICE_TYPE_LIMIT, remark)
        label = "ETF轮动买入" if direction == DIRECTION_BUY else "ETF轮动卖出"
        self._executor.register_fill_context(order_id, {"label": label,
                                                        "detail": reason})
        self._book.apply_order_update(
            order_id, OS_REPORTED, code=code, direction=direction,
            price=price, qty=qty, remark=remark)
        self._store.save_order({
            "order_id": order_id, "remark": remark, "code": code,
            "direction": direction, "price": price, "qty": qty,
            "status": OS_REPORTED, "created_ts": self._clock()})
        self._store.write_audit(
            "rotation_order", f"{label} {code} {qty}@{price}",
            {"code": code, "qty": qty, "price": price, "order_id": order_id})
        if direction == DIRECTION_SELL:
            self._pending_sells[code] = {"order_id": order_id, "qty": qty}
        return order_id
