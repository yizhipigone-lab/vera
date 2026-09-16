"""trade/executor.py — 预埋单 + 撤单流水线 + 两级价格阶梯 (计划书 §5.1)。

设计意图:
    静态价位规则 (阶梯止盈) → 预埋限价单: 排队时间优先、零监控延迟、
    程序崩溃照常在券商端成交。动态规则触发后走撤单流水线:
    锁 → 撤 → 等 ack → 刷 → 买一价限价卖 → 5s 未成交/≥14:57 升级逃生
    通道 (限价): 尾盘两市限价@跌停, 盘中深市五档即成剩余撤销、沪市
    笼内最凶限价 (买一×98%); 市价类报单被柜台禁用 (63596), 对手最优
    仅为无价格数据时的 fallback。
    买一锚定天然避开 2% 价格笼子;多级追价移 P3 (无滑点数据不猜)。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from trade.book import (
    DIRECTION_SELL,
    OS_REPORTED,
    OS_SUCCEEDED,
    PRICE_TYPE_LIMIT,
    PRICE_TYPE_MARKET_PEER_FIRST,
    PRICE_TYPE_SZ_5LEVEL_CANCEL,
    TERMINAL_STATUSES,
    is_etf,
    round_price,  # 治理III W1-c: 唯一真相源迁 book.py (本模块内部+auto_buy 经此引用)
)
from trade.quote_stale import is_quote_stale
from trade.closing_auction import auction_sell_price  # 收盘竞价限价单一实现 (2026-09-15)
from trade.risk import OrderIntent
from utils.logger import get_logger

_logger = get_logger("trade.executor")

# 撤单 ack 轮询: 2026-08-01 M2 修复 —— 原 2s 实测 87% 超时,
# 按实测分布重定为 5s (覆盖 ~95% 正常 ack 延迟)
_CANCEL_ACK_TIMEOUT_SEC = 5.0
_CANCEL_ACK_POLL_SEC = 0.1
# 卖出单挂出后超过该秒数未成交 → 升级逃生通道 (限价)
_PENDING_FILL_TIMEOUT_SEC = 5.0

# 成交通知用: monitor _evaluate 的 reason 串前缀 → 中文策略名。
# 基础版 (2026-07-31): 不改 monitor, 在下单侧把 reason 翻成中文标签存走。
_REASON_LABELS = {
    "trailing": "移动止盈",
    "cost_stop": "硬止损",
    "time_stop": "时间止损",
    "cond_time": "条件时间止盈",
    "first_day": "首日不达标",
    "ladder_tp": "阶梯止盈",
}


def _label_from_reason(reason: str) -> str:
    """monitor reason 串 → 中文策略名 (飞书成交通知)。未知/手工兜底, 永不空。"""
    if not reason:
        return "系统卖出"
    if "manual_sell" in reason or "人工" in reason:
        return "人工卖出"
    head = reason.split(":", 1)[0].strip()
    return _REASON_LABELS.get(head, "系统卖出")


def _detail_from_reason(reason: str) -> str:
    """monitor reason 串 → 自然语言成交原因全文 (trades.reason,
    成交记录页"原因"列)。monitor 正文 2026-07-31 起已带关键数字
    (峰值/激活线/回撤/阈值), 这里只把英文头换成中文策略名:
    'trailing: 最高 12.00 ...' → '移动止盈: 最高 12.00 ...'。"""
    label = _label_from_reason(reason)
    body = reason.split(":", 1)[1].strip() if reason and ":" in reason else ""
    return f"{label}: {body}" if body else label


# 2026-08-01 P1: limit_ratio 统一到 core/limit_ratio.py
# (全项目唯一真相源, 消除回测/实盘两份实现的 ST 口径漂移风险)
from core.limit_ratio import limit_ratio  # noqa: E402 (re-export for callers)


def _is_sz(code: str) -> bool:
    """深市判定 (2026-07-27 实测驱动, 2026-08-01 审计纠正): 两市尾盘
    14:57-15:00 都是收盘集合竞价, **都不接受市价单** (深市五张"对手
    最优"全废单; 沪市 2018 年起尾盘也是集合竞价, 且市价类报单被柜台
    禁用, 废单码 63596) —— 尾盘价格类型必须市场感知, 两市都走限价。"""
    return code.split(".")[-1].upper() == "SZ"


class ClearLock:
    """清仓锁 (MQ/QP 验证过的设计): key=(env, account, code),
    TTL 兜底 + order_id 双索引反查。

    这是业务防重入锁, 不是并发锁 —— 防的是"监控腿触发中,
    对账/人工命令又来卖同一只票"的双卖, 单写者线程内也有
    跨事件的重入风险。TTL 300s 兜底: 持锁方崩溃不锁死该股。
    rebind 解决"先锁后拿 order_id"时序 (QP H1 教训:
    下单返回 order_id 前锁上没有可反查的键)。
    """

    def __init__(self, env: str, account: str, ttl_sec: float = 300.0,
                 clock: Callable[[], float] = time.time):
        self._env = env
        self._account = account
        self._ttl = ttl_sec
        self._clock = clock
        self._locks: dict[tuple, dict] = {}   # (env, account, code) -> {ts, order_id}
        self._by_order: dict[str, tuple] = {}  # order_id -> key

    def _key(self, code: str) -> tuple:
        return (self._env, self._account, code)

    def acquire(self, code: str) -> bool:
        key = self._key(code)
        cur = self._locks.get(key)
        if cur is not None:
            if self._clock() - cur["ts"] < self._ttl:
                return False
            # TTL 过期: 持锁方疑似死亡, 夺锁并留痕
            _logger.warning("清仓锁 TTL 过期被夺: %s (原 order_id=%s)",
                            key, cur.get("order_id"))
            self._drop(key)
        self._locks[key] = {"ts": self._clock(), "order_id": None}
        return True

    def release(self, code: str) -> None:
        self._drop(self._key(code))

    def release_by_order_id(self, order_id: str) -> None:
        key = self._by_order.get(order_id)
        if key is not None:
            self._drop(key)

    def rebind_order_id(self, code: str, order_id: str) -> None:
        """下单拿到 order_id 后补挂反查索引。
        审计L9修复: rebind 前清掉旧 order_id 映射 —— 同一 (env,account,code)
        换单重挂时, 旧映射不清会条目泄漏, 旧单终态误放新锁。"""
        key = self._key(code)
        if key in self._locks:
            old_oid = self._locks[key].get("order_id")
            if old_oid:
                self._by_order.pop(old_oid, None)
            self._locks[key]["order_id"] = order_id
            self._by_order[order_id] = key

    def is_held(self, code: str) -> bool:
        key = self._key(code)
        cur = self._locks.get(key)
        return cur is not None and self._clock() - cur["ts"] < self._ttl

    def _drop(self, key: tuple) -> None:
        cur = self._locks.pop(key, None)
        if cur and cur.get("order_id"):
            self._by_order.pop(cur["order_id"], None)


class FillContext:
    """成交通知上下文存储 (治理III W4-b 自 Executor 三件套收编)。

    order_id -> {label, tier, sell_ratio, detail...}: 下单时 register,
    成交回报 (部成多笔共享) 由消费方 peek 读取, 订单终态 discard。
    独立小对象 → Executor 公开方法预算释放, 语义与撤单流水线无关。
    单写者线程 (消费者线程) 访问, 无需锁; 满容量丢最老一条。
    """

    _CAP = 1000

    def __init__(self):
        self._ctx: dict[str, dict] = {}

    def register(self, order_id: str, ctx: dict) -> None:
        if order_id not in self._ctx and len(self._ctx) >= self._CAP:
            self._ctx.pop(next(iter(self._ctx)))
        self._ctx[order_id] = ctx

    def peek(self, order_id: str) -> dict | None:
        """读取不删 —— 2026-07-31: 同一订单的部成多笔共享同一份原因
        (金逸影视 1300 股拆 6 笔成交, 只有首笔有原因), 清理责任在订单终态。"""
        return self._ctx.get(order_id)

    def discard(self, order_id: str) -> None:
        self._ctx.pop(order_id, None)


@dataclass(frozen=True)
class PlaceRequest:
    """唯一下单口请求 (计划书 2026-09-05 §3.1): 五路下单的合法差异, 逐字段表达。

    price = 实际委托价 (发单/订单簿/落库, auto_buy 传 order_price ——
    P0-6 口径); risk_price = 风控口径价 (auto_buy 传参考价 —— 现状风控
    先于定价, 金额闸吃参考价, 审计 P1 不得混用), None 时用 price。
    audit_extra 需含 order_id 的路径以 "order_id": None 占位放历史键位,
    place_order 回填 (键序与收口前逐字节一致, 审计 P2)。
    account_immediately=False 现状仅人工买 (回报经事件链入账; 与四路
    立即入账的分叉已登记 CHANGELOG 待单独立项, 不得改默认)。
    """

    code: str
    direction: int
    price: float
    qty: int
    price_type: object = PRICE_TYPE_LIMIT
    risk_price: float | None = None
    intent_flags: dict | None = None   # {"rotation": True} / {"manual": True} / None
    remark_prefix: str = ""            # "L1"/"X"/"R"/"B" (进 V{mmdd}-{seq}{前缀})
    fill_payload: dict | None = None   # fill_ctx.register 的 {label, detail, ...}
    audit_kind: str = ""
    audit_message: str = ""
    audit_extra: dict | None = None
    account_immediately: bool = True


class Executor:
    """卖出执行。只有消费者线程调用, 无并发设计。"""

    def __init__(
        self,
        gateway,
        book,
        store,
        risk_gate,
        config,
        build_risk_ctx: Callable[[], "object"],
        get_quote: Callable[[str], dict | None] | None = None,
        get_prev_close: Callable[[str], float | None] | None = None,
        st_checker: Callable[[str], bool] | None = None,
        env: str = "live",
        clock: Callable[[], float] = time.time,
        cancel_ack_timeout_sec: float = _CANCEL_ACK_TIMEOUT_SEC,
        # 2026-08-01 P0-3: pending 终态为废单/已撤且持仓仍在时回调,
        # Monitor 注入 _triggered.discard 解除当日触发标记
        on_pending_died: Callable[[str], None] | None = None,
    ):
        self._gw = gateway
        self._book = book
        self._store = store
        self._risk = risk_gate
        self._cfg = config
        # 风控上下文由 root 组装 (总资产/基准权益等实时值 executor 不该知道来源)
        self._build_ctx = build_risk_ctx
        self._get_quote = get_quote or (lambda code: None)
        # 昨收: 启动时由 root 注入 xtdata 日线或网关快照; Fake 场景测试注入
        self._prev_close = get_prev_close or (lambda code: None)
        self._st = st_checker or (lambda code: False)
        self._clock = clock
        self._ack_timeout = cancel_ack_timeout_sec
        self._on_pending_died = on_pending_died
        self.lock = ClearLock(env, config.account_id, clock=clock)
        self._pending: dict[str, dict] = {}  # code -> {order_id, ts, qty, reason}
        # 外部卖单登记 (治理III W2-5): rotation 等绕过本类撤单流水线的
        # 直连卖单, 只借用 in_flight 暴露槽, 不进 _pending 操作链
        # (避免被 pending_check 追价/逃生误触发双卖)
        self._external_sells: dict[str, dict] = {}  # code -> {order_id, qty}
        # 成交通知上下文独立小对象 (治理III W4-b): 收走 register/peek/
        # discard 三件套, 释放 Executor 公开方法预算; 上下文与撤单流水线无关
        self.fill_ctx = FillContext()
        # 审计M1修复: remark 序号进程生命周期单调递增, 不再按日/调用方
        # 重置 —— 重置会让预埋/卖出/人工买入同日出重号, "盘后按 remark
        # 对账"的唯一性前提就破了。mmdd 前缀仍保留 (人读友好)。
        self._seq = 0

    def apply(self, cfg) -> None:
        """热更契约 (治理III W2-1): 换配置引用。cfg 用时读属性, 换引用即热。"""
        self._cfg = cfg

    # ── 唯一下单口 (计划书 2026-09-05 唯一下单口收口) ──────────

    def place_order(self, req: PlaceRequest,
                    *, risk_ctx: object | None = None
                    ) -> tuple[str | None, str | None]:
        """下单七步脊柱: 风控→取号→发单→登记成交原因→订单簿→落库→审计。

        五路下单 (预埋/_sell/轮动/自动买/人工买) 的唯一入口 (计划书 §3.2):
        - 风控拒: **静默**返回 (None, why) —— 风控层每道拒绝已自写
          audit (risk.py), 调用方各自留痕 (warning/审计/_skip), 本方法
          不再加一份, 否则预埋/人工买会双倍告警。
        - 风控价口径: OrderIntent 用 req.risk_price (None 时用 req.price);
          发单/订单簿/落库一律 req.price (委托价, P0-6 口径)。
        - account_immediately=False (人工买): 跳过订单簿+落库, 回报经
          事件链自然入账 (分叉已登记 CHANGELOG, 统一前不得改默认)。
        - audit_extra 含 "order_id": None 占位时回填, 键位保持调用方
          构造序 —— 落库 detail_json 键序与收口前逐字节一致。
        - created_ts 显式下单时刻单点收口于此 (2026-08-10 泰山石油:
          order_id 被 QMT 复用时新单不继承旧 created_ts)。
        - 回填可用 (_refresh_can_use) 不在脊柱: executor 内部两路作为
          调用方尾巴自做 (保崩溃窗口与 pending 超时起点语义, 计划书 §五.3)。
        """
        intent = OrderIntent(
            code=req.code, direction=req.direction,
            price=req.risk_price if req.risk_price is not None else req.price,
            qty=req.qty, **(req.intent_flags or {}))
        ok, why = self._risk.check(intent, risk_ctx or self._build_ctx())
        if not ok:
            return None, why
        remark = self._next_remark(req.remark_prefix)
        order_id = self._gw.order(req.code, req.direction, req.price,
                                  req.qty, req.price_type, remark)
        self.fill_ctx.register(order_id, req.fill_payload)
        if req.account_immediately:
            self._book.apply_order_update(
                order_id, OS_REPORTED, code=req.code,
                direction=req.direction, price=req.price, qty=req.qty,
                remark=remark)
            self._store.save_order({
                "order_id": order_id, "remark": remark, "code": req.code,
                "direction": req.direction, "price": req.price,
                "qty": req.qty, "status": OS_REPORTED,
                # 2026-08-10: 显式下单时刻 — order_id 被 QMT 复用时
                # 新单不继承旧 created_ts (泰山石油事件)
                "created_ts": self._clock()})
        extra = dict(req.audit_extra or {})
        if extra.get("order_id", "__missing__") is None:
            extra["order_id"] = order_id
        self._store.write_audit(req.audit_kind, req.audit_message, extra)
        return order_id, None

    # ── 预埋单 ──────────────────────────────────────────────────

    def place_ladder(self, date_str: str) -> list[str]:
        """全档位一次性挂限价卖单 (档位间无依赖, 消灭"成交推进"逻辑)。
        返回挂出的 order_id 列表。date_str 格式 YYYYMMDD (tier 日期维度用)。

        T+1 衔接确认 (2026-07-27 尾盘自动买入): 昨日尾盘买入的票,
        can_use 由本函数开头的 QMT 全量刷新回填 (book 买入当日为 0),
        成本口径 = book.avg_cost (自成交加权自算) —— 本函数按 book 持仓
        全量扫描, 新票明日 09:15 自动纳入预埋, 无需任何特判。

        2026-08-10: 阶梯止盈总开关 enabled=false 时直接返回不挂 ——
        与盘中兜底 (monitor._evaluate.hit_ladder 同判 stop.ladder_tp.enabled)
        两端同步, 关闭即彻底不触发该规则。三条调用路径: 09:15 定时器与
        手动命令均经 EVENT_COMMAND → _dispatch_cmd 直调本函数, 仅靠此入口
        guard 拦; 启动补偿 (_startup_catchup) 另有 enabled 前置判断, 本
        guard 是其兜底。"""
        if not self._cfg.stop.ladder_tp.enabled:
            self._store.write_audit(
                "ladder_skip_disabled", "阶梯止盈已关闭, 不挂预埋单", {})
            return []
        # 2026-08-16 卖出总开关: 关闭时不挂任何预埋单 (不新增卖出委托),
        # 已挂出的预埋单不动。
        if not self._cfg.auto_sell_enabled:
            self._store.write_audit(
                "ladder_skip_disabled", "卖出总开关已关闭, 不挂预埋单", {})
            return []
        self._sync_can_use()
        placed: list[str] = []
        for code, pos in sorted(self._book.snapshot()["positions"].items()):
            if pos.volume <= 0:
                continue
            # 2026-07-27 ETF 误卖事件裁决③: ETF 不纳入自动管理。
            # 每票一条 audit 留痕即可, 不按档刷屏
            if self._cfg.exclude_etf and is_etf(code):
                self._store.write_audit(
                    "ladder_skip_etf", f"{code} 为 ETF, 不纳入自动管理",
                    {"code": code})
                continue
            prev_close = self._prev_close(code)
            if not prev_close:
                # 无昨收无法判涨停, fail-closed: 该票本轮不挂, 宁可漏不可错
                self._store.write_audit(
                    "ladder_skip", f"{code} 无昨收, 本轮不挂预埋单", {"code": code})
                continue
            limit_up = prev_close * (1 + limit_ratio(code, self._st(code)))
            # 审计C1修复: 预埋跳过只认"当日"标记 (date_str 即当日),
            # 昨日标记留痕不阻碍今日重挂 (计划书 §5.1 日终过期次日重挂)
            done = self._book.tier_done(code, date_str)
            remaining = pos.volume
            for tier, (profit, ratio) in enumerate(self._cfg.stop.ladder_tp.levels):
                if tier in done:
                    continue  # 已预埋档不重复挂 (乐观标记在, 防废单重复卖)
                # 审计L10修复: 0.01 对齐用四舍五入 (round_price,
                # 不用 round() 银行家舍入)
                price = round_price(pos.avg_cost * (1 + profit))
                if price > limit_up:
                    # 超涨停价挂了只会废单, 今日跳过该档
                    self._store.write_audit(
                        "ladder_skip", f"{code} 档位{tier} 价 {price} 超涨停 "
                        f"{limit_up:.2f}, 跳过",
                        {"code": code, "tier": tier, "price": price})
                    continue
                # 数量口径: 比例相对原始持仓; ratio≥1.0 = 清仓档, 卖剩余全部。
                # 审计M2修复: 比例档手数四舍五入 int(x+0.5) (0.5 边界向上),
                # 不用 int() 截断 —— 1000×0.29 截断成 2 手静默少卖 90 股。
                # 清仓档仍向下取整: 余 50~99 股时四舍五入会卖出超过持仓的
                # 100 股, 宁留尾数 (<100) 不超卖
                remaining_lots = int(remaining / 100)
                if ratio < 1.0:
                    lots = min(int(pos.volume * ratio / 100 + 0.5), remaining_lots)
                else:
                    lots = remaining_lots
                qty = lots * 100
                if qty <= 0:
                    continue
                # 唯一下单口 (计划书 T2): 七步脊柱收口, 合法差异经 PlaceRequest 表达
                order_id, reason = self.place_order(PlaceRequest(
                    code=code, direction=DIRECTION_SELL, price=price, qty=qty,
                    remark_prefix=f"L{tier}",
                    fill_payload={
                        "label": "阶梯止盈", "tier": tier,
                        "profit": profit, "sell_ratio": ratio,
                        # 2026-07-31: 自然语言成交原因 (成交记录页全文展示)
                        "detail": f"阶梯止盈·档{tier + 1}: 预埋价 {price} "
                                  f"(成本 {pos.avg_cost:.2f} {profit:+.0%}), "
                                  f"卖 {ratio:.0%}"},
                    audit_kind="ladder_place",
                    audit_message=f"{code} 档{tier} 预埋 {qty}@{price}",
                    audit_extra={"code": code, "tier": tier, "order_id": None,
                                 "price": price, "qty": qty}))
                if order_id is None:
                    # 风控拒绝已写 audit (risk 层), 该档今日不挂;
                    # 不占 remaining —— 没挂出去的量不算花掉
                    _logger.warning("预埋单被风控拒绝: %s 档%d %s", code, tier, reason)
                    continue
                remaining -= qty
                # 乐观标记: 提交成功即标记 (QP 做法) —— 废单也不重复卖,
                # 误标漏卖的损失 < 重复卖的损失。审计C1修复: 带当日日期
                self._book.mark_tier(code, tier, date_str)
                self._store.tier_state.save(
                    code, sorted(self._book.tier_done(code, date_str)), date_str)
                # 2026-07-30: 预埋成功后立即从 QMT 回填可用 — 券商挂单即冻结,
                # 页面"可用"应与冻结同步 (原只在成交/撤单后刷新, 显示滞后)。
                # 刷新失败不阻断预埋 (可用由下次对账兜底)。
                try:
                    self._refresh_can_use(code)
                except Exception as e:
                    _logger.warning("预埋后回填可用失败 (下次对账兜底): %s: %s",
                                    code, e)
                placed.append(order_id)
        return placed

    # ── 触发卖出 (撤单流水线) ───────────────────────────────────

    def execute_exit(self, code: str, reason: str, manual: bool = False,
                     qty: int | None = None) -> bool:
        """监控腿触发后的卖出流水线: 锁→撤→等ack→刷→买一价限价卖。
        任何一步拿不到数据都 fail-closed (宁可不卖, 不可瞎卖)。

        qty (2026-07-30, 用户要求): 指定卖出数量 (≤可用), None=卖全部可用。
        指定时校验: >0 且 ≤ 可用, 超可用拒卖 (audit 留痕, 不静默截断)。

        manual=True (人工命令, 2026-07-27 裁决①): 任何时段放行,
        且买一价 ts 缺失/陈旧不拦 —— 人工单本来就是用户当下意图,
        与自动规则"陈旧价宁可不卖"的口径刻意不同 (注释即契约)。"""
        if not self.lock.acquire(code):
            self._store.write_audit(
                "exit_lock_fail", f"{code} 清仓锁被占用, 跳过本次触发 ({reason})",
                {"code": code, "reason": reason})
            return False
        try:
            self._cancel_open_orders(code)
            can_use = self._refresh_can_use(code)
            if can_use <= 0:
                self._store.write_audit(
                    "exit_skip", f"{code} 可用为 0, 无可卖 ({reason})",
                    {"code": code, "reason": reason})
                return False
            if qty is not None and qty > can_use:
                self._store.write_audit(
                    "exit_skip", f"{code} 请求卖 {qty} > 可用 {can_use}, 拒卖 ({reason})",
                    {"code": code, "qty": qty, "can_use": can_use, "reason": reason})
                return False
            sell_qty = qty if qty is not None else can_use
            quote = self._get_quote(code)
            if not quote or not quote.get("bid1"):
                self._store.write_audit(
                    "exit_fail_closed", f"{code} 无买一价, 本轮不卖 ({reason})",
                    {"code": code, "reason": reason})
                return False
            if not manual:
                # 审计M6修复 + 2026-07-27 裁决③: 陈旧/无戳买一价与无价
                # 同等 fail-closed (只约束自动规则; 人工单见 manual 分支)。
                # 判定统一走 trade/quote_stale.py (单一真相源)。2026-08-16
                # fail-closed 修复: 旧实现 ts 键缺席时判"不陈旧"继续卖
                # (fail-open), 现与 monitor/rotation 对齐 —— 无 ts 键、
                # ts 为 None、tick_ts_missing、超时 任一即陈旧, 宁可不卖。
                stale, _reason = is_quote_stale(quote, self._clock(),
                                                self._cfg.quote_stale_sec)
                if stale:
                    self._store.write_audit(
                        "exit_fail_closed",
                        f"{code} 买一价无时间戳或陈旧, 本轮不卖 ({reason})",
                        {"code": code, "reason": reason,
                         "quote_ts": quote.get("ts")})
                    return False
            return self._sell(code, sell_qty, float(quote["bid1"]),
                              PRICE_TYPE_LIMIT, reason, "exit_sell")
        finally:
            # 锁不在 finally 里放: 挂出卖单后锁要留到成交 (防重复触发),
            # 由 pending_check 确认成交后 release_by_order_id
            if code not in self._pending:
                self.lock.release(code)

    def pending_check(self, now_ts: float | None = None,
                      now_hhmm: str | None = None) -> None:
        """未成交检查 (由 monitor 定时扫描驱动): 成交则清锁销登记;
        超 5s 或已过 force_market_after → 撤单升级对手最优。"""
        now = now_ts if now_ts is not None else self._clock()
        for code, p in list(self._pending.items()):
            status = self._order_status(p["order_id"])
            if status is None:
                continue  # 查不到状态, 本轮不动 (行情/连接故障 fail-closed)
            if status in TERMINAL_STATUSES:
                # 2026-08-01 P0-3 (H1): 终态为废单/已撤且持仓仍在时,
                # 通知 Monitor 解除 _triggered —— 该票下轮扫描应重新评估,
                # 否则废单一笔就把当日保护永久锁死。
                # 已成不解除: 持仓已清 (或部成剩余由下一轮重新触发)。
                if self._on_pending_died and status != OS_SUCCEEDED:
                    pos = self._book.snapshot()["positions"].get(code)
                    if pos is not None and pos.volume > 0:
                        self._on_pending_died(code)
                self.lock.release_by_order_id(p["order_id"])
                del self._pending[code]
                continue
            timeout = now - p["ts"] > _PENDING_FILL_TIMEOUT_SEC
            force = (now_hhmm is not None
                     and now_hhmm >= self._cfg.force_market_after)
            if not (timeout or force):
                continue
            self._gw.cancel(p["order_id"])
            self._wait_terminal(p["order_id"])
            can_use = self._refresh_can_use(code)
            if can_use <= 0:
                self.lock.release_by_order_id(p["order_id"])
                del self._pending[code]
                continue
            why = "超时5s" if timeout else f"已过{self._cfg.force_market_after}"
            self._store.write_audit(
                "exit_escalate", f"{code} 卖单未成交 ({why}), 升级逃生通道",
                {"code": code, "old_order_id": p["order_id"], "qty": can_use})
            del self._pending[code]  # 旧登记先销, _sell 成功会重建 + rebind 锁
            # 市场感知逃生通道 (四象限, 2026-08-13 起沪市也全限价):
            #   尾盘 force + .SZ (收盘集合竞价 14:57+): 限价@跌停价。单一价格
            #         撮合, 挂跌停=最大成交优先权, 成交价仍是收盘价, 不吃亏。
            #         无昨收算不出跌停 → fallback 对手最优 (试一下比不发强)。
            #   盘中超时 + .SZ (2026-08-11 002253 事件): 五档即成剩余撤销。
            #         跌停价撞价格笼子 88009 废单 (002253 当天 9.86 买一未成交,
            #         升级挂跌停 8.91 被毙, 历史深市升级单全是跌停价废单);
            #         对手最优是单档 FOK, 盘口量不够整单撤; 五档 IOC 扫买1-买5
            #         尽量成交剩余才撤, 成交概率最高。注: SZ_5LEVEL_CANCEL 实盘
            #         未实测, 上线需小单验证柜台表现。
            #   尾盘 force + .SH (2026-08-13 新增): 与深市完全同口径限价@跌停。
            #         沪市 2018 年起尾盘也是收盘集合竞价 (14:57-15:00 只收
            #         限价单), 且市价类报单被柜台禁用 (废单码 63596, 沪市 7/7
            #         全废); 挂跌停=收盘竞价最大成交优先权, 成交价仍是收盘价,
            #         不吃亏。无昨收 → fallback 对手最优 (同深市)。
            #   盘中超时 + .SH (2026-08-13 新增): 笼子内最凶限价 = 买一价×98%
            #         (主板 2% 价格笼子的卖出下限, 笼内最 aggressive 的合法价;
            #         深市已实测盘中挂跌停撞笼子 88009 废单, 沪市同理不能挂
            #         跌停)。无盘口 quote → fallback 对手最优。
            #   fallback 对手最优仅为无价格数据时的最后手段。
            if force:
                # 沪深尾盘收盘集合竞价只收限价单: 限价@跌停 (单一实现
                # trade/closing_auction.py, 2026-09-15 收口 —— 原深市/沪市
                # 两分支除审计文案外逐字节相同, 且与 auto_buy 买侧为双胞胎)。
                prev_close = self._prev_close(code)
                if prev_close:
                    limit_down = auction_sell_price(code, prev_close,
                                                    self._st(code))
                    ok = self._sell(code, can_use, limit_down, PRICE_TYPE_LIMIT,
                                    p["reason"], "exit_sell_market")
                else:
                    self._store.write_audit(
                        "exit_escalate", f"{code} 无昨收, 跌停价算不出,"
                        " 仍发对手最优", {"code": code})
                    ok = self._sell(code, can_use, 0.0,
                                    PRICE_TYPE_MARKET_PEER_FIRST,
                                    p["reason"], "exit_sell_market")
            elif _is_sz(code):
                ok = self._sell(code, can_use, 0.0, PRICE_TYPE_SZ_5LEVEL_CANCEL,
                                p["reason"], "exit_sell_market")
            else:
                # 沪市盘中超时: 笼子内最凶限价 (买一×98%), 不挂跌停 (撞 2%
                # 价格笼子废单, 深市 88009 已实测)
                quote = self._get_quote(code)
                if quote and quote.get("bid1"):
                    cage_floor = round_price(float(quote["bid1"]) * 0.98)
                    ok = self._sell(code, can_use, cage_floor, PRICE_TYPE_LIMIT,
                                    p["reason"], "exit_sell_market")
                else:
                    self._store.write_audit(
                        "exit_escalate", f"{code} 沪市无盘口, 笼内限价算不出,"
                        " 仍发对手最优", {"code": code})
                    ok = self._sell(code, can_use, 0.0,
                                    PRICE_TYPE_MARKET_PEER_FIRST,
                                    p["reason"], "exit_sell_market")
            if not ok:
                # 升级卖出失败 (如风控拒绝): 放锁, 由下一轮监控重新触发
                self.lock.release(code)

    def register_external_sell(self, code: str, order_id: str, qty: int) -> None:
        """外部卖单登记 (治理III W2-5): 绕过本类流水线的直连卖单 (如轮动)
        挂出时借暴露槽, 让对账 in_flight 降级网覆盖它 —— 单源读, 组合根
        不再手拼两路。不进 _pending (防 pending_check 追价/逃生误触发双卖)。"""
        self._external_sells[code] = {"order_id": order_id, "qty": qty}

    def clear_external_sells(self) -> None:
        """清空外部卖单登记 (轮动每次调仓起手调用, 语义同旧 _pending_sells.clear)。"""
        self._external_sells.clear()

    def in_flight_sells(self) -> dict[str, int]:
        """在途卖单数量 {code: qty} —— 对账差异降级用 (注入 reconciler)。
        含本类 _pending (预埋/监控/逃生链) + 外部登记 (轮动直连卖单,
        审计 M4, 治理III W2-5 单源收口)。"""
        return {code: p["qty"] for code, p in
                {**self._pending, **self._external_sells}.items()}

    # ── 内部 ────────────────────────────────────────────────────

    def _sell(self, code: str, qty: int, price: float, price_type,
              reason: str, audit_kind: str) -> bool:
        """过风控 → 下单 → 登记待查 → 锁 rebind。各级卖出共用。
        下单七步脊柱走唯一下单口 place_order (计划书 T2); _pending/锁/
        回填可用为调用方尾巴 (顺序语义见计划书 §五.3)。"""
        order_id, why = self.place_order(PlaceRequest(
            code=code, direction=DIRECTION_SELL, price=price, qty=qty,
            price_type=price_type, remark_prefix="X",
            fill_payload={"label": _label_from_reason(reason),
                          "detail": _detail_from_reason(reason)},
            audit_kind=audit_kind,
            audit_message=f"{code} 卖出 {qty}@{price or '对手最优'} ({reason})",
            audit_extra={"code": code, "order_id": None, "price": price,
                         "qty": qty, "price_type": str(price_type),
                         "reason": reason}))
        if order_id is None:
            self._store.write_audit(
                "exit_risk_reject", f"{code} 卖出被风控拒绝: {why}",
                {"code": code, "qty": qty, "reason": reason})
            return False
        self._pending[code] = {"order_id": order_id, "ts": self._clock(),
                               "qty": qty, "reason": reason}
        self.lock.rebind_order_id(code, order_id)
        # 2026-07-30: 卖单挂出后立即回填可用 — 券商挂单即冻结,
        # 页面"可用"应与冻结同步 (原只在成交/撤单后刷新, 显示滞后)。
        try:
            self._refresh_can_use(code)
        except Exception as e:
            _logger.warning("卖单后回填可用失败 (下次对账兜底): %s: %s", code, e)
        return True

    def _cancel_open_orders(self, code: str) -> None:
        """撤该股全部在途单, 并等 ack (2s 超时不阻塞, 告警继续)。"""
        for oid, rec in self._book.snapshot()["orders"].items():
            if rec.code == code and rec.status not in TERMINAL_STATUSES:
                self._gw.cancel(oid)
                self._store.write_audit(
                    "exit_cancel", f"{code} 撤在途单 {oid}",
                    {"code": code, "order_id": oid})
                if not self._wait_terminal(oid):
                    self._store.write_audit(
                        "exit_cancel_timeout", f"{code} 撤单 {oid} 2s 无 ack",
                        {"code": code, "order_id": oid})

    def _wait_terminal(self, order_id: str) -> bool:
        """轮询订单终态 (查网关 = 真相源, 不赌本地事件流快慢)。
        deadline 用墙钟 (time.monotonic) 不用注入的业务时钟 —— 等 ack
        是真实世界等待; 用业务时钟在假时钟测试里会死循环 (审计M8
        修复过程中实测踩中)。超时阈值构造注入, 测试可缩短。"""
        deadline = time.monotonic() + self._ack_timeout
        while time.monotonic() < deadline:
            status = self._order_status(order_id)
            if status is not None and status in TERMINAL_STATUSES:
                return True
            time.sleep(_CANCEL_ACK_POLL_SEC)
        return False

    def _order_status(self, order_id: str) -> int | None:
        for o in self._gw.query_orders():
            if o["order_id"] == order_id:
                return o["status"]
        return None

    def _refresh_can_use(self, code: str) -> int:
        """查 QMT 持仓回填可用数量 (撤单解冻后的真相)。"""
        for p in self._gw.query_positions():
            if p["code"] == code:
                self._book.set_can_use(code, p["can_use"])
                return p["can_use"]
        return 0

    def _sync_can_use(self) -> None:
        """预埋前全量刷新可用数量 (2026-08-06 002155.SZ 事件)。

        昨日尾盘买入的票 book.can_use=0 (T+1), 而首次定时对账 (09:35)
        晚于预埋 (09:15), 不刷新则 t1_sellable 闸误拒全部预埋档,
        阶梯止盈退化为监控兜底 (一锅端市价卖, 无档位分批)。
        QMT 是可用数量唯一真相源, 这里只做 book 镜像刷新。
        查询失败/返回空 (P0-4: 断线时 QMT 静默返空) fail-closed:
        留痕后沿用本地旧值 —— 风控宁可拒挂, 方向安全。"""
        try:
            qmt = {p["code"]: p for p in self._gw.query_positions()}
        except Exception as e:
            self._store.write_audit(
                "ladder_sync_fail", f"预埋前刷新可用失败, 沿用本地旧值: {e}",
                {})
            return
        book_codes = list(self._book.snapshot()["positions"])
        if not qmt and book_codes:
            self._store.write_audit(
                "ladder_sync_fail",
                "预埋前刷新可用返回空 (查询不可用), 沿用本地旧值", {})
            return
        for code in book_codes:
            p = qmt.get(code)
            if p is not None:
                self._book.set_can_use(code, p["can_use"])

    def _next_remark(self, rule_code: str) -> str:
        """策略侧单号发号器 (审计M1修复): V{mmdd}-{seq}{规则码} ≤24 字符。
        (2026-09-05 唯一下单口收口: 转私有 —— 外部调用方已全走 place_order)
        seq 进程生命周期单调递增、不重置 —— 预埋/卖出/人工买入共用此
        发号器 (人工买入在 trade_main 也调这里), "盘后按 remark 对账"
        的唯一性才成立。跨日 mmdd 变了 seq 也不回零 (对账按全串匹配,
        不依赖 seq 日内语义)。"""
        self._seq += 1
        mmdd = time.strftime("%m%d", time.localtime(self._clock()))
        return f"V{mmdd}-{self._seq:03d}{rule_code}"[:24]
