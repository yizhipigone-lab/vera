"""trade/rotation.py — ETF 轮动 + 双池资金分配 (2026-08-14)。

设计意图 (照 trade/auto_buy.py 骨架):
    在选股系统之外新增一个 ETF 轮动系统, 两个系统同时跑, 资金按
    etf_ratio 分两半, 靠"预算帽"软隔离。信号计算在工作线程 (拉
    399673 日线收盘价, 阻塞可达数秒), 调仓执行在消费者线程 (唯一写者)。

规则 (用户手册, 唯一真相):
    - MA20 向上 且 回撤 >= -20% → 满仓 159949 (STATE_FULL_CYB)
    - MA20 向上 但 回撤 < -20%  → 半仓 159949+518880 (STATE_HALF)
    - MA20 向下                → 满仓 518880 (STATE_FULL_GOLD)
    "满仓" = ETF 池的 100% = etf_ratio × 总资产 (不是整个账户)。

调仓 (模型 B: 预算帽 + 自然回笼 + 双向, 用户 2026-08-14 拍板):
    每日算目标市值 = 三态比例 × (etf_ratio × 总资产), 与当前持仓对比:
    - 状态变了 → 换档 (先卖超出的 ETF, 回款后买目标 ETF)
    - 状态没变 → 只补仓 (低配的 ETF 用自由现金买, 绝不主动卖来凑比例)
    当前状态从持仓市值派生 (自愈: 换档半途失败, 次日按市值差额自然补齐,
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
from trade.events import EVENT_ROTATION, Event
from trade.monitor import is_trading_day_cached, trading_session
from trade.quote_stale import is_quote_stale
from trade.risk import OrderIntent
from utils.logger import get_logger

_logger = get_logger("trade.rotation")

# 三态常量 (持久化/UI 共用的字符串)
STATE_FULL_CYB = "full_cyb"    # 满仓创业板50ETF
STATE_HALF = "half"            # 半仓 (创业板+黄金各半)
STATE_FULL_GOLD = "full_gold"  # 满仓黄金ETF

# 三态 → (创业板ETF比例, 黄金ETF比例)
STATE_RATIOS = {
    STATE_FULL_CYB: (1.0, 0.0),
    STATE_HALF: (0.5, 0.5),
    STATE_FULL_GOLD: (0.0, 1.0),
}

_LOT = 100  # ETF 一手 = 100 份


def round_price_etf(x: float) -> float:
    """ETF 场内基金最小报价单位 0.001 元, 用千分位四舍五入。
    不能复用 executor.round_price(股票 0.01 档): 2.004 → 2.00 会挂在不成交价
    (审计 HIGH#1)。"""
    return int(x * 1000 + 0.5) / 1000


def compute_signal(closes: list[float], ma_window: int = 20,
                   high_window: int = 250,
                   drawdown_threshold: float = 0.20) -> dict:
    """纯函数: 收盘价序列 (交易日升序) → 信号 dict。

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


def _derive_state(v_cyb: float, v_gold: float) -> str | None:
    """从两只 ETF 的市值派生当前状态 (自愈: 换档半途失败次日自然补齐)。
    None = 两只都空仓 (首日未建仓)。阈值 90%/10% 容差防碎仓误判。"""
    total = v_cyb + v_gold
    if total <= 0:
        return None
    ratio = v_cyb / total
    if ratio >= 0.9:
        return STATE_FULL_CYB
    if ratio <= 0.1:
        return STATE_FULL_GOLD
    return STATE_HALF


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
        # 冷启动恢复最近一次信号 (UI 展示用; 不驱动交易)
        try:
            self._last = store.rotation_signal.load()
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
        """信号结果处理 (消费者线程): 失败记 audit; 正常则执行调仓 + 持久化。"""
        self._running = False
        source = data.get("source")
        signal = data.get("signal")
        try:
            if signal is None:
                self._last = {"ts": self._clock(), "source": source,
                              "error": data.get("error", "未知错误")}
                self._store.write_audit(
                    "rotation_error", f"轮动信号失败: {data.get('error')}",
                    {"source": source})
                return
            self._last = {"ts": self._clock(), "source": source, "signal": signal}
            if signal.get("state") is None:
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
        """工作线程: 拉 399673 日线收盘价 (阻塞) → 算信号 → 只 put 事件。
        取数口径 (最新完整收盘, 无未来函数): 收盘后 (≥15:05) 用今日收盘价,
        盘中/盘前用昨日 —— 收盘后点「立即」看的是今日信号 (方案 A, 2026-08-14)。"""
        cfg = self._cfg_getter().rotation
        try:
            now = datetime.fromtimestamp(self._clock())
            # 15:05 后今日日线已完整 (收盘 15:00 + 5 分钟 feed 缓冲)
            after_close = now.hour > 15 or (now.hour == 15 and now.minute >= 5)
            end = now if after_close else (now - timedelta(days=1))
            # 回退到最近交易日 (审计 L1: 周一/节后用自然日减一会标成非交易日)
            while not is_trading_day_cached(end.date()):
                end -= timedelta(days=1)
            end_str = end.strftime("%Y%m%d")
            # 取最近 N 根收盘价 (count 口径, 避开 start/end 日期格式坑);
            # high_window + ma_window + 100 留足非交易日余量
            need = cfg.high_window + cfg.ma_window + 100
            closes = self._gateway.query_daily_closes(cfg.signal_index, count=need)
            signal = compute_signal(closes, cfg.ma_window, cfg.high_window,
                                    cfg.drawdown_threshold)
            signal["date"] = end_str     # 信号基于哪一天的收盘价 (非运行日)
            signal["index"] = cfg.signal_index
            # 注 (审计 L6): 本线程读 cfg 算信号, 消费者线程 _execute 会再读 cfg
            # 取 etf_ratio/代码。若两步之间热改配置, 信号按旧窗口算、调仓按新
            # 配置执行; 窗口极小(秒级)且次日按持仓派生自愈, 接受此边界。
            self._engine.put(Event(type=EVENT_ROTATION, ts=self._clock(),
                                   data={"signal": signal, "source": source}))
        except Exception as e:
            _logger.exception("ETF 轮动信号工作线程异常")
            self._engine.put(Event(type=EVENT_ROTATION, ts=self._clock(),
                                   data={"signal": None, "error": str(e),
                                         "source": source}))

    def _execute(self, signal: dict) -> None:
        """调仓执行 (消费者线程)。fail-closed: 任何一步拿不到数据就不动作。"""
        cfg = self._cfg_getter().rotation
        if trading_session(self._clock()) != "continuous":
            self._store.write_audit("rotation_skip", "非连续竞价时段, 跳过调仓", {})
            return
        target_state = signal.get("state")
        if target_state not in STATE_RATIOS:
            self._store.write_audit("rotation_skip", "信号状态非法, 跳过调仓",
                                    dict(signal))
            return
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
        cyb_code, gold_code = cfg.cyb_etf, cfg.gold_etf
        # 用 QMT 真实持仓 (book.can_use 可能因在途冻结/预埋陈旧, 审计 M1;
        # 与 executor 卖出前 _refresh_can_use 同口径 —— QMT 是可卖量唯一真相源)
        qmt_pos0 = {p["code"]: p for p in self._gateway.query_positions()}
        cyb_vol = int((qmt_pos0.get(cyb_code) or {}).get("volume", 0) or 0)
        gold_vol = int((qmt_pos0.get(gold_code) or {}).get("volume", 0) or 0)
        cyb_can = int((qmt_pos0.get(cyb_code) or {}).get("can_use", 0) or 0)
        gold_can = int((qmt_pos0.get(gold_code) or {}).get("can_use", 0) or 0)
        v_cyb = self._etf_value(cyb_code, cyb_vol)
        v_gold = self._etf_value(gold_code, gold_vol)
        derived = _derive_state(v_cyb, v_gold)
        state_changed = derived != target_state
        cyb_ratio, gold_ratio = STATE_RATIOS[target_state]
        t_cyb = cyb_ratio * pool
        t_gold = gold_ratio * pool

        quotes = self._fetch_quotes([cyb_code, gold_code])

        # 换档: 只卖"状态变了才卖" (模型 B: 不因上涨漂移而主动卖)
        self._pending_sells.clear()   # 当日重跑时重置在途卖单登记 (审计 M4)
        sell_ids: list[str] = []
        if state_changed:
            oid = self._sell_to(cyb_code, v_cyb, t_cyb, quotes.get(cyb_code), cyb_can)
            if oid:
                sell_ids.append(oid)
            oid = self._sell_to(gold_code, v_gold, t_gold, quotes.get(gold_code), gold_can)
            if oid:
                sell_ids.append(oid)

        # 卖单等成交 (限价@买一, 流动性好的 ETF 秒级成交)
        self._wait_fills(sell_ids)

        # 卖后重读 QMT 真实持仓 (book 可能尚未消费成交回报), 算真实 ETF 池值。
        # 关键(审计 CRITICAL#2): 卖未成交 → 池值未降 → 池级预算帽归零 → 不超买,
        # 绝不用股票池现金补 ETF 池。
        qmt_pos = {p["code"]: p for p in self._gateway.query_positions()}
        cyb_vol2 = int((qmt_pos.get(cyb_code) or {}).get("volume", 0) or 0)
        gold_vol2 = int((qmt_pos.get(gold_code) or {}).get("volume", 0) or 0)
        v_cyb2 = self._etf_value(cyb_code, cyb_vol2)
        v_gold2 = self._etf_value(gold_code, gold_vol2)

        # 回款刷新现金 (卖单已成交则含卖款)
        try:
            cash = float(self._gateway.query_asset().get("cash", 0.0) or 0.0)
        except Exception:
            cash = 0.0
        # 池级预算帽: 只补到 E=etf_ratio×总资产, 绝不超配 (双向自然再平衡的落点)
        pool_gap = max(0.0, pool - v_cyb2 - v_gold2)
        cash = min(cash, pool_gap)
        cash = self._buy_to(cyb_code, max(0.0, t_cyb - v_cyb2), cash, quotes.get(cyb_code))
        cash = self._buy_to(gold_code, max(0.0, t_gold - v_gold2), cash, quotes.get(gold_code))

        self._store.write_audit(
            "rotation_summary",
            f"ETF 轮动: 目标 {target_state} (池 {pool:.0f}), "
            f"现值 创{v_cyb:.0f}/金{v_gold:.0f}, "
            f"换档 {state_changed}, 状态派生自 {derived}",
            {"state": target_state, "derived": derived, "changed": state_changed,
             "pool": round(pool, 2), "v_cyb": round(v_cyb, 2),
             "v_gold": round(v_gold, 2), "signal": signal})

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
