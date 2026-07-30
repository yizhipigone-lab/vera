"""trade/reconciler.py — 三方对账 (计划书 §5.3)。

设计意图:
    A = QMT 实时 (唯一真相源) vs B = 本地账本 vs C = 成交还原
    (昨仓快照 + 当日成交净额)。偏差分级 NONE/WARN/CRITICAL,
    CRITICAL 直接 kill switch;所有结果只写 reconcile_log,
    永不回写 book/store 持仓 —— 对账只告警+熔断, 自动改账会把
    "券商是对的"这个锚也弄丢 (铁律 1)。
    公开接口刻意只有 reconcile() 一个方法。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from trade.book import DIRECTION_BUY
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
        self._in_flight = in_flight_sells or (lambda: {})
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
        adopted = self._adopt_manual_trades(now)
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

    def _adopt_manual_trades(self, now: float) -> int:
        """手工成交认领 (2026-07-27 ETF 误卖事件裁决④): 用户在券商
        客户端手工下的单没有回报流进本系统, A(QMT)≠B(本地账本) 的
        差异先尝试用当日成交记录解释 —— 本地没见过的成交按 QMT 记录
        补记进账本 (strategy 标"手工") + 落库 + audit, 返回认领笔数。

        分寸 (注释即契约): 认领是把券商已证明的事实补记进账本
        (QMT→本地单向), 不是按差异改账 —— 不违反"对账永不回写"
        铁律 (那禁止的是按 A/B 差额直接改持仓数字)。幂等:
        traded_id 判重 (store 当日成交 + book 幂等集合双保险)。"""
        today = datetime.fromtimestamp(now).strftime("%Y%m%d")
        known = self._store.load_today_trade_ids(today)
        adopted = 0
        for t in self._gateway.query_trades():
            tid = str(t.get("traded_id", ""))
            if not tid or tid in known:
                continue
            ok = self._book.apply_trade(
                tid, str(t.get("order_id", "")), t["code"],
                t["direction"], float(t["price"]), int(t["qty"]),
                strategy="手工")
            if not ok:
                continue  # book 幂等集合已见过 (双保险), 不重复记账
            try:
                self._store.save_trade({
                    "traded_id": tid, "order_id": str(t.get("order_id", "")),
                    "code": t["code"], "direction": t["direction"],
                    "price": float(t["price"]), "qty": int(t["qty"]),
                    "ts": t.get("ts", now)})
            except Exception:
                pass  # 唯一约束兜底: 已落库视为已认领
            self._store.write_audit(
                "manual_adopt",
                f"手工成交认领: {t['code']} "
                f"{'买' if t['direction'] == DIRECTION_BUY else '卖'} "
                f"{t['qty']}@{t['price']}",
                {"code": t["code"], "qty": t["qty"], "price": t["price"],
                 "traded_id": tid, "strategy": "手工"})
            adopted += 1
        return adopted

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
