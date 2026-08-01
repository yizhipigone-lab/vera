"""trade/book.py — 本地账本 + 订单状态机。

设计意图:
    QMT 是持仓/资产/成交的唯一真相源 (铁律 1), 本地账本只存
    QMT 不知道的事: 策略归属、阶梯止盈档位状态、以及成交回报
    驱动的持仓视图 (监控腿判断用, 等不起 QMT 查询流控)。
    状态机是纯函数, 终态不可逆 —— 废单/已撤/已成之后再来
    任何回报都不许改状态 (防券商重复/乱序回报污染)。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from utils.logger import get_logger

_logger = get_logger("trade.book")

# ═══════════════════════════════════════════════════════════════
# xtquant 官方枚举常量 — 全项目只此一份, Fake 与真网关共用
# ═══════════════════════════════════════════════════════════════

# 订单状态 (xtconstant)
OS_UNREPORTED = 48          # 未报
OS_WAIT_REPORTING = 49      # 待报
OS_REPORTED = 50            # 已报
OS_REPORTED_CANCEL = 51     # 已报待撤
OS_PARTSUCC_CANCEL = 52     # 部成待撤
OS_PART_CANCEL = 53         # 部撤
OS_CANCELED = 54            # 已撤
OS_PART_SUCC = 55           # 部成
OS_SUCCEEDED = 56           # 已成
OS_JUNK = 57                # 废单
OS_UNKNOWN = 255            # 未知

TERMINAL_STATUSES = frozenset({OS_PART_CANCEL, OS_CANCELED, OS_SUCCEEDED, OS_JUNK})

# 方向 (xtconstant)
DIRECTION_BUY = 23
DIRECTION_SELL = 24

# 价格类型 (xtconstant)
PRICE_TYPE_LIMIT = 11       # 限价
PRICE_TYPE_LATEST = 5       # 最新价
# 对手最优 (逃生通道): 真网关在方法内映射到 xtconstant,
# 这里用名字占位 —— 顶层 import xtquant 是铁律禁止的
PRICE_TYPE_MARKET_PEER_FIRST = "MARKET_PEER_FIRST"


def is_etf(code: str) -> bool:
    """场内 ETF 判定 (2026-07-27 ETF 误卖事件裁决③): 沪市 51/56/58、
    深市 15/16/18 前缀 (取 '.' 前段)。覆盖场内基金; LOF 501/508 类
    不在此口径内 (与 VERA universe 配置 default.yaml 的 etf 注释一致)。"""
    num = code.split(".")[0]
    return num.startswith(("51", "56", "58", "15", "16", "18"))


def transition(old: int, new: int) -> bool:
    """订单状态机校验 (纯函数)。

    规则: 终态不可逆 —— 终态之后只接受同态重复回报 (幂等),
    其余一律拒绝; 非终态之间不做顺序限制 (QMT 回报可能跳态,
    48→50→55→56 是常态, 但 50→51→54 省略中间态也合法)。
    """
    if old in TERMINAL_STATUSES:
        return new == old
    return True


@dataclass(frozen=True)
class PositionView:
    """持仓视图 (snapshot 里的不可变形态)。"""

    code: str
    volume: int
    can_use: int
    avg_cost: float
    strategy: str = ""


@dataclass(frozen=True)
class OrderRecord:
    """订单记录 (snapshot 里的不可变形态)。"""

    order_id: str
    code: str
    direction: int
    price: float
    qty: int
    filled_qty: int
    status: int
    remark: str = ""


@dataclass
class _Position:
    """内部可变持仓。avg_cost 由本地成交自算。

    TODO P2: 不含税费/手续费 —— MVP 先通, 成交原始数据已落盘,
    事后可回溯补算 (计划书 §一)。
    """

    volume: int = 0
    can_use: int = 0
    avg_cost: float = 0.0
    strategy: str = ""


class Book:
    """本地账本。只有消费者线程调用写方法, 锁是给 snapshot 读的兜底。"""

    def __init__(self) -> None:
        self._positions: dict[str, _Position] = {}
        self._orders: dict[str, OrderRecord] = {}
        # 审计C1修复: code → trade_date → 已预埋档位集合 (日期维度)
        self._tiers: dict[str, dict[str, set[int]]] = {}
        self._seen_trades: set[str] = set()
        self._lock = threading.Lock()

    def apply_order_update(
        self,
        order_id: str,
        status: int,
        code: str = "",
        direction: int = 0,
        price: float = 0.0,
        qty: int = 0,
        filled_qty: int | None = None,
        remark: str = "",
    ) -> bool:
        """应用委托回报。状态机校验不过 → 记日志并拒绝 (返回 False)。"""
        with self._lock:
            old = self._orders.get(order_id)
            if old is not None and not transition(old.status, status):
                _logger.warning(
                    "拒绝非法状态迁移: %s %s → %s", order_id, old.status, status
                )
                return False
            self._orders[order_id] = OrderRecord(
                order_id=order_id,
                code=code or (old.code if old else ""),
                direction=direction or (old.direction if old else 0),
                price=price or (old.price if old else 0.0),
                qty=qty or (old.qty if old else 0),
                filled_qty=filled_qty if filled_qty is not None
                else (old.filled_qty if old else 0),
                status=status,
                remark=remark or (old.remark if old else ""),
            )
            return True

    def apply_trade(
        self,
        traded_id: str,
        order_id: str,
        code: str,
        direction: int,
        price: float,
        qty: int,
        ts: float | None = None,
        strategy: str = "",
    ) -> bool:
        """应用成交回报。按 traded_id 幂等 —— 重复回报不双扣 (返回 False)。
        strategy: 新建仓位的策略归属 (手工认领传"手工", 2026-07-27 裁决④)。"""
        del ts  # MVP 账本不记时间, 时间在 store.trades 里
        with self._lock:
            if traded_id in self._seen_trades:
                _logger.warning("重复成交回报已忽略: %s", traded_id)
                return False
            self._seen_trades.add(traded_id)

            pos = self._positions.setdefault(code, _Position())
            if not pos.strategy and strategy:
                pos.strategy = strategy
            if direction == DIRECTION_BUY:
                # 加权平均成本; T+1 下当日买入不可卖, can_use 不动,
                # 可用数量的真相由 QMT 对账回填
                total_cost = pos.avg_cost * pos.volume + price * qty
                pos.volume += qty
                pos.avg_cost = total_cost / pos.volume
            else:
                pos.volume = max(0, pos.volume - qty)
                pos.can_use = max(0, pos.can_use - qty)
                if pos.volume == 0:
                    pos.avg_cost = 0.0

            # 同步推进订单的已成交量 (成交是比状态回报更硬的进度事实)
            old = self._orders.get(order_id)
            if old is not None:
                new_filled = min(old.qty, old.filled_qty + qty)
                new_status = (
                    OS_SUCCEEDED if new_filled >= old.qty else OS_PART_SUCC
                )
                if transition(old.status, new_status):
                    self._orders[order_id] = OrderRecord(
                        order_id=old.order_id, code=old.code,
                        direction=old.direction, price=old.price, qty=old.qty,
                        filled_qty=new_filled, status=new_status,
                        remark=old.remark,
                    )
            return True

    def snapshot(self) -> Mapping:
        """不可变拷贝, 供 Web/监控读。映射与记录都不可改。"""
        with self._lock:
            positions = {
                code: PositionView(
                    code=code, volume=p.volume, can_use=p.can_use,
                    avg_cost=p.avg_cost, strategy=p.strategy,
                )
                for code, p in self._positions.items()
            }
            tiers = {
                code: {date: frozenset(t) for date, t in by_date.items()}
                for code, by_date in self._tiers.items()
            }
            return MappingProxyType({
                "positions": MappingProxyType(positions),
                "orders": MappingProxyType(dict(self._orders)),
                "tiers": MappingProxyType(tiers),
            })

    # ── 阶梯止盈档位状态 (审计C1修复: 按 (code, 日期) 二维记录) ────

    def set_can_use(self, code: str, can_use: int) -> None:
        """QMT 查询回填可用数量 (执行流水线撤单后刷新持仓用)。
        QMT 是可用数量的唯一真相源, 本地只存镜像 —— 这不是对账回写,
        是执行路径的显式刷新 (计划书 §5.1 "刷新持仓")。"""
        with self._lock:
            pos = self._positions.setdefault(code, _Position())
            pos.can_use = max(0, int(can_use))

    def mark_tier(self, code: str, tier_idx: int, trade_date: str) -> None:
        """乐观标记: 下单提交成功即标记, 防废单后重复卖 (QP 经验)。
        审计C1修复: 必须带 trade_date —— "已预埋"是当日事实,
        昨日标记只留痕, 不阻碍今日重挂 (计划书 §5.1 日终过期次日重挂)。"""
        with self._lock:
            self._tiers.setdefault(code, {}).setdefault(trade_date, set()).add(tier_idx)

    def tier_done(self, code: str, trade_date: str) -> frozenset[int]:
        """查某票当日已预埋的档位集合 (预埋跳过逻辑只认当日)。"""
        with self._lock:
            return frozenset(self._tiers.get(code, {}).get(trade_date, ()))

    def restore(self, positions: list[dict] | None = None,
                tiers: dict | None = None,
                seen_trades: set[str] | None = None,
                orders: dict | None = None) -> None:
        """冷启动恢复三合一 (公开接口 ≤8 的约束下合并, 启动流程只调一次):
        - positions: 采纳 QMT 实时持仓为开盘账本。这不是对账回写
          (铁律 1 禁止的是对账后的自动改账) —— 冷启动本地为空,
          QMT 是唯一真相源, 持仓只能从这里来。
        - tiers: store 读回的当日档位状态 {(code, trade_date): [档...]}。
        - seen_trades: 当日已成交 traded_id 回填幂等集合 (审计H4修复:
          不回填则重启后成交回报重推会双扣持仓 —— 灌入的持仓已含
          当日成交, 再 apply_trade 一次就错账)。
        """
        with self._lock:
            for p in positions or ():
                self._positions[p["code"]] = _Position(
                    volume=int(p["volume"]),
                    can_use=int(p.get("can_use", p["volume"])),
                    avg_cost=float(p.get("avg_cost", 0.0)),
                    strategy=p.get("strategy", ""),
                )
            for (code, trade_date), tiers_ in (tiers or {}).items():
                self._tiers.setdefault(code, {}).setdefault(trade_date, set()).update(tiers_)
            if seen_trades:
                self._seen_trades |= set(seen_trades)
            # 2026-07-30 (600808 事件): 恢复非终态在途订单簿 — 原实现重启后
            # _orders 为空, 撤单流水线找不到券商仍在挂的预埋单, 冻结持仓
            # 无法释放 (13:00 trailing 触发只卖出未冻结的 600/3800)。
            for oid, o in (orders or {}).items():
                self._orders[oid] = OrderRecord(
                    order_id=oid, code=o["code"], direction=o["direction"],
                    price=o["price"], qty=o["qty"],
                    filled_qty=int(o.get("filled_qty", 0)), status=o["status"],
                    remark=o.get("remark", ""))
