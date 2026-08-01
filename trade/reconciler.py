"""trade/reconciler.py — 三方对账 (计划书 §5.3)。

设计意图:
    A = QMT 实时 (唯一真相源) vs B = 本地账本 vs C = 成交还原
    (昨仓快照 + 当日成交净额)。偏差分级 NONE/WARN/CRITICAL,
    CRITICAL 直接 kill switch;所有结果只写 reconcile_log,
    永不回写 book/store 持仓 —— 对账只告警+熔断, 自动改账会把
    "券商是对的"这个锚也弄丢 (铁律 1)。
    公开接口两个: reconcile() (三方对账) 与 sync_reports()
    (2026-07-30 增量同步: QMT 成交/委托单向补记回写本地,
    是"认领"语义的推广, 同样不按差额改账, 不违反铁律 1)。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from trade.book import DIRECTION_BUY, TERMINAL_STATUSES
from utils.logger import get_logger

_logger = get_logger("trade.reconciler")

LEVEL_NONE = "NONE"
LEVEL_WARN = "WARN"
LEVEL_CRITICAL = "CRITICAL"
# P0-③ 实测 (2026-07-26): 断线后 query_positions 不抛异常, 静默返回
# 空列表 —— 空返回 ≠ 持仓清零。查询不可用的本轮结论记 UNKNOWN:
# 不触发急停、不影响 reconciled (与真 CRITICAL 区分)
LEVEL_UNKNOWN = "UNKNOWN"

_SEVERITY = {LEVEL_NONE: 0, LEVEL_WARN: 1, LEVEL_CRITICAL: 2}


@dataclass(frozen=True)
class ReconcileDiff:
    """单票差异记录。price_source 标注估值口径 —— 无价时用成本兜底
    必须在报告里留痕, 否则事后复盘会误读门限判定。"""

    code: str
    level: str
    expected: int          # B 本地账本持仓
    actual: int            # A QMT 实时持仓
    restored: int | None   # C 成交还原持仓 (无昨仓快照则 None)
    price_source: str      # "quote" | "cost"
    reason: str


@dataclass(frozen=True)
class ReconcileReport:
    """对账报告。passed = 无 CRITICAL, 供启动流程放行判断。
    adopted: 本轮手工认领笔数 (2026-07-27 裁决④)。"""

    level: str
    diffs: tuple[ReconcileDiff, ...]
    ts: float
    adopted: int = 0

    @property
    def passed(self) -> bool:
        return self.level != LEVEL_CRITICAL


class Reconciler:
    """三方对账。所有外部依赖构造函数注入, 不 import 任何单例。"""

    def __init__(
        self,
        gateway,
        book,
        store,
        kill_switch,
        quote_price: Callable[[str], float | None] | None = None,
        in_flight_sells: Callable[[], dict[str, int]] | None = None,
        pop_fill_context: Callable[[str], dict] | None = None,
        reason_from_ctx: Callable[[dict], str] | None = None,
        on_adopted_trade: Callable[[dict, dict, float | None], None] | None = None,
        on_order_terminal: Callable[[str], None] | None = None,
        query_retries: int = 3,
        retry_interval_sec: float = 1.0,
    ):
        self._gateway = gateway
        self._book = book
        self._store = store
        self._kill = kill_switch
        # 最近行情快照价 (monitor 的 quote 缓存); 无价时成本价兜底
        self._quote_price = quote_price or (lambda code: None)
        # 在途卖单数量 (executor 提供), 用于差异降级:
        # 卖出已报未成的部分, A 已冻结但 B 未扣 —— 这种差异是流水不是错账
        self._in_flight = in_flight_sells or (dict)
        # 成交原因 (2026-07-31): 回调丢失走补记时 ctx 还在 executor,
        # peek 读取 (不删) 既落 reason 又给飞书通知; 部成多笔共享同一
        # 份原因, 订单终态时经 on_order_terminal 回收
        self._pop_ctx = pop_fill_context or (lambda order_id: {})
        self._reason_of = reason_from_ctx or (lambda ctx: "")
        # 补记成交回调 (trade_dict, ctx, avg_cost) → 飞书通知 (2026-07-31)
        self._on_adopted_trade = on_adopted_trade
        # 订单终态回调 (executor 清 fill ctx, peek 语义的配套回收)
        self._on_order_terminal = on_order_terminal
        # 空查询重试参数 (P0-③): 测试注 0 间隔, 生产 3 次 × 1s
        self._query_retries = query_retries
        self._retry_interval = retry_interval_sec

    def reconcile(self, now_ts: float | None = None) -> ReconcileReport:
        """执行一次三方对账。时机: 启动 / 重连后 / 每日 4 时点 / 收盘。"""
        now = now_ts if now_ts is not None else time.time()

        book_pos = self._book.snapshot()["positions"]
        actual_list = self._query_positions_with_retry(book_pos)
        if actual_list is None:
            # P0-③ 实测: 断线时空返回 ≠ 持仓清零。判"查询不可用":
            # 本轮 UNKNOWN, 不急停、不动 reconciled (由调用方维持现状)
            self._store.write_reconcile(
                LEVEL_UNKNOWN, "", "", "",
                {"reason": "query_unavailable: 本地有持仓但连续 "
                           f"{self._query_retries} 次查询返回空"})
            self._store.write_audit(
                "reconcile_unknown",
                "对账查询不可用 (疑似断线), 本轮 UNKNOWN 不急停", {})
            return ReconcileReport(level=LEVEL_UNKNOWN, diffs=(), ts=now)
        actual = {p["code"]: p for p in actual_list}
        # 2026-07-27 ETF 误卖事件裁决④: 先认领手工成交再算差异
        # 2026-07-30: 认领升级为 sync_reports (成交补记 + 委托状态回写)
        adopted = self.sync_reports(now)["adopted"]
        restored = self._restore_positions(now)
        in_flight = self._in_flight()
        # 认领可能改账 (手工成交补记), 差异比对用认领后的最新账本
        book_pos = self._book.snapshot()["positions"]

        diffs: list[ReconcileDiff] = []
        for code in sorted(set(actual) | set(book_pos)):
            av = actual.get(code, {}).get("volume", 0)
            bp = book_pos.get(code)
            bv = bp.volume if bp else 0
            if av == bv:
                continue

            cost = (bp.avg_cost if bp and bp.avg_cost > 0
                    else actual.get(code, {}).get("avg_cost", 0.0))
            quote = self._quote_price(code)
            price, price_source = (quote, "quote") if quote else (cost, "cost")
            # 门限: max(100 股, 市值×0.5% 折合股数); 无价兜底时按成本估市值
            threshold = max(100, int(max(av, bv) * 0.005))
            diff_qty = abs(bv - av)

            if diff_qty <= threshold:
                level, reason = LEVEL_NONE, "偏差在门限内"
            elif in_flight.get(code, 0) >= diff_qty:
                level, reason = LEVEL_WARN, "差异可由在途卖单解释, 降级观察"
            else:
                level, reason = LEVEL_CRITICAL, "持仓差异超门限且无法在途解释"
            if adopted:
                reason += f" (含手工认领 {adopted} 笔)"

            diff = ReconcileDiff(
                code=code, level=level, expected=bv, actual=av,
                restored=restored.get(code), price_source=price_source,
                reason=reason,
            )
            diffs.append(diff)
            self._store.write_reconcile(
                level, code, str(bv), str(av),
                {"restored": restored.get(code), "threshold": threshold,
                 "price_source": price_source, "reason": reason,
                 "adopted": adopted},
            )

        # B vs C 交叉验证: 本地账本与成交还原对不上, 说明本地账本的
        # 事件流有丢失 —— 至少 WARN, 不因 A==B 就假装没事
        for code in sorted(set(restored) - set(d.code for d in diffs)):
            bp = book_pos.get(code)
            bv = bp.volume if bp else 0
            if restored[code] != bv:
                diffs.append(ReconcileDiff(
                    code=code, level=LEVEL_WARN, expected=bv,
                    actual=actual.get(code, {}).get("volume", 0),
                    restored=restored[code], price_source="cost",
                    reason="本地账本与成交还原不一致, 事件流疑似丢失",
                ))
                self._store.write_reconcile(
                    LEVEL_WARN, code, str(bv), str(restored[code]),
                    {"reason": "B_vs_C_mismatch"},
                )

        overall = LEVEL_NONE
        for d in diffs:
            if _SEVERITY[d.level] > _SEVERITY[overall]:
                overall = d.level

        if overall == LEVEL_NONE and not diffs:
            # 一致也留一行, 证明对账跑过 (静默 ≠ 健康)
            self._store.write_reconcile(
                LEVEL_NONE, "", "", "",
                {"reason": "三方一致" + (f" (含手工认领 {adopted} 笔)"
                                        if adopted else ""),
                 "adopted": adopted})

        if overall == LEVEL_CRITICAL:
            _logger.critical("对账 CRITICAL, 触发急停: %s",
                             [(d.code, d.expected, d.actual) for d in diffs
                              if d.level == LEVEL_CRITICAL])
            self._kill.activate("reconciler")

        return ReconcileReport(level=overall, diffs=tuple(diffs), ts=now,
                               adopted=adopted)

    def sync_reports(self, now: float | None = None) -> dict:
        """增量同步 (2026-07-30): QMT → 本地单向补记成交 + 回写委托状态。

        背景: QMT 回调链实测不可靠 (on_order_status/on_deal_status 可能
        缺失), 纯事件驱动会让本地记录永久停在陈旧状态。本方法是回调的
        主动补偿网 —— 定时 (config.sync_interval_sec) / 对账 / 重连后
        各跑一轮, traded_id/order_id 幂等, 重复跑无副作用。
        两腿各自容错: 一路查询失败不影响另一路, 异常记日志不上抛
        (对账主流程不能被同步腿拖死)。
        返回 {"adopted": 补记成交笔数, "orders_updated": 回写委托笔数}。
        """
        now = now if now is not None else time.time()
        adopted = 0
        orders_updated = 0
        try:
            adopted = self._adopt_manual_trades(now)
        except Exception:
            _logger.exception("成交补记失败 (本轮跳过, 下轮重试)")
        try:
            orders_updated = self._sync_orders()
        except Exception:
            _logger.exception("委托状态回写失败 (本轮跳过, 下轮重试)")
        if adopted or orders_updated:
            self._store.write_audit(
                "sync_reports",
                f"增量同步: 补记成交 {adopted} 笔, 回写委托 {orders_updated} 笔",
                {"adopted": adopted, "orders_updated": orders_updated})
        return {"adopted": adopted, "orders_updated": orders_updated}

    def _adopt_manual_trades(self, now: float) -> int:
        """手工成交认领 (2026-07-27 ETF 误卖事件裁决④): 用户在券商
        客户端手工下的单没有回报流进本系统, A(QMT)≠B(本地账本) 的
        差异先尝试用当日成交记录解释 —— 本地没见过的成交按 QMT 记录
        补记进账本 + 落库 + audit, 返回认领笔数。

        2026-07-30 归因修复: 先按 order_id 查本地订单簿 —— 查得到
        说明是系统自己的单 (成交回调丢失), 继承该单 remark 作策略归属,
        audit 记 trade_backfill; 查不到才是真手工单 (strategy 标"手工",
        audit 记 manual_adopt)。同时以成交为硬事实回写 orders 表进度
        (与 _on_trade 的 update_order_filled 同语义)。

        分寸 (注释即契约): 认领是把券商已证明的事实补记进账本
        (QMT→本地单向), 不是按差异改账 —— 不违反"对账永不回写"
        铁律 (那禁止的是按 A/B 差额直接改持仓数字)。幂等:
        traded_id 判重 (store 当日成交 + book 幂等集合双保险)。
        A3 (2026-08-01): book 幂等命中但 trades 表缺行 (首次
        save_trade 失败的遗留态) 时仍补写 trades 行 —— QMT 为
        真相源, 本方法是回调/落库丢失的统一兜底网。"""
        today = datetime.fromtimestamp(now).strftime("%Y%m%d")
        known = self._store.load_today_trade_ids(today)
        local_orders = self._book.snapshot()["orders"]
        adopted = 0
        for t in self._gateway.query_trades():
            tid = str(t.get("traded_id", ""))
            if not tid or tid in known:
                continue
            order_id = str(t.get("order_id", ""))
            local_order = local_orders.get(order_id)
            # 归因: 本地订单簿查得到 = 系统的单 (回调丢失补记),
            # 继承 remark; 查不到 = 券商客户端手工单
            strategy = (local_order.remark if local_order is not None
                        and local_order.remark else
                        ("系统" if local_order is not None else "手工"))
            # H1 (2026-07-31 审计): apply_trade 清仓会把 avg_cost 清零,
            # 通知盈亏% 要在 apply 前快照成本 (与 _on_trade 实时路径同口径)
            pre_pos = self._book.snapshot()["positions"].get(t["code"])
            pre_avg_cost = pre_pos.avg_cost if pre_pos else 0.0
            ok = self._book.apply_trade(
                tid, order_id, t["code"],
                t["direction"], float(t["price"]), int(t["qty"]),
                strategy=strategy)
            # 2026-07-31: peek 读 fill ctx (不删) —— 回调丢失时 ctx 仍在
            # executor; 部成多笔共享同一份原因, 订单终态由 _sync_orders /
            # _on_order 回收 (手工单无 ctx)
            ctx = (self._pop_ctx(order_id)
                   if local_order is not None else {})
            record = {
                "traded_id": tid, "order_id": order_id,
                "code": t["code"], "direction": t["direction"],
                "price": float(t["price"]), "qty": int(t["qty"]),
                "ts": t.get("ts", now),
                # 2026-07-30: 来源落库 — 系统单回调丢失补记仍是 system,
                # 只有本地查不到 order_id 的才是真手工单 (manual)
                "source": "system" if local_order is not None else "manual",
                "reason": (self._reason_of(ctx)
                           if local_order is not None else "")}
            # 落库一次, 两条路径共用 (唯一约束兜底: 已落库视为已认领);
            # A3 补写分支视落库成败决定是否留痕计数
            saved = True
            try:
                self._store.save_trade(record)
            except Exception:
                saved = False
            if not ok:
                # A3 (2026-08-01 计划书批次1): book 幂等命中 = 实时 _on_trade
                # 已记过账; 能走到这里说明 tid 不在当日 trades 表 (上方 known
                # 已过滤, 查询先行不依赖异常) —— 即"book 有 / trades 表缺"
                # 的遗留态 (首次 save_trade 失败, 如 WAL busy 重试仍败, 重启
                # 后会成双扣洞)。QMT 是真相源, sync_reports 是所有回调/落库
                # 丢失的统一兜底网: 此处主动补写 trades 行闭环。
                # 注意只补 trades 行 —— update_order_filled 是累加语义不可
                # 重放 (_on_trade 已累过), 订单进度由 _sync_orders 腿兜底;
                # 飞书通知实时路径已发过, 不重复打扰。
                if not saved:
                    continue
                self._store.write_audit(
                    "trade_db_backfill",
                    f"成交落库补写(book已记账/trades表缺): {t['code']} "
                    f"{'买' if t['direction'] == DIRECTION_BUY else '卖'} "
                    f"{t['qty']}@{t['price']}",
                    {"code": t["code"], "qty": t["qty"], "price": t["price"],
                     "traded_id": tid, "order_id": order_id,
                     "strategy": strategy})
                adopted += 1
                continue
            try:
                self._store.update_order_filled(order_id, int(t["qty"]))
            except Exception:
                pass  # 本地无此委托 (手工单) 时无行可更新, 正常
            kind = "trade_backfill" if local_order is not None else "manual_adopt"
            self._store.write_audit(
                kind,
                f"{'成交补记(回调丢失)' if local_order is not None else '手工成交认领'}: "
                f"{t['code']} "
                f"{'买' if t['direction'] == DIRECTION_BUY else '卖'} "
                f"{t['qty']}@{t['price']}",
                {"code": t["code"], "qty": t["qty"], "price": t["price"],
                 "traded_id": tid, "order_id": order_id, "strategy": strategy})
            # 2026-07-31: 补记成交同样发飞书 (QMT 成交回调常丢失, 补记是主路径)
            if self._on_adopted_trade:
                try:
                    self._on_adopted_trade(t, ctx, pre_avg_cost)
                except Exception:
                    pass  # 通知失败不影响对账/补记
            adopted += 1
        return adopted

    def _sync_orders(self) -> int:
        """委托状态回写 (2026-07-30): 拉 QMT 当日委托, 与本地订单簿比对,
        状态/已成交量有变化的经状态机校验后回写 book + orders 表。
        本地是终态而 QMT 返回非终态 (查询滞后) 时状态机拒绝, 保本地 —
        成交硬事实优先于查询快照。返回回写笔数。"""
        local_orders = self._book.snapshot()["orders"]
        updated = 0
        for o in self._gateway.query_orders():
            oid = str(o.get("order_id", ""))
            if not oid:
                continue
            local = local_orders.get(oid)
            status = int(o.get("status", 0))
            filled = int(o.get("filled_qty", 0))
            # 2026-07-31: QMT 报终态即回收 fill ctx (peek 语义的配套;
            # discard 幂等)。成交补记在本轮 _adopt 先跑完, 此处清理安全
            if status in TERMINAL_STATUSES and self._on_order_terminal:
                self._on_order_terminal(oid)
            if (local is not None and local.status == status
                    and local.filled_qty == filled):
                continue  # 无变化, 不重写 (3 分钟一轮, 绝大多数走这里)
            if local is not None and local.status in TERMINAL_STATUSES:
                continue  # 本地终态不可回退 (成交硬事实 > 查询快照)
            ok = self._book.apply_order_update(
                oid, status, code=str(o.get("code", "")),
                direction=int(o.get("direction", 0)),
                price=float(o.get("price", 0.0)), qty=int(o.get("qty", 0)),
                filled_qty=filled, remark=str(o.get("remark", "")))
            if not ok:
                continue  # 状态机拒绝 (非法迁移), 记日志由 book 负责
            self._store.save_order({
                "order_id": oid, "remark": str(o.get("remark", "")),
                "code": str(o.get("code", "")),
                "direction": int(o.get("direction", 0)),
                "price": float(o.get("price", 0.0)),
                "qty": int(o.get("qty", 0)), "filled_qty": filled,
                "status": status})
            updated += 1
        return updated

    def _query_positions_with_retry(self, book_pos) -> list[dict] | None:
        """查 QMT 持仓, 带"空结果可疑"重试 (P0-③):
        本地有持仓而查询连续返回空 → 返回 None (查询不可用);
        本地也空仓 → 空结果是真空仓, 正常返回 []。
        连接健康时查询异常上抛 (那是真故障, 不在这里掩)。"""
        local_has_positions = any(p.volume > 0 for p in book_pos.values())
        for attempt in range(self._query_retries):
            result = self._gateway.query_positions()
            if result or not local_has_positions:
                return result
            if attempt < self._query_retries - 1:
                _logger.warning(
                    "本地有持仓但查询返回空 (第 %d/%d 次), %.1fs 后重试",
                    attempt + 1, self._query_retries, self._retry_interval)
                time.sleep(self._retry_interval)
        return None

    def _restore_positions(self, now: float) -> dict[str, int]:
        """C 方: 昨仓快照 + 当日成交净额。无快照的票不出现在 C 方
        (缺基准不猜, 由 A vs B 主比对兜底)。"""
        snapshot = self._store.load_position_snapshot()
        if not snapshot:
            return {}
        day_start = datetime.fromtimestamp(now).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        net = self._store.net_trades_since(day_start)
        return {code: snap["volume"] + net.get(code, 0)
                for code, snap in snapshot.items()}
