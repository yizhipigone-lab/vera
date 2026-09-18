"""trade/auto_buy.py — 尾盘自动选股买入特性 (2026-08-01 批次4: 组合根瘦身)。

来源:
    从 trade_main.py 整段抽离 (原 _start_auto_buy / _auto_buy_worker /
    _on_signals / _execute_auto_buys / _await_and_fill_dispositions,
    2026-07-27 MVP 起), **纯结构搬迁, 行为零变更** —— 验收网是
    tests/trade/test_auto_buy.py 的 20 个端到端测试, 零改动通过即兼容。

线程纪律 (与原实现完全一致):
    - start() / on_signals() 只在消费者线程被调用 (EventEngine 唯一写者,
      组合根经 EVENT_COMMAND "auto_buy" / EVENT_SIGNALS 两行接线进来);
    - TDX 选股阻塞可达 60s, 在自带工作线程跑, 跑完只 put EVENT_SIGNALS,
      不碰任何交易状态 —— 本类不引入新锁 (消费者线程串行即是锁)。

依赖全注入 (计划书批次4 拍板):
    cfg 传 **getter 而非值** (cfg_getter=lambda: app._cfg) —— 配置热更新
    (_apply_config 换 self._cfg 引用) 才能穿透到本特性, 这是评审 ⚠ 点;
    build_risk_ctx / get_prev_close 留在组合根 (它们读 _reconciled /
    _day_baseline 等组合根状态), 以 callable 注入。
"""

from __future__ import annotations

import datetime as _dt
import threading
import time

from trade.book import (
    DIRECTION_BUY,
    OS_SUCCEEDED,
    PRICE_TYPE_LIMIT,
    TERMINAL_STATUSES,
    is_etf,
    label_of,
)
from trade.events import EVENT_SIGNALS, Event
from trade.closing_auction import auction_buy_price
from trade.decision_codes import action_of as _decision_action  # 决策台账动作码 (2026-09-18)
from trade.executor import PlaceRequest, limit_ratio, round_price
from trade.monitor import is_trading_day_cached
from trade.regime import index_above_ma
from utils.logger import get_logger

_logger = get_logger("trade.auto_buy")


class AutoBuyFeature:
    """尾盘自动选股买入。公开接口 4 个 (批次4 拍板):
    start(source) / on_signals(data) / last / running。"""

    def __init__(self, engine, cfg_getter, store, gateway, book, monitor,
                 risk, executor, *, selection_runner, build_risk_ctx,
                 get_prev_close, budget_provider=None, st_checker=None,
                 clock=time.time):
        self._engine = engine
        self._cfg_getter = cfg_getter      # callable → TradeConfig (热更穿透)
        self._store = store
        self._gateway = gateway
        self._book = book
        self._monitor = monitor
        self._risk = risk
        self._executor = executor
        self._clock = clock
        self._selection_runner = selection_runner
        self._build_risk_ctx = build_risk_ctx
        self._get_prev_close = get_prev_close
        # 2026-08-14 双池预算帽: callable → 股票池还能花的钱 (None=不设帽,
        # 即轮动关闭时的原口径)。注入自 composition root。
        self._budget_provider = budget_provider
        # 2026-09-15 审计修复: ST 判定与 Executor 同一份注入 (TDX IsSTGP),
        # 此前涨停判定漏传 st —— ST 股(5%板)被按 10%/20% 算, 口径偏松。
        self._st = st_checker or (lambda code: False)
        self._running = False                   # 选股工作线程在跑 (防重入)
        self._last: dict | None = None          # 最近一次运行 (页面展示)
        self._placed: tuple = ("", set())       # (日期, 当日已下单代码)
        # disposition 终态轮询参数 (实例属性, 测试直注小值;
        # 生产 2s×~10s, 尾盘窗口可接受 —— 会短暂阻塞消费者线程, 注释即契约)
        self._await_interval = 2.0
        self._await_timeout = 10.0

    # ── 公开接口 ────────────────────────────────────────────────

    @property
    def last(self) -> dict | None:
        """最近一次尾盘自动买入运行结果 (页面展示, 只读)。"""
        return self._last

    @property
    def running(self) -> bool:
        """选股工作线程是否在跑 (防重入状态, 只读)。"""
        return self._running

    def start(self, source: str) -> None:
        """发起一次尾盘选股 (消费者线程内只开线程, 绝不自己跑 TDX)。
        scheduled 要求 enabled + 当天是交易日; manual (api 立即执行) 任何时段
        放行 — 2026-07-27 裁决①同款语义: 人工命令不受时段/开关约束。"""
        if source == "scheduled" and not self._cfg_getter().auto_buy.enabled:
            return  # 未启用: 定时事件静默丢弃 (面板里有关闭语义)
        # 2026-09-18 修: 补交易日守卫 (与 rotation.start 同口径)。此前 scheduled
        # 只判开关, 周末/节假日 14:54 定时器理论上也会跑一遍选股并走到汇总。
        # 静默丢弃、不写审计: 休市日本就不该有"决策", 页面由日历标「休市」。
        if source == "scheduled" and not is_trading_day_cached(
                _dt.datetime.fromtimestamp(self._clock()).date()):
            return
        if self._running:
            self._store.write_audit(
                "auto_buy_skip", "上一次选股仍在运行, 本次忽略",
                {"source": source})
            return
        self._running = True
        threading.Thread(target=self._worker, args=(source,),
                         name="auto-buy-selection", daemon=True).start()
        self._store.write_audit(
            "auto_buy_start", f"尾盘选股已发起 ({source})", {"source": source})

    def on_signals(self, data: dict) -> None:
        """选股结果处理 (消费者线程): 错误记 audit 页面可见;
        正常结果逐票过滤执行。"""
        self._running = False
        if data.get("signals") is None:
            self._last = {
                "ts": self._clock(), "source": data.get("source"),
                "error": data.get("error", "未知错误"), "dispositions": [],
            }
            self._store.write_audit(
                "auto_buy_error", f"尾盘选股失败: {data.get('error')}",
                {"source": data.get("source")})
            self._log_decision(
                "PICK_ERROR",
                f"尾盘选股这一轮没跑成：{data.get('error')}",
                {"source": data.get("source")})
            return
        self._execute(data["signals"], data.get("source", "?"))

    # ── 决策台账 (2026-09-18) ──────────────────────────────────

    def _log_decision(self, code: str, text: str,
                      evidence: dict | None = None, trade_ids=None) -> None:
        """落一行决策台账。尾盘选股是「整池」决策, 对象固定记 ``pool``。

        fail-soft 由 store 层兜底 (``DailyDecisionStore.log`` 内部吞异常 +
        补写审计) —— 台账写不进去绝不影响交易, 最坏是页面上少一行。
        """
        day = _dt.datetime.fromtimestamp(self._clock()).strftime("%Y-%m-%d")
        self._store.decision.log([{
            "trade_date": day, "strategy": "auto_buy", "subject": "pool",
            "action": _decision_action(code), "reason_code": code,
            "reason_text": text, "evidence": evidence or {},
            "trade_ids": trade_ids or "", "source": "live",
        }])

    def _log_pick_result(self, signals: list[dict],
                         dispositions: list[dict], bought: int,
                         source: str) -> None:
        """汇总型落一行台账: 买到了 / 公式没选出票 / 选出来但一张单没下成。

        「凭什么」里两样都要有 (2026-09-18 计划书 B4): ①**每只票的最终状态**
        (已成@价 / 废单(状态码) / 在途, 由 _await_and_fill_dispositions 回填);
        ②**过滤原因统计** (如"涨停拒买 2 只、已持仓 1 只") —— 否则"没买到"
        这件事在页面上只剩一句空话。
        """
        filters: dict[str, int] = {}
        picks: list[dict] = []
        for d in dispositions:
            if d.get("action") == "buy":
                picks.append({"code": d["code"], "qty": d.get("qty"),
                              "price": d.get("price"),
                              "result": d.get("status") or "已下单(状态未回)"})
            else:
                reason = d.get("reason") or "原因不明"
                filters[reason] = filters.get(reason, 0) + 1
                picks.append({"code": d["code"], "result": f"过滤：{reason}"})
        if bought > 0:
            code = "PICK_BUY"
            text = (f"尾盘选股买入了 {bought} 只新股票"
                    f"（公式共选出 {len(signals)} 只）")
        elif not signals:
            code = "PICK_NO_SIGNAL"
            text = "尾盘选股的公式今天一只票都没选出来，所以没有买入"
        else:
            code = "PICK_ALL_FILTERED"
            detail = "、".join(f"{k} {v} 只" for k, v in filters.items()) or "原因不明"
            text = f"尾盘选股选出 {len(signals)} 只，但一张单都没下成（{detail}）"
        tids = [d["order_id"] for d in dispositions
                if d.get("action") == "buy" and d.get("order_id")]
        self._log_decision(code, text, {
            "selected": len(signals), "bought": bought,
            "filters": filters, "picks": picks, "source": source}, tids)

    # ── 内部 ────────────────────────────────────────────────────

    def _worker(self, source: str) -> None:
        """工作线程: TDX 选股阻塞可达 60s, 绝不能跑在消费者线程
        (唯一写者卡死 = 全系统停摆)。跑完只 put 事件, 不碰任何状态。"""
        cfg = self._cfg_getter().auto_buy
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

    def _series_contains_today(self, code: str) -> bool:
        """指数日线序列的末根是否已是"今天" (2026-09-16 审计修复收尾)。

        背景: 本闸门要在"当日这根日线还不在序列里"时追加实时价, 才能判"现在站没
        站上均线"; 但 2026-09-16 的新鲜度修复会让盘后当日日线主动补下载落地 ——
        此时再追加实时价等于把当天计两次, 均线(默认 200 日)被拉偏。
        判据取网关记录的"实际返回序列末根日期" [trade/gateway.py:132-140]:
        盘中当日 bar 被网关按无未来函数裁掉 → 不是今天 → 照旧追加;
        盘后已落地 → 是今天 → 不追加。未知('')按"不在"处理(保持原行为)。
        """
        last_day = self._gateway.last_daily_bar_day(code)
        if not last_day:
            return False
        now = _dt.datetime.fromtimestamp(self._clock())
        return last_day == now.strftime("%Y%m%d")

    def _regime_allows(self, rf) -> bool:
        """弱市择时闸门: 指数最新价 vs MA 均线。True=放行, False=禁买。
        数据不足/取数失败一律 False (fail-closed, 宁可不买不可瞎买)。"""
        try:
            closes = self._gateway.query_daily_closes(
                rf.index_code, count=rf.ma_window + 20)
            q = (self._gateway.query_quotes([rf.index_code]) or {}).get(
                rf.index_code) or {}
            last = q.get("last") or 0.0
            if last > 0 and not self._series_contains_today(rf.index_code):
                closes = list(closes) + [last]
            return bool(index_above_ma(closes, rf.ma_window))  # None → False
        except Exception:
            _logger.exception("弱市择时闸门取数失败 (fail-closed 不买)")
            return False

    def _execute(self, signals: list[dict], source: str) -> None:
        """逐票过滤执行 (消费者线程)。过滤顺序 = 便宜到贵:
        上限 → 已持仓 → ETF → 今日已买过 → 涨停 → 现金/数量 → 风控。"""
        cfg = self._cfg_getter().auto_buy
        # 弱市择时闸门 (2026-08-16): 指数跌破 MA 年线 → 当日不买新仓。
        # 在最早、最便宜处拦 (先于资金/行情查询); 已持仓不受影响。
        rf = self._cfg_getter().regime_filter
        if rf.enabled and not self._regime_allows(rf):
            self._last = {"ts": self._clock(), "source": source,
                          "error": f"弱市闸门: {rf.index_code} 未站上 MA{rf.ma_window}",
                          "dispositions": []}
            self._store.write_audit(
                "auto_buy_skip_regime",
                f"{rf.index_code} 未站上 MA{rf.ma_window}, 尾盘不买", {})
            self._log_decision(
                "PICK_REGIME_BLOCK",
                f"大盘没站上年线：{label_of(rf.index_code)} 的最新价还在 "
                f"{rf.ma_window} 日均线下方，今天不买新股票",
                {"index_code": rf.index_code, "ma_window": rf.ma_window})
            return
        today = time.strftime("%Y%m%d", time.localtime(self._clock()))
        hhmm = time.strftime("%H:%M", time.localtime(self._clock()))
        # 当日已下单代码 (跨批次防重; 批次内也靠它) — 按日重置
        if self._placed[0] != today:
            self._placed = (today, set())
        placed_codes = self._placed[1]
        day_start = time.mktime(time.strptime(today, "%Y%m%d"))
        bought_codes = self._store.bought_codes_since(day_start)
        try:
            cash = self._gateway.query_asset()["cash"]
        except Exception:
            self._store.write_audit(
                "auto_buy_error", "查询资金失败, 本轮自动买入中止", {})
            self._log_decision("PICK_ERROR",
                               "查不到账户可用资金，本轮尾盘买入中止（宁可不动手）")
            return
        # 2026-08-14 双池预算帽: 轮动启用时股票买入被股票池预算封顶,
        # 不花 ETF 池的钱 (卖出回笼的现金让给低配的 ETF 池)。
        budget_capped = False   # 2026-08-17: 记下"现金被预算帽压过", 供下方报准确原因
        if self._budget_provider is not None:
            try:
                cap = self._budget_provider()
                if cap is not None and cap < cash:
                    cash = cap
                    budget_capped = True
            except Exception:
                pass  # 预算帽取不到 fail-open 回退原口径 (软隔离非安全闸)

        positions = self._book.snapshot()["positions"]
        # 2026-07-27 首次实跑修复: 候选票批量取行情。
        # monitor 缓存只覆盖持仓/订阅票, 信号票从未查过 → 61/61 全"无行情"。
        # 批量查一次注入, 缓存兜底(持仓票)。
        # 2026-08-07 (用户拍板): 取快照前先补订阅 —— 信号票不在订阅集,
        # 裸快照可能缺盘口 (ask1=0 → "对手最优"市价兜底 → 券商通道拒单,
        # 0807 实盘实测 4/4 全灭: 深市回 54 / 沪市回 57); 订阅后盘口 tick
        # 进 monitor 缓存, 定价时 monitor.quote_of 兜底才有盘口可用。
        quotes: dict = {}
        codes = [s["code"] for s in signals]
        try:
            self._gateway.subscribe_quotes(codes)
        except Exception as e:
            _logger.warning("尾盘买入补订阅失败 (继续, 快照仍可用): %s", e)
        try:
            quotes = self._gateway.query_quotes(codes) or {}
        except Exception:
            self._store.write_audit(
                "auto_buy_error", f"批量查询行情失败({len(codes)}只), 本轮自动买入中止", {})
            self._log_decision(
                "PICK_ERROR",
                f"这 {len(codes)} 只候选票的行情一只都没拿到，本轮尾盘买入中止")
            return
        # 首轮缺 ask1 的票定向补查一次 (瞬时空盘口/快照残缺给第二次机会)
        missing_ask = [c for c in codes if not (quotes.get(c) or {}).get("ask1")]
        if missing_ask:
            try:
                for c, q in (self._gateway.query_quotes(missing_ask) or {}).items():
                    if q.get("ask1"):
                        quotes[c] = q
            except Exception:
                _logger.warning("尾盘买入盘口补查失败 (%d只), 维持原快照",
                                len(missing_ask))
        dispositions: list[dict] = []
        bought = 0

        def _skip(code: str, reason: str) -> None:
            dispositions.append({"code": code, "action": "skip",
                                 "reason": reason})

        for sig in signals:
            code = sig["code"]
            if bought >= cfg.max_buys_per_day:
                _skip(code, "达每日上限")
                continue
            pos = positions.get(code)
            if pos is not None and pos.volume > 0:
                _skip(code, "已持仓")
                continue
            if self._cfg_getter().exclude_etf and is_etf(code):
                _skip(code, "ETF不管理")
                continue
            if code in placed_codes or code in bought_codes:
                _skip(code, "今日已买过")
                continue
            # 涨停拒买 + 定价: 都需要行情。无价/无昨收 fail-closed —
            # 尾盘买入不是救火, 宁可不买不可瞎买
            quote = quotes.get(code) or self._monitor.quote_of(code)
            if not quote or not quote.get("last"):
                _skip(code, "无行情")
                continue
            prev_close = quote.get("prev_close") or self._get_prev_close(code)
            if not prev_close:
                _skip(code, "无昨收无法判涨停")
                continue
            limit_up = prev_close * (1 + limit_ratio(code, self._st(code)))
            if quote["last"] >= limit_up:
                _skip(code, "涨停拒买")
                continue
            price = quote.get("ask1") or quote["last"]
            amount = min(cfg.amount_per_stock, cash * 0.95)
            qty = int(amount / price / 100) * 100
            if qty < 100:
                # 2026-08-17: 区分"买不起一手"的真实原因, 别让"现金不足"误导——
                # 双池预算帽把股票池额度压到 0 时账户其实有钱 (钱归 ETF 池)。
                if cfg.amount_per_stock < price * 100:
                    _skip(code, "单票上限低于一手")
                elif budget_capped:
                    _skip(code, "股票池预算不足")
                else:
                    _skip(code, "现金不足一手")
                continue
            # 风控检查移入唯一下单口 (计划书 T4): risk_price=price 参考价,
            # 现状语义"风控先于定价吃参考价"经 risk_price 字段保持 (审计 P1)。
            # 定价 (2026-07-27 实测驱动, 市场感知; 2026-08-01 P0-2 修复):
            # - ≥force_market_after: **全板块禁市价单** —— 深市 14:57-15:00
            #   收盘集合竞价只收限价单 (07-31 实测 5 张"对手最优"全废单),
            #   沪市 2018 年起收盘也是集合竞价、同样拒市价单 (6/6 废单)。
            # - .SZ 创业/科创 (300/301/688): 20% 涨停价超 2% 价格笼子,
            #   被交易主机暂存不废不成交 (07-31 实测 0/12 零成交),
            #   改为 min(涨停价, 卖一×1.02) 贴笼子上限。
            #   **注意**: 科创板 688.SH 同样是 20% 涨停 + 2% 笼子,
            #   不能靠 is_sz 判定 —— 688 是沪市, is_sz 恒 False,
            #   条件只用代码前缀 (08-01 实测 688099 同死法)。
            # - 深主板 / 沪主板: 10% 涨停价在笼子内, 直接挂涨停
            #   (单一价格撮合, 成交价=收盘价, 与限价@涨停同效)。
            force = hhmm >= self._cfg_getter().force_market_after
            if force:
                order_type = PRICE_TYPE_LIMIT
                # 收盘竞价限价规则单一实现 (2026-09-15 收口 trade/closing_auction,
                # 原此处女 20% 品种/主板分支与 executor 卖侧为双胞胎硬编码)
                order_price, price_kind = auction_buy_price(
                    code, quote.get("ask1") or quote["last"], limit_up)
            elif not quote.get("ask1"):
                # 2026-08-12 (用户拍板): 限价兜底替代"对手最优"市价单 ——
                # 券商柜台禁市价类程序化报单 (63596 废单, 08-12 沪市 7/7 全废),
                # 深市对手最优 IOC 性质剩余自动撤 (08-12 深市 4/4 已撤)。
                # 无盘口时用 最新价+0.5% 限价 (08-12 用户拍板, 比 1% 买得便宜;
                # 够不着的概率略升, 但挂单是"未成交"不是废单, 收盘自动失效),
                # 涨停帽封顶防越界废单。
                order_price = round_price(min(limit_up, quote["last"] * 1.005))
                order_type = PRICE_TYPE_LIMIT
                price_kind = "限价兜底(无盘口)"
            else:
                order_price = price
                order_type = PRICE_TYPE_LIMIT
                price_kind = "卖一价"
            # 唯一下单口 (计划书 T4): 七步脊柱收口。
            # price=order_price 实际委托价 (P0-6 入账/发单口径, 原注释收编);
            # risk_price=price 参考价 (风控金额闸口径, 审计 P1)。
            order_id, why = self._executor.place_order(PlaceRequest(
                code=code, direction=DIRECTION_BUY, price=order_price, qty=qty,
                risk_price=price, price_type=order_type, remark_prefix="B",
                fill_payload={"label": "TDX买入"},
                audit_kind="auto_buy",
                audit_message=f"尾盘买入 {code} {qty}@{order_price or price} ({price_kind})",
                audit_extra={"code": code, "qty": qty,
                             "price": order_price or price, "order_id": None,
                             "source": source, "price_kind": price_kind}),
                risk_ctx=self._build_risk_ctx())
            if order_id is None:
                _skip(code, f"风控拒: {why}")
                continue
            placed_codes.add(code)
            cash -= qty * price
            bought += 1
            dispositions.append({"code": code, "action": "buy",
                                 "price": order_price or price, "qty": qty,
                                 "order_id": order_id, "reason": price_kind})

        self._await_and_fill_dispositions(dispositions)
        summary = {
            "ts": self._clock(), "source": source,
            "selected": len(signals), "bought": bought,
            "dispositions": dispositions,
        }
        self._last = summary
        # 汇总落 audit (结构化 detail_json 即持久化, 页面可读)
        self._store.write_audit(
            "auto_buy_summary",
            f"尾盘自动买入: 选中 {len(signals)} / 买入 {bought} ({source})",
            {"selected": len(signals), "bought": bought,
             "dispositions": dispositions})
        # 决策台账 (2026-09-18): 整池一条 —— "今天为什么买 / 为什么没买"
        self._log_pick_result(signals, dispositions, bought, source)

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
            orders = {o["order_id"]: o for o in self._gateway.query_orders()}
            pending_ids = {oid for oid in pending_ids
                           if orders.get(oid, {}).get("status")
                           not in TERMINAL_STATUSES}
            if not pending_ids:
                break
            time.sleep(self._await_interval)
        trades = {t["order_id"]: t for t in self._gateway.query_trades()}
        orders = {o["order_id"]: o for o in self._gateway.query_orders()}
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
