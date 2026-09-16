"""trade/rotation.py — ETF 轮动 + 双池资金分配 (2026-08-14; 2026-08-20 动量改造;
2026-09-16 资金三份错峰改造)。

设计意图 (照 trade/auto_buy.py 骨架):
    在选股系统之外新增一个 ETF 轮动系统, 两个系统同时跑, 资金按
    etf_ratio 分两半, 靠"预算帽"软隔离。信号计算在工作线程 (拉
    两只风险腿日线收盘价, 阻塞可达数秒), 调仓执行在消费者线程 (唯一写者)。

规则 (用户手册, 唯一真相; 2026-08-20 由 MA20 三态改造为动量):
    - 两只风险腿 (cyb_etf + risk_etf2) 各算 4 周动量 m = 末收盘/N日前收盘 − 1,
      择动量最高 且 > 0 的腿满仓; 两腿都 ≤ 0 → 满仓避险篮子 (默认单黄金,
      "空仓买黄金")。
    - 日频移动止损: 持仓风险腿期间, 每日更新「持仓期最高价 H」, 当日
      < H×(1−trailing_stop_pct) → 当日切避险篮子 (H 换腿时重置)。
    - 周频信号: 每份在自己的信号日 (signal_day 锚定, 节假日前移) 尾盘 14:54
      重算动量并**当日直接执行** (T 日执行, 对齐回测「信号日 T 日收盘价买入」铁律);
      其余交易日尾盘只做日频移动止损检查 + 维持当前目标。
    "满仓" = 该份 ETF 池的 100% = etf_ratio × 总资产 ÷ 份数 (不是整个账户)。

资金分份错峰 (2026-09-16, 计划书 docs/plan/2026-09-16_ETF轮动资金三份错峰改造_计划书.md):
    config.signal_day 为 tuple: 单元素 = 一份资金 (旧行为); 多元素 = 轮动池
    等分 N 份, 各份独立按各自锚定信号日跑同一套规则 (单一代码路径, N=1 即退化)。
    关键结构:
    - rotation_lots 表: 份内虚拟持仓簿记 {(tranche, code): qty+entry_high} ——
      QMT 账户级同码持仓合并, 份归属只记在这; 与 QMT 漂移只告警不改账 (铁律1);
    - 持仓/止损判定以 lots 份内为准 (QMT 只剩对账 + can_use 两个用途);
    - 统一执行 pass: 一次运行算 N 遍、下一遍单 (分单不并单 —— 成交回报按
      order_id 归份无歧义); clear_external_sells 每次运行只在开头调一次;
    - 迁移 (D4): 首次运行把现有持仓按手轮转分份、继承/推算 entry_high、
      打「已初始化」标志防误删重迁移。

调仓 (模型 B: 预算帽 + 自然回笼 + 双向, 用户 2026-08-14 拍板, 逐份适用):
    每份每日算目标市值 = 目标腿 100% 份池 或 避险篮子 100% 份池, 与份内持仓对比:
    - 目标腿变了 → 换档 (先卖该份超出的 ETF, 回款后买目标 ETF)
    - 目标没变 → 只补仓 (低配的 ETF 用自由现金买, 绝不主动卖来凑比例)
    份内持仓从 rotation_lots 派生; 换档半途失败由次日对账告警 + 人工核对兜底。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from trade.book import (
    DIRECTION_BUY,
    DIRECTION_SELL,
    TERMINAL_STATUSES,
    round_price_etf,  # 价格档位单一真相源 (治理III W3 自本模块迁入 book.py)
)
from scheduler.trading_calendar import next_trading_day
from trade import pool_money  # 市值口径单一真相源 (治理III W2-1)
from trade.events import EVENT_ROTATION, Event
from trade.executor import PlaceRequest  # 唯一下单口请求 (2026-09-05 收口)
from trade.monitor import is_trading_day_cached, trading_session
from trade.quote_stale import is_quote_stale
from trade.shadow import SHADOW_LOG_PATH, run_shadow
from utils.logger import get_logger

_logger = get_logger("trade.rotation")

# 周频信号日锚定 weekday (config.signal_day 单项 → datetime.weekday() 0=周一)。
_SIGNAL_DAY_WEEKDAY = {"monday": 0, "tuesday": 1, "wednesday": 2,
                       "thursday": 3, "friday": 4}
_WD_CN = {"monday": "周一", "tuesday": "周二", "wednesday": "周三",
          "thursday": "周四", "friday": "周五"}


def _is_signal_day(d, signal_day: str) -> bool:
    """d 是否周频信号日 (计划书自审 §八.3): 信号日 = 每周「最后一个交易日
    ≤ 配置锚定 weekday」, 节假日休市时前移到最后一个能交易的 weekday。
    信号日尾盘算动量并**当日执行** (T 日执行)。"""
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


def _etf_label(code: str) -> str:
    """代码 → '简称(代码)' 给人看; 查不到简称时退回原代码。

    2026-09-07 用户反馈: 审计/决策文案满屏 513100.SH 谁看得明白。
    复用 trade/analysis.name_of 名称表 (进程级缓存, 拿不到不落缓存会重试);
    惰性 import 防模块环。只影响人类可读文案, 机器字段仍存原代码。"""
    try:
        from trade.analysis import name_of
        name = name_of(code)
    except Exception:
        name = ""
    return f"{name}({code})" if name else code


def _fmt_momentum(momentum: dict | None) -> str:
    """动量 dict {代码: float|None} → 人话字符串 '创业板50(159949.SZ) +3.2% / 纳指ETF(513100.SH) +8.5%'。
    空/None/全 None 返回 ''。None 表示该腿数据不足 (不参与择腿)。"""
    if not momentum:
        return ""
    parts = []
    for c, m in momentum.items():
        parts.append(f"{_etf_label(c)} {'—' if m is None else f'{float(m) * 100:+.1f}%'}")
    return " / ".join(parts)


def compute_momentum_signal(closes_by_leg: dict, momentum_window: int = 20) -> dict:
    """纯函数: {腿代码: 收盘序列(交易日升序)} → 动量择腿信号 dict (2026-08-20 动量改造)。

    逐腿算动量 m = 末收盘 / momentum_window 日前收盘 − 1，取动量最高 且 > 0 的腿为目标；
    两腿都 ≤ 0 → target=None (切避险篮子, "空仓买黄金")。
    数据不足的腿 (closes 长度 ≤ momentum_window 或 N日前收盘 ≤ 0) 不参与择腿;
    两腿都不足 → target=None + reason (fail-safe 切避险, 同旧三态口径)。
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


class RotationFeature:
    """ETF 轮动特性。公开接口 (照 AutoBuyFeature): start / on_signals /
    last / running。依赖全注入, cfg 传 getter (热更穿透)。

    2026-09-16 三份错峰: 份运行时状态 self._tranches [{anchor, pending_target,
    has_target, last_signal}], 份内持仓 self._lots {(i, code): {qty, entry_high}}
    (镜像 store rotation_lots 表, 消费者线程唯一写)。
    """

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
        # 份内虚拟持仓 {(tranche, code): {"qty": int, "entry_high": float}}
        # (2026-09-16 错峰; store rotation_lots 表的内存镜像, 消费者线程唯一写)
        self._lots: dict = {}
        # 份运行时状态 [{anchor, pending_target, has_target, last_signal}]
        self._tranches: list[dict] = []
        self._anchors_key: tuple = ()
        # 「已初始化」标志 (计划书 D4): False → 首个执行 pass 做迁移初始化
        self._migration_pending = True
        # 冷启动恢复: lots + 逐份状态 (entry_high 随 lots, 不再依赖单条 blob —
        # 2026-09-10 信号 blob 被覆盖 → 全系统失忆事故, 计划书 D5/D7)
        states: dict = {}
        try:
            self._lots = store.rotation_lots.load_all()
            states = store.rotation_tranche.load_all()
            self._migration_pending = (
                store.rotation_meta.get("lots_initialized") != "1")
        except Exception:
            _logger.exception("轮动状态冷启动恢复异常 (按未初始化处理)")
        self._sync_tranches(states)
        if self._lots:
            # 有簿记 = 已初始化过 (meta 丢失时的自愈, 但不反向重排 ——
            # lots 表本身就是真相, 无需迁移)
            self._migration_pending = False
        if any(t.get("last_signal") for t in self._tranches):
            # 冷启动 UI 回看: 从逐份状态重建 _last (旧版从单行 blob 恢复,
            # 2026-09-16 起改从 tranche_state; 只组装展示, 不回写)
            tranches_out = [{"tranche": i, "anchor": t["anchor"],
                             "signal": t["last_signal"]}
                            for i, t in enumerate(self._tranches)]
            self._last = {"ts": None, "source": "restore",
                          "tranches": tranches_out,
                          "signal": tranches_out[0]["signal"]}

    # ── 公开接口 ────────────────────────────────────────────────

    @property
    def last(self) -> dict | None:
        """最近一次信号+调仓结果 (页面展示, 只读)。
        结构: {"ts","source","tranches":[{tranche,anchor,signal}],
               "signal": 第 0 份 signal (旧字段, 兼容旧前端/日报)}。"""
        return self._last

    @property
    def running(self) -> bool:
        """信号工作线程是否在跑 (防重入, 只读)。"""
        return self._running

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
        # 审计 T4 (2026-09-16): 份状态对齐 (锚定热变更重建 + audit 留痕)
        # 从信号工作线程挪到此处 —— 交易状态唯一写者=消费者线程,
        # 工作线程只读 self._tranches (start→Thread 启动的 happens-before
        # 保证读到本次对齐结果)。
        self._sync_tranches()
        self._running = True
        try:
            threading.Thread(target=self._worker, args=(source,),
                             name="rotation-signal", daemon=True).start()
        except Exception as e:
            # 审计 L8: 线程创建失败时复位 _running, 否则后续触发恒被"上一次
            # 仍在运行"拦下、轮动永久静默停摆 (极罕见, 但活性要保证)
            self._running = False
            self._store.write_audit(
                "rotation_error", f"信号线程创建失败: {e}",
                {"source": source})
            return
        self._store.write_audit(
            "rotation_start", f"ETF 轮动已发起 ({source})", {"source": source})

    def on_signals(self, data: dict) -> None:
        """信号结果处理 (消费者线程)。数据形态:
          - signals=None / signal=None → 错误 (记 audit)
          - signal_only=True            → 非交易日/首信号未算: 纯跳过, 不执行
          - 正常 {"signals": {份号: 信号}} → 执行统一调仓 pass (_execute)
        兼容旧形态 {"signal": {...}} → 归一为 {0: signal} (单份, 测试/旧事件)。
        无论哪条路径, finally 都把实例当前状态注入落库 (2026-09-16 D7:
        杜绝 9-10 「跳过路径用空信号覆盖好状态」事故)。
        """
        self._running = False
        source = data.get("source")
        signals = data.get("signals")
        if signals is None and "signal" in data:
            sig = data.get("signal")
            signals = None if sig is None else {0: sig}
        signal_only = bool(data.get("signal_only"))
        error = None
        try:
            if signals is None:
                error = data.get("error", "未知错误")
                self._store.write_audit(
                    "rotation_error", f"轮动信号失败: {error}",
                    {"source": source})
                return
            if signal_only:
                # 非交易日 / 首个周频信号未算: 纯跳过, 不执行不更新目标状态
                note = (data.get("note")
                        or next((s.get("note") for s in signals.values()
                                 if isinstance(s, dict) and s.get("note")),
                                None)
                        or "跳过 (非交易日或首信号未算)")
                self._store.write_audit("rotation_skip", note,
                                        {"source": source})
                return
            self._execute(signals, data.get("migration_closes") or {})
        finally:
            # 异常路径也尽量落簿记 (内存态已变的别丢) + 注入实例状态 (D7)
            self._persist_lots()
            self._save_states(source, error)

    # ── 份状态 ──────────────────────────────────────────────────

    def _sync_tranches(self, states: dict | None = None) -> None:
        """按 cfg.signal_day 对齐份运行时状态 (锚定变了 → 重建, lots 不动)。

        冷启动时从 tranche_state 表恢复 pending_target/has_target
        (has_target 兼容旧记录: 无显式键时以 "pending_target" in signal 判);
        锚定热变更 → 份状态清零重来 (计划书 §七: 改份数/锚定建议重启)。

        调用纪律 (审计 T4, 2026-09-16): 仅在消费者线程调用 (构造函数 /
        start() 发起工作线程前) —— 本函数重建共享状态并写 audit,
        交易状态唯一写者=消费者线程; 信号工作线程只读 self._tranches。
        """
        anchors = tuple(self._cfg_getter().rotation.signal_day)
        if anchors == self._anchors_key and self._tranches:
            return
        old = self._tranches
        if old and anchors != self._anchors_key:
            # 锚定/份数热变更: 份状态按新锚定重建 (计划书 §七: 建议重启;
            # 审计留痕让人看得见, 别静默换节奏 —— 设计评审补)
            self._store.write_audit(
                "rotation_reanchor",
                f"轮动信号日锚定热变更 {list(self._anchors_key)} → "
                f"{list(anchors)}, 份状态按新锚定重建 (建议重启)",
                {"old": list(self._anchors_key), "new": list(anchors)})
        self._anchors_key = anchors
        self._tranches = []
        for i, anchor in enumerate(anchors):
            t = {"anchor": anchor, "pending_target": None,
                 "has_target": False, "last_signal": None}
            # 优先按份号从持久化恢复; 锚定未变时也可从旧内存态带过来
            rec = (states or {}).get(i)
            sig = (rec or {}).get("signal") if isinstance(rec, dict) else None
            if isinstance(sig, dict):
                t["pending_target"] = sig.get("pending_target")
                t["has_target"] = sig.get("has_target",
                                          "pending_target" in sig)
                t["last_signal"] = sig
            elif i < len(old) and old[i]["anchor"] == anchor:
                t.update({k: old[i][k] for k in
                          ("pending_target", "has_target", "last_signal")})
            self._tranches.append(t)

    def _save_states(self, source: str | None, error: str | None) -> None:
        """逐份落 tranche_state + 组装 self._last (D7: 任何路径都注入实例
        当前 pending_target/entry_high/has_target —— 实例内存即真相,
        不存在「忘了写键」的跳过路径)。fail-soft, 不影响交易。"""
        try:
            ts = self._clock()
            tranches_out = []
            for i, t in enumerate(self._tranches):
                sig = dict(t.get("last_signal") or {})
                sig["anchor"] = t["anchor"]
                sig["pending_target"] = t["pending_target"]
                sig["has_target"] = t["has_target"]
                sig["entry_high"] = {
                    c: v["entry_high"]
                    for (j, c), v in self._lots.items()
                    if j == i and v.get("entry_high")}
                rec = {"ts": ts, "source": source, "signal": sig}
                self._store.rotation_tranche.save(i, rec)
                tranches_out.append(
                    {"tranche": i, "anchor": t["anchor"], "signal": sig})
            self._last = {"ts": ts, "source": source,
                          "tranches": tranches_out,
                          "signal": tranches_out[0]["signal"]
                          if tranches_out else None}
            if error:
                self._last["error"] = error
        except Exception:
            _logger.debug("轮动状态落库异常 (不影响交易)")

    # ── 内部: 工作线程 ──────────────────────────────────────────

    def _worker(self, source: str) -> None:
        """工作线程: 逐份判定信号日 (各自锚定) → 有信号日的份共享一次动量计算
        (同窗口同数据, 一天只拉一次); 迁移挂起时顺带取轮动池代码近 20 日收盘
        (entry_high 回退推算用)。只 put 事件, 不碰交易写者。
        取数口径 (2026-08-16 拍板): 收盘后 (≥15:05) 用今日完整日线; 盘中 (尾盘
        14:54) 用实时价当今日收盘价; 无实时价/非交易日回退昨日 —— 无未来函数。"""
        try:
            now = datetime.fromtimestamp(self._clock())
            today = now.date()
            if not is_trading_day_cached(today):
                self._engine.put(Event(
                    type=EVENT_ROTATION, ts=self._clock(),
                    data={"signals": {}, "signal_only": True,
                          "note": "非交易日, 不动作", "source": source}))
                return
            cfg = self._cfg_getter().rotation
            self._shadow_tick(today)   # 2026-08-23 P0: 影子三态每日落盘 (只记录不交易)

            # 迁移挂起 (D4): 查账户里有没有轮动池持仓, 有则取近 20 日收盘
            # (消费者线程迁移时推算 entry_high 用; 取数在工作线程, 不堵写者)
            migration_closes: dict = {}
            held_codes: set = set()
            if self._migration_pending:
                try:
                    rot_codes = self._rotation_codes(cfg)
                    for p in self._gateway.query_positions() or []:
                        if (p.get("code") in rot_codes
                                and int(p.get("volume", 0) or 0) > 0):
                            held_codes.add(p["code"])
                    for code in held_codes:
                        closes, _src = self._fetch_closes(
                            code, cfg.momentum_window + 1, 2)
                        if closes:
                            migration_closes[code] = closes[
                                -cfg.momentum_window:]
                except Exception as e:
                    _logger.warning("迁移预取数失败 (迁移时退实时价): %s", e)

            need = [i for i, t in enumerate(self._tranches)
                    if _is_signal_day(today, t["anchor"])]
            signals: dict = {}
            if need:
                # 多份同窗口同标的 → 算一次, 各份共享 (dict 浅拷贝, 嵌套只读)
                sig = self._compute_momentum(now)
                for i in need:
                    signals[i] = dict(sig)
            if (not signals and not self._lots and not held_codes
                    and not any(t["has_target"] for t in self._tranches)):
                # 冷启动后首个周频信号还没算: 不动作, 等信号日 (避免先买黄金再换腿)
                self._engine.put(Event(
                    type=EVENT_ROTATION, ts=self._clock(),
                    data={"signals": {}, "signal_only": True,
                          "note": "首个周频信号未算, 等信号日",
                          "source": source}))
                return
            self._engine.put(Event(
                type=EVENT_ROTATION, ts=self._clock(),
                data={"signals": signals,
                      "migration_closes": migration_closes,
                      "source": source}))
        except Exception as e:
            _logger.exception("ETF 轮动信号工作线程异常")
            self._engine.put(Event(type=EVENT_ROTATION, ts=self._clock(),
                                   data={"signals": None, "error": str(e),
                                         "source": source}))

    def _shadow_tick(self, today) -> None:
        """影子三态每日落盘 (2026-08-23 P0 影子校尺, 只记录不交易)。

        拉 cyb_etf 收盘 → 复用旧 MA20 三态规则 (已迁 trade/legacy_three_state,
        单一规则源, 经 shadow.run_shadow 落盘)
        → 追加 data/shadow_rotation.jsonl; tools/shadow_compare.py 季度对比
        影子 vs 实盘滚动 90 日收益。

        **只用 QMT 快源, 不走 _fetch_closes 降级链** (2026-08-23 测试超时修复):
        TDX/腾讯降级是秒级网络阻塞, 会把工作线程拖过交易窗口; 影子是
        best-effort 日报, QMT 不在就跳过今天 (次日按日去重自然补),
        历史空洞由 shadow_compare 用全量历史重放补齐, 不靠生产快照。"""
        try:
            cfg = self._cfg_getter().rotation
            closes = self._gateway.query_daily_closes(cfg.cyb_etf, count=260)
            if not closes or len(closes) < 251:
                _logger.info("影子三态 QMT 取数不足 (%d 根), 本日跳过",
                             len(closes or []))
                return
            run_shadow([float(c) for c in closes],
                       today.strftime("%Y%m%d"), SHADOW_LOG_PATH)
        except Exception as e:
            _logger.warning("影子三态落盘失败 (不影响交易): %s", e)

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
                # 2026-09-16 审计修复①: 补下载后仍陈旧时网关会落 history_stale
                # 标记 —— 这里把"陈旧"写进来源名, 让交易页看得见 (不再静默)。
                stale = getattr(self._gateway, "history_stale", {}).get(code)
                if stale:
                    _logger.warning("轮动信号 %s 用陈旧 QMT 日线 (%s)", code, stale)
                return [float(c) for c in closes], ("QMT(陈旧)" if stale else "QMT")
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

    # ── 内部: 统一执行 pass (消费者线程) ─────────────────────────

    @staticmethod
    def _rotation_codes(cfg) -> set:
        """轮动池全部代码 —— 委托 pool_money.rotation_codes 唯一真相源
        (2026-09-16 设计评审收口: 本模块曾手写第二份, 沉淀经验#1)。"""
        return pool_money.rotation_codes(cfg)

    def _execute(self, signals: dict, migration_closes: dict) -> None:
        """统一执行 pass (消费者线程, 计划书 D3): 一次运行、算 N 遍、下一遍单。
        fail-closed: 任何一步拿不到数据就不动作。"""
        cfg = self._cfg_getter().rotation
        if trading_session(self._clock()) != "continuous":
            self._store.write_audit("rotation_skip", "非连续竞价时段, 跳过调仓", {})
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
        n_tranches = len(self._tranches)
        pool = cfg.etf_ratio * total
        pool_per = pool / n_tranches                 # 份池 (D3f)
        risk_legs = [cfg.cyb_etf] + ([cfg.risk_etf2] if cfg.risk_etf2 else [])
        codes = sorted(self._rotation_codes(cfg))

        # 用 QMT 真实持仓 + can_use 回填 (2026-08-19 修复: 昨尾盘买入 can_use 陈旧)
        qmt_pos0 = {p["code"]: p for p in self._gateway.query_positions()}
        for c in codes:
            p = qmt_pos0.get(c)
            if p is not None:
                self._book.set_can_use(c, int(p.get("can_use", 0) or 0))
        quotes = self._fetch_quotes(codes)

        # 0) 迁移初始化 (D4, 首个执行 pass 一次性): 现有持仓按手轮转分份
        if self._migration_pending:
            self._migrate(qmt_pos0, quotes, migration_closes, cfg,
                          risk_legs)
        # 0.5) 上一轮在途卖单核销 (自愈): 委托隔夜成交/废单后 QMT 已变、
        #      份簿记还是旧的 —— 只核销「我们自己挂出去的」卖单,
        #      人工买卖不动簿记 (仍走对账告警, 铁律 1)
        self._settle_open_sells()

        # 1) 逐份日频移动止损 (D3b): 持仓判定以 lots 份内为准
        #    (QMT 账户级只剩对账 + can_use 两个用途)
        stop_off: dict[int, str] = {}
        for i in range(n_tranches):
            held = [c for (j, c), v in self._lots.items()
                    if j == i and c in risk_legs and v["qty"] > 0]
            if len(held) != 1:
                continue
            h = held[0]
            q = quotes.get(h)
            last = q.get("last") if q else None
            if not (last and last > 0):
                continue
            lot = self._lots[(i, h)]
            lot["entry_high"] = max(lot.get("entry_high") or 0.0, last)
            if last < lot["entry_high"] * (1.0 - cfg.trailing_stop_pct):
                stop_off[i] = h
                self._store.write_audit(
                    "rotation_stop",
                    f"份{i + 1}/{n_tranches} 日频移动止损触发, 切避险篮子",
                    {"tranche": i, "code": h,
                     "entry_high": lot["entry_high"]})

        # 2) 逐份定目标 (止损优先; 信号日择腿; 否则维持; 首信号未算有持仓
        #    → 维持持仓腿只守止损 (D6); 无持仓 → skip 等信号日)
        targets: dict[int, object] = {}     # i → 腿代码 | None(避险) | "skip"
        decisions: dict[int, str] = {}
        for i in range(n_tranches):
            t = self._tranches[i]
            sig = signals.get(i)
            if i in stop_off:
                targets[i] = None
                hi = self._lots[(i, stop_off[i])]["entry_high"]
                decisions[i] = (f"日频移动止损: {stop_off[i]} 从持仓期最高 "
                                f"{hi:.3f} 回撤超 "
                                f"{cfg.trailing_stop_pct * 100:.0f}% → 切避险黄金")
            elif sig is not None and not sig.get("insufficient"):
                targets[i] = sig.get("target")
                t["has_target"] = True
                decisions[i] = self._pick_decision(sig, targets[i])
            elif sig is not None and sig.get("insufficient"):
                # 数据不足 fail-safe (逐份隔离, 不拖其他份): 维持现状
                targets[i] = self._maintain_target(i, risk_legs)
                decisions[i] = (f"数据不足 fail-safe ({sig.get('reason', '')})"
                                f" → 维持现状")
            elif t["has_target"]:
                targets[i] = t["pending_target"]
                decisions[i] = self._pick_decision(None, targets[i])
            else:
                held = [c for (j, c), v in self._lots.items()
                        if j == i and v["qty"] > 0]
                if held:
                    # D6: 首信号未算但有持仓 (迁移/状态丢失) → 维持, 只守止损。
                    # 持多码时优先风险腿 (换档残留: 黄金+风险腿同在, 别误选
                    # 黄金把好的风险腿卖掉 —— 首轮自审发现)
                    held_risk = [c for c in held if c in risk_legs]
                    targets[i] = held_risk[0] if held_risk else None
                    decisions[i] = "首信号未算, 维持持仓待信号日 (止损照常)"
                else:
                    targets[i] = "skip"
                    decisions[i] = "首个周频信号未算, 等信号日"

        def _mk_legs(tcode):
            return momentum_target_values(cfg.cyb_etf, cfg.risk_etf2,
                                          cfg.gold_etf, cfg.hedge_etf2,
                                          cfg.hedge_ratio, tcode, pool_per)

        # 3) 卖 (D3d): 某份对某代码目标为 0 → 卖该份 (分单不并单,
        #    成交回报按 order_id 归份无歧义); 目标腿超配绝不主动削 (模型B)
        self._executor.clear_external_sells()   # D3.3: 每次运行仅开头一次
        can_use_rem = {c: int((qmt_pos0.get(c) or {}).get("can_use", 0) or 0)
                       for c in codes}
        sell_ids: list[str] = []
        sell_lots: dict[str, tuple] = {}        # order_id → (i, code, qty)
        for i in range(n_tranches):
            if targets[i] == "skip":
                continue
            legs_map = dict(_mk_legs(targets[i]))
            for (j, code), lot in sorted(self._lots.items()):
                if j != i or lot["qty"] <= 0:
                    continue
                if legs_map.get(code, 0.0) > 0:
                    continue            # 目标腿: 超配漂移接受, 不卖
                qty = min(lot["qty"], can_use_rem.get(code, 0))
                if qty <= 0:
                    continue
                oid = self._sell_qty(code, qty, quotes.get(code),
                                     decisions[i])
                if oid:
                    sell_ids.append(oid)
                    sell_lots[oid] = (i, code, qty)
                    can_use_rem[code] = can_use_rem.get(code, 0) - qty
        self._wait_fills(sell_ids)
        # 成交确认后减份簿记 (只认 explicit filled_qty>0; 废单 filled=0 不动 —
        # 否则废单会被 `or qty` 误核销, 首轮自审发现); 未到终态/无成交量
        # 回报的 → 记在途台账, 下一轮开场核销
        open_sells: dict = {}
        if sell_lots:
            try:
                ods = {o["order_id"]: o
                       for o in self._gateway.query_orders()}
                for oid, (i, code, qty) in sell_lots.items():
                    o = ods.get(oid) or {}
                    fq = o.get("filled_qty")
                    if o.get("status") in TERMINAL_STATUSES and fq:
                        lot = self._lots.get((i, code))
                        if lot:
                            lot["qty"] = max(0, lot["qty"]
                                             - min(int(fq), qty))
                    else:
                        open_sells[oid] = [i, code, qty]
            except Exception as e:
                _logger.warning("卖单终态回查失败 (簿记维持, 对账兜底): %s", e)
                open_sells = {oid: list(v) for oid, v in sell_lots.items()}
        self._save_open_sells(open_sells)

        # 4) 卖后重读 QMT 持仓/现金 → 逐份买 (D3f: 份预算帽 + 现金按份序分配)
        qmt_pos = {p["code"]: p for p in self._gateway.query_positions()}
        try:
            cash = float(self._gateway.query_asset().get("cash", 0.0) or 0.0)
        except Exception:
            cash = 0.0
        # 全局池级预算帽 (审计 CRITICAL#2, 口径 = Σ份帽, 防超买)
        all_val = sum(self._lot_value(c, v["qty"])
                      for (_i, c), v in self._lots.items())
        cash = min(cash, max(0.0, pool - all_val))
        for i in range(n_tranches):
            if targets[i] == "skip" or cash <= 0:
                continue
            tranche_val = sum(
                self._lot_value(c, v["qty"])
                for (j, c), v in self._lots.items() if j == i)
            cap_rem = max(0.0, pool_per - tranche_val)   # 份预算帽 (逐腿递减)
            if cap_rem <= 0:
                continue
            for code, tval in _mk_legs(targets[i]):
                if tval <= 0 or cash <= 0 or cap_rem <= 0:
                    continue
                cur = self._lot_value(
                    code, self._lots.get((i, code), {}).get("qty", 0))
                spent = self._buy_lot(i, code, max(0.0, tval - cur),
                                      min(cap_rem, cash), quotes.get(code),
                                      decisions[i], code in risk_legs)
                cash -= spent
                cap_rem -= spent

        # 5) 收尾: 逐份 pending_target/entry_high 对齐 + 簿记落库 +
        #    对账告警 + 审计。entry_high 随 lots 行生灭 (切腿清旧自然达成)。
        now = datetime.fromtimestamp(self._clock())
        for i in range(n_tranches):
            if targets[i] == "skip":
                self._store.write_audit(
                    "rotation_skip",
                    f"份{i + 1}/{n_tranches}: {decisions[i]}",
                    {"tranche": i, "anchor": self._tranches[i]["anchor"]})
                continue
            t = self._tranches[i]
            t["pending_target"] = (targets[i]
                                   if targets[i] in risk_legs else None)
            t["has_target"] = True
            # 每份 last_signal: 信号日=动量信号, 否则=维持/止损注记
            sig = signals.get(i) or {}
            t["last_signal"] = dict(sig) if sig else {
                "target": targets[i] if targets[i] in risk_legs else None,
                "insufficient": False,
                "date": now.strftime("%Y%m%d"),
                "note": "日频执行 (当前生效 target + 移动止损)"}
            t["last_signal"]["decision"] = decisions[i]
            leg_txt = " / ".join(
                f"{_etf_label(c)}: "
                f"{self._lot_value(c, self._lots.get((i, c), {}).get('qty', 0)):,.0f}元"
                for c in codes)
            self._store.write_audit(
                "rotation_summary",
                f"ETF轮动·份{i + 1}/{n_tranches}({_WD_CN.get(t['anchor'], t['anchor'])}): "
                f"{decisions[i]}; 份池 {pool_per:,.0f}元"
                f"(≈总资产×{cfg.etf_ratio:.0%}÷{n_tranches}); 各腿现值: {leg_txt}",
                {"tranche": i, "anchor": t["anchor"],
                 "target": targets[i] if targets[i] in risk_legs else None,
                 "pool": round(pool_per, 2), "decision": decisions[i],
                 "momentum": (signals.get(i) or {}).get("momentum"),
                 "stop_off": i in stop_off, "stop_leg": stop_off.get(i),
                 "signal": t["last_signal"]})
        self._persist_lots()
        self._reconcile_lots(qmt_pos, codes)

    def _pick_decision(self, sig: dict | None, target) -> str:
        """择腿/避险决策人话 (含动量数据 + 判断依据), 供 trades.reason 与审计。"""
        mom = _fmt_momentum((sig or {}).get("momentum"))
        if target is None:
            return (f"两腿动量均≤0 ({mom}) → 空仓买黄金" if mom
                    else "避险篮子 (当前生效目标)")
        return (f"动量择腿 ({mom}) → 目标 {_etf_label(target)}" if mom
                else f"目标 {_etf_label(target)} (当前生效目标)")

    def _maintain_target(self, i: int, risk_legs: list):
        """维持目标 = 当前生效 pending_target; 无则按份内持仓腿 (D6);
        无目标且无持仓 → "skip" (数据不足 fail-safe 时空仓不动作,
        绝不把「没信号」误解成「切黄金」)。"""
        t = self._tranches[i]
        if t["has_target"]:
            return t["pending_target"]
        held = [c for (j, c), v in self._lots.items()
                if j == i and v["qty"] > 0]
        if held:
            return held[0] if held[0] in risk_legs else None
        return "skip"

    # ── 迁移初始化 (计划书 D4) ───────────────────────────────────

    def _migrate(self, qmt_pos: dict, quotes: dict,
                 migration_closes: dict, cfg, risk_legs: list) -> None:
        """首个执行 pass 一次性: QMT 现有轮动池持仓按手 (100 份) 轮转分份,
        零头归尾份; entry_high 继承旧 rotation_state → 近 20 日最高收盘 →
        迁移日实时价 (宁紧勿松); 完成打「已初始化」标志 + 写审计。"""
        n = len(self._tranches)
        try:
            legacy = self._store.rotation_signal.load() or {}
            legacy_eh = ((legacy.get("signal") or {}).get("entry_high")
                         or {})
        except Exception:
            legacy_eh = {}
        detail: dict = {}
        for code in sorted(self._rotation_codes(cfg)):
            p = qmt_pos.get(code)
            vol = int((p or {}).get("volume", 0) or 0)
            if vol <= 0:
                continue
            if code in risk_legs:
                eh = legacy_eh.get(code)
                if not eh and migration_closes.get(code):
                    eh = max(migration_closes[code])
                if not eh:
                    q = quotes.get(code) or {}
                    eh = float(q.get("last") or 0.0)
            else:
                eh = 0.0            # 避险腿不需要移动止损基准
            hands, rem = divmod(vol, _LOT)
            for h in range(hands):
                i = h % n
                lot = self._lots.setdefault(
                    (i, code), {"qty": 0, "entry_high": 0.0})
                lot["qty"] += _LOT
                lot["entry_high"] = eh or lot["entry_high"]
            if rem:                 # 碎股零头归尾份 (全清时允许卖非整手)
                lot = self._lots.setdefault(
                    (n - 1, code), {"qty": 0, "entry_high": 0.0})
                lot["qty"] += rem
                lot["entry_high"] = eh or lot["entry_high"]
            detail[code] = {"volume": vol, "entry_high": eh}
        # 份目标派生: 持风险腿 → pending=该腿; 仅持避险 → pending=None;
        # 全空 → has_target=False 等信号日 (staggered 建仓, 不首日扎堆)
        for i, t in enumerate(self._tranches):
            held = [c for (j, c), v in self._lots.items()
                    if j == i and v["qty"] > 0]
            held_risk = [c for c in held if c in risk_legs]
            if held_risk:
                t["pending_target"] = held_risk[0]
                t["has_target"] = True
            elif held:
                t["pending_target"] = None
                t["has_target"] = True
        try:
            self._store.rotation_meta.set("lots_initialized", "1")
        except Exception:
            _logger.warning("迁移标志落库失败 (下次执行会重试)")
        self._migration_pending = False
        self._persist_lots()
        self._store.write_audit(
            "rotation_migrate",
            f"轮动迁移初始化: 现有持仓按手轮转分 {n} 份 "
            f"(明细: {detail or '无持仓, 空仓起步'})",
            {"tranches": n, "detail": detail})

    def _settle_open_sells(self) -> None:
        """开场核销上一轮在途卖单 (自愈, 2026-09-16 错峰簿记配套)。

        只核销「我们自己挂出去的」卖单 (order_id 台账存 rotation_meta);
        人工买卖不动簿记 (仍走 _reconcile_lots 对账告警)。核销口径:
        - 终态: 按已成交量减簿记 (废单 filled=0 → 不动, 份额回来簿记仍在);
        - 查不到 (隔夜委托 QMT 不再返回): 按已成交核销 —— ETF 限价@买一
          隔夜未成交极罕见; 若实际废单, 差额由对账告警兜给人看 (方向可见);
        - 仍在途: 保留台账, 簿记不动 (份额还冻在挂单里)。
        """
        import json
        try:
            raw = self._store.rotation_meta.get("rotation_open_sells")
            pending = json.loads(raw) if raw else {}
            if not isinstance(pending, dict) or not pending:
                return
        except Exception:
            return
        try:
            ods = {o["order_id"]: o for o in self._gateway.query_orders()}
        except Exception:
            ods = {}
        remaining: dict = {}
        for oid, rec in pending.items():
            try:
                i, code, qty = int(rec[0]), str(rec[1]), int(rec[2])
            except (TypeError, ValueError, IndexError):
                continue
            o = ods.get(oid)
            if o is None:
                self._reduce_lot(i, code, qty)          # 隔夜查不到 → 按成交核销
            elif o.get("status") in TERMINAL_STATUSES:
                filled = int(o.get("filled_qty") or 0)
                if filled > 0:
                    self._reduce_lot(i, code, min(filled, qty))
            else:
                remaining[oid] = [i, code, qty]         # 仍在途 → 留台账
        self._save_open_sells(remaining, merge=False)

    def _reduce_lot(self, tranche: int, code: str, qty: int) -> None:
        lot = self._lots.get((tranche, code))
        if lot:
            lot["qty"] = max(0, lot["qty"] - qty)

    def _save_open_sells(self, updates: dict, merge: bool = True) -> None:
        """在途卖单台账落盘 (rotation_meta JSON)。merge=True 与已有台账合并
        (本轮新挂单 + 上轮仍在途); False = 整表替换 (核销后回写)。"""
        import json
        try:
            if merge:
                raw = self._store.rotation_meta.get("rotation_open_sells")
                cur = json.loads(raw) if raw else {}
                if not isinstance(cur, dict):
                    cur = {}
                cur.update(updates)
                updates = cur
            self._store.rotation_meta.set(
                "rotation_open_sells", json.dumps(updates))
        except Exception:
            _logger.warning("在途卖单台账落库失败 (对账告警兜底)")

    def _persist_lots(self) -> None:
        """份簿记整表落库 (消费者线程唯一写; qty≤0 的行即删除)。"""
        try:
            self._store.rotation_lots.replace_all(self._lots)
        except Exception:
            _logger.warning("轮动份簿记落库异常 (内存态继续, 对账兜底)")

    def _reconcile_lots(self, qmt_pos: dict, codes: list) -> None:
        """份簿记 vs QMT 账户级持仓对账: 不一致只告警不改账 (铁律 1)。"""
        try:
            drift = {}
            for c in codes:
                lot_qty = sum(v["qty"] for (j, cc), v in self._lots.items()
                              if cc == c)
                qmt_qty = int((qmt_pos.get(c) or {}).get("volume", 0) or 0)
                if lot_qty != qmt_qty:
                    drift[c] = {"lots": lot_qty, "qmt": qmt_qty}
            if drift:
                self._store.write_audit(
                    "rotation_lots_drift",
                    f"轮动份簿记与 QMT 持仓不一致 (只告警不改账): {drift}; "
                    "簿记可能因买侧废单/部成失真 (买入为「委托发出即全额"
                    "记份簿记」, 无自动核销路径), 幻影持仓会压制后续补仓 —— "
                    "请人工核对 QMT 实际持仓; 确认失真后的人工修账入口: "
                    "直接改 SQLite 库 rotation_lots 表, 或清除 "
                    "rotation_meta 的 lots_initialized 标志后重启 "
                    "(触发迁移按 QMT 持仓重建簿记)",
                    drift)
        except Exception:
            _logger.debug("份簿记对账异常 (不影响交易)")

    # ── 内部: 下单与行情 ────────────────────────────────────────

    def _lot_value(self, code: str, qty: int) -> float:
        """份内持仓市值 = 份内股数 × 最新价; 无行情回退成本价。
        口径单一真相源 trade/pool_money.position_value (治理III W2-1)。"""
        return pool_money.position_value(
            code, qty, self._monitor.quote_of,
            self._book.snapshot()["positions"])

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

    def _sell_qty(self, code: str, qty: int, quote: dict | None,
                  decision: str) -> str | None:
        """卖指定数量 (份内目标为 0 的腿: 换档卖旧腿 / 止损清风险腿;
        全清允许残余非整手)。模型B: 目标腿绝不主动削。返回 order_id。"""
        if qty <= 0:
            return None
        price = self._side_price(quote, sell=True)
        if not price:
            self._store.write_audit(
                "rotation_fail_closed", f"{code} 无买一价, 本轮不卖", {"code": code})
            return None
        return self._place_order(code, DIRECTION_SELL, price, qty, decision)

    def _buy_lot(self, tranche: int, code: str, gap: float, budget: float,
                 quote: dict | None, decision: str,
                 is_risk_leg: bool) -> float:
        """按缺口买入 (gap=目标市值−份内现值, budget=min(份预算帽余量, 现金));
        成交委托发出即记份簿记 (买入风险腿 entry_high=买价, 同腿补仓保最高)。
        返回实际花掉的金额 (未买/被风控拒 = 0)。"""
        if gap <= 0 or budget <= 0:
            return 0.0
        price = self._side_price(quote, sell=False)
        if not price:
            self._store.write_audit(
                "rotation_fail_closed", f"{code} 无卖一价, 本轮不买", {"code": code})
            return 0.0
        spend_cap = min(gap, budget)
        # 注 (审计 L9): int() 向下取整到 100 份, 每单留 <1 手现金缓冲覆盖
        # ETF 免印花税的微小佣金, 不会超支; 未显式预留费用/滑点, 若券商有
        # 最低佣金需后续核对 (2026-09-16 错峰: 逐份分单, 最低佣金影响 ×N, 见计划书 §六)。
        qty = int(spend_cap / price / _LOT) * _LOT
        if qty < _LOT:
            return 0.0
        if not self._place_order(code, DIRECTION_BUY, price, qty, decision):
            return 0.0
        lot = self._lots.setdefault((tranche, code),
                                    {"qty": 0, "entry_high": 0.0})
        lot["qty"] += qty
        if is_risk_leg:
            lot["entry_high"] = max(lot.get("entry_high") or 0.0, price)
        return round(price * qty, 2)

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
                     decision: str) -> str | None:
        """过风控 → 下单 → 入账 (book/store) → audit。轮动买入 rotation=True
        绕过单笔金额/持仓数上限; 急停/对账/日亏/T+1 四道闸照常。
        decision = 本次调仓决策原因 (含动量数据), 进 trades.reason 与审计。
        返回 order_id (None=风控拒)。下单七步脊柱走唯一下单口
        Executor.place_order (计划书 T3); 卖单登记尾巴留在本模块。"""
        price = round_price_etf(price)
        label = "ETF轮动买入" if direction == DIRECTION_BUY else "ETF轮动卖出"
        order_id, why = self._executor.place_order(PlaceRequest(
            code=code, direction=direction, price=price, qty=qty,
            intent_flags={"rotation": True}, remark_prefix="R",
            fill_payload={"label": label, "detail": f"{label}: {decision}"},
            audit_kind="rotation_order",
            audit_message=f"{label} {code} {qty}@{price} ({decision})",
            audit_extra={"code": code, "qty": qty, "price": price,
                         "order_id": None, "decision": decision}),
            risk_ctx=self._build_ctx())
        if order_id is None:
            self._store.write_audit(
                "rotation_risk_reject",
                f"{code} {'买' if direction == DIRECTION_BUY else '卖'}被风控拒: {why}",
                {"code": code, "qty": qty, "price": price, "reason": why,
                 "decision": decision})
            return None
        if direction == DIRECTION_SELL:
            # 治理III W2-5: 卖单登记借 executor 共享槽 (对账 in_flight 单源读)
            self._executor.register_external_sell(code, order_id, qty)
        return order_id
