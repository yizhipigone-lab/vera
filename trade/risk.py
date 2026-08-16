"""trade/risk.py — 风控闸门 + 急停 (三重态)。

设计意图:
    急停三重态 = 内存标志 + DB 标志 + 文件标志, 任一生效即全面拒单;
    只许人工解除 (deactivate 清三源, 代码路径不许自动调用 ——
    调用方只有 Web/CLI 的人工按钮)。
    所有读取侧异常一律 fail-safe 视为激活: 宁可误拒, 不可漏放。
    闸门加新道不改 check 签名 (QP risk_gate 经验)。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from trade.book import DIRECTION_BUY, PositionView
from utils.logger import get_logger

_logger = get_logger("trade.risk")


class KillSwitch:
    """急停三重态。is_active 任一源为真即真; 读取异常 fail-safe 视为激活。"""

    def __init__(self, store, flag_path: str | Path):
        self._store = store
        self._path = Path(flag_path)
        self._memory = False

    def is_active(self) -> bool:
        if self._memory:
            return True
        try:
            if self._store.get_kill_flag()["active"]:
                return True
        except Exception:
            # DB 读不出来 ≠ 没激活; 按激活处理
            _logger.exception("kill_flag DB 读取异常, fail-safe 视为激活")
            return True
        return self._file_active()

    def activate(self, source: str) -> None:
        """三源全置。文件写失败只记日志 —— 内存+DB 已够拒单。"""
        self._memory = True
        self._store.set_kill_flag(True, source)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"active": True, "source": source, "ts": time.time()},
                           ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            _logger.exception("kill 文件写入失败 (内存+DB 已激活)")

    def deactivate(self) -> None:
        """解除急停 —— 只许人工解除, 清三源。自动路径禁止调用本方法。"""
        self._memory = False
        self._store.set_kill_flag(False, "")
        try:
            if self._path.exists():
                self._path.unlink()
        except Exception:
            _logger.exception("kill 文件删除失败, 请人工核查 %s", self._path)

    def _file_active(self) -> bool:
        if not self._path.exists():
            return False
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return bool(data.get("active"))
        except Exception:
            # 文件损坏/读不出 = 状态未知, 按激活处理
            _logger.exception("kill 文件读取异常, fail-safe 视为激活")
            return True


@dataclass(frozen=True)
class OrderIntent:
    """一道下单意图。风控看到的最小事实集。

    manual=True 表示人工指令 (Web/CLI 手动买卖): 2026-08-13 用户裁决 ——
    人工买入不受单笔金额上限约束 (用户对自己的当下意图负全责),
    整手/下限/持仓数等其他闸保持生效。"""

    code: str
    direction: int
    price: float
    qty: int
    manual: bool = False
    # 2026-08-14 ETF 轮动: rotation=True 的买单绕过单笔金额上限与持仓数上限
    # (轮动是"总资产×比例"的满仓/半仓, 受 position_sizing 的 2 万上限约束
    # 永远买不满); 急停/对账/日亏/T+1 四道保命闸照常生效。
    rotation: bool = False


@dataclass(frozen=True)
class RiskContext:
    """风控判断所需的当前世界快照 (由消费者线程组装)。

    day_baseline_equity 为 None 表示盘前基准缺失 ——
    日亏闸走 fail-safe 禁买 (不知道亏没亏, 就当已经亏了)。
    2026-07-26 用户裁决②: 删除在途预扣字段 (集中度闸已砍)。
    """

    reconcile_passed: bool
    total_asset: float
    positions: Mapping[str, PositionView]
    day_baseline_equity: float | None
    current_equity: float
    is_trading_day: bool


class RiskGate:
    """事前风控闸门 (2026-07-26 裁决② 做减法后 4 道 + sizing 校验)。
    每道拒绝写 audit。加闸门不改 check 签名。"""

    def __init__(self, kill_switch: KillSwitch, store,
                 daily_loss_limit: float = 0.05, sizing=None):
        self._kill = kill_switch
        self._store = store
        self._loss_limit = daily_loss_limit
        # sizing = PositionSizingConfig (None 表示不校验, 测试最小装配用)
        self._sizing = sizing

    def check(self, intent: OrderIntent, ctx: RiskContext) -> tuple[bool, str]:
        """过闸。返回 (是否放行, 拒绝原因)。"""
        # 闸 1: 急停 (三重态任一生效, 买卖全拒 —— 铁律 7, 没有例外)
        if self._kill.is_active():
            return self._reject("kill_switch", "急停激活, 全面拒单", intent)

        # 闸 2: 启动对账未通过 —— 审计L8修复(产品裁决): 只拦买单。
        # 卖出是降风险动作, 拦住止损卖单等于把"账面不确定"升级成
        # "亏损确定扩大"; 且执行链路会用 QMT 实时 can_use 双校验 (闸 T+1),
        # 卖错的风险有第二道防线。急停 (闸 1) 才是全面拒单, 闸 2 不是。
        if intent.direction == DIRECTION_BUY and not ctx.reconcile_passed:
            return self._reject("reconcile", "启动对账未通过, 禁买", intent)

        if intent.direction == DIRECTION_BUY:
            # 买入 sizing 校验 (用户裁决②新增, 替代被砍的集中度闸):
            # 对齐回测 position_sizing 口径 [config/default.yaml] —
            # 金额下限/上限、整手、持仓数上限
            if self._sizing is not None:
                ok, reason = self._check_sizing(intent, ctx)
                if not ok:
                    return self._reject("sizing", reason, intent)

            # 闸 3: 日亏软熔断 (基准缺失 fail-safe 禁买)
            if ctx.day_baseline_equity is None:
                return self._reject("daily_loss", "盘前权益基准缺失, fail-safe 禁买", intent)
            if ctx.current_equity < ctx.day_baseline_equity * (1 - self._loss_limit):
                return self._reject(
                    "daily_loss",
                    f"日亏超软熔断线 {self._loss_limit:.0%}: "
                    f"{ctx.current_equity:.0f} < 基准 {ctx.day_baseline_equity:.0f}",
                    intent,
                )
        else:
            # 闸 4: T+1 可卖双校验 —— 本地账本可用数量 + 交易日历。
            # 可用数量的最终真相在券商端 (预埋单冻结), 这道理查挡低级错误
            pos = ctx.positions.get(intent.code)
            if pos is None or pos.can_use < intent.qty:
                have = pos.can_use if pos else 0
                return self._reject(
                    "t1_sellable", f"可卖数量不足: 需 {intent.qty}, 有 {have}", intent)
            if not ctx.is_trading_day:
                return self._reject("t1_sellable", "非交易日, 禁止卖出", intent)

        return True, ""

    def _check_sizing(self, intent: OrderIntent, ctx: RiskContext) -> tuple[bool, str]:
        """买入 sizing 四查: 整手 / 金额下限 / 金额上限 / 持仓数上限。
        与回测 position_sizing 同一参数结构, 不写第二份口径。"""
        s = self._sizing
        if intent.qty % s.lot_size != 0:
            return False, f"非整手: {intent.qty} 不是 {s.lot_size} 的整数倍"
        amount = intent.price * intent.qty
        # 2026-08-15 (审计 L5): 轮动买入豁免金额下限 —— 低单价 ETF (如黄金
        # 518880 一手约 560 元) 的 1~3 手补仓会被 2000 元下限误拒, 导致半仓
        # 小额缺口永不补齐; 轮动有池级预算帽兜底, 豁免下限无超买风险
        if not intent.rotation and amount < s.min_buy_amount:
            return False, f"买入金额 {amount:.0f} 低于下限 {s.min_buy_amount:.0f}"
        # 2026-08-13 用户裁决: 人工买入 (manual=True) 跳过单笔金额上限;
        # 2026-08-14: 轮动买入 (rotation=True) 同理 —— 满仓/半仓是
        # "总资产×比例", 受 2 万上限约束永远买不满
        if not intent.manual and not intent.rotation and amount > s.max_buy_amount:
            return False, f"买入金额 {amount:.0f} 高于上限 {s.max_buy_amount:.0f}"
        held = {c for c, p in ctx.positions.items() if p.volume > 0}
        # 轮动买入的 2 只 ETF 是"池子", 不受选股持仓数上限约束
        if not intent.rotation and intent.code not in held and len(held) >= s.max_positions:
            return False, f"持仓数 {len(held)} 已达上限 {s.max_positions}"
        return True, ""

    def _reject(self, gate: str, reason: str, intent: OrderIntent) -> tuple[bool, str]:
        """拒绝统一收口: 每道拒绝写 audit, 格式一致便于盘后筛。"""
        self._store.write_audit(
            kind="risk_reject",
            message=f"[{gate}] {reason}",
            detail={"code": intent.code, "direction": intent.direction,
                    "price": intent.price, "qty": intent.qty},
        )
        return False, f"{gate}: {reason}"
