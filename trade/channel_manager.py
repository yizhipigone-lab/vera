"""trade/channel_manager.py — 通道运行时管理器 (方案设计书 §5.3)。

职责单一: 持有"武装意图 vs 武装生效"双层态, 驱动探针与悬空重探,
向组合根暴露通道状态; 不直接下单、不触碰事件总线。

双层态铁律:
    armed_effective = armed_intent(配置, 持久) and last_probe.ok(运行时, 唯一钥匙)
    —— 意图存配置 (重启免重新打字确认), 生效态是运行时的; 探针不过,
    任何路径都不能让网关放行 (网关层 armed=False 的 raise 是物理防线)。

断线哨兵: 盘中轮询结果经 on_poll_result() 喂入 —— 连续失败达
rounds 次判通道断 (同花顺 GUI 通道无行情心跳, 轮询是唯一断线证据;
QMT 时代的心跳守卫在此通道天然失效, 见方案设计书 §5.7)。

本模块不 import easytrader/xtquant, 只依赖网关通用接口 —— 无 GUI/
无客户端环境可全量单测 (接缝: 注入替身网关)。
"""
from __future__ import annotations

import time
from typing import Any, Callable


class ChannelManager:
    """通道运行时状态机。构造后立即从配置对齐意图; 生效需探针。

    gateway: 真网关实例 (组合根按 channel 建好再传入)。
    armed_intent: 配置里的武装意图 (ths_armed, 持久)。
    retry_sec: 武装悬空态自动重探间隔 (ths_probe_retry_sec)。
    rounds: 轮询连续失败判断线 (ths_disconnect_rounds)。
    quote_check: 可选 callable() -> bool, 探针的行情源可用性检查
        (方案设计书 §5.8: THS 无行情腿, 无替代行情源禁止武装)。默认 None
        表示"不检查"(QMT 通道本有行情腿, 无需此项)。
    write_audit: 审计留痕 callable(kind, message, detail), 可 None。
    clock: 注入时钟 (测试锚定)。
    """

    def __init__(
        self,
        gateway: Any,
        *,
        armed_intent: bool = False,
        retry_sec: int = 60,
        rounds: int = 3,
        quote_check: Callable[[], bool] | None = None,
        write_audit: Callable[..., None] | None = None,
        clock=time.time,
    ) -> None:
        self._gw = gateway
        self._retry_sec = max(5, int(retry_sec))
        self._rounds = max(1, int(rounds))
        self._quote_check = quote_check
        self._audit = write_audit
        self._clock = clock
        self._armed_intent = bool(armed_intent)
        self._armed_effective = False
        self._last_probe_ok = False
        self._last_probe_ts = 0.0
        self._last_probe_error = ""
        self._consecutive_failures = 0
        self._probed = False
        self._gw.set_armed(False)   # 启动一律未武装, 生效必须过探针

    # ── 只读状态 ──────────────────────────────────────────────
    @property
    def armed_intent(self) -> bool:
        return self._armed_intent

    @property
    def armed_effective(self) -> bool:
        return self._armed_effective

    @property
    def last_probe(self) -> dict:
        return {
            "ok": self._last_probe_ok,
            "ts": self._last_probe_ts,
            "error": self._last_probe_error,
        }

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def channel_down(self) -> bool:
        """轮询连续失败达到判断线 —— 通道断 (GUI 无心跳, 哨兵语义)。"""
        return self._consecutive_failures >= self._rounds

    # ── 配置对齐 (热更 / 重启) ─────────────────────────────────
    def apply(self, *, armed_intent: bool | None = None,
              retry_sec: int | None = None,
              rounds: int | None = None) -> None:
        """换配置引用时调用。改 intent 不自动武装/解除 —— 生效仍以
        最近一次探针为准 (意图是必要条件, 探针通过才放行)。"""
        if armed_intent is not None:
            self._armed_intent = bool(armed_intent)
            if not self._armed_intent and self._armed_effective:
                # 意图是生效的必要条件 —— 关意图立即解武装
                self._set_effective(False)
                self._audit and self._audit(
                    "ths_disarm", "武装意图关闭, 通道解除武装", {})
        if retry_sec is not None:
            self._retry_sec = max(5, int(retry_sec))
        if rounds is not None:
            self._rounds = max(1, int(rounds))

    # ── 探针 ──────────────────────────────────────────────────
    def full_probe(self) -> dict:
        """完整探针 (方案设计书 §5.4): 连接 → 三查 → 资产自洽 → 行情源。

        只读, 失败只记状态不抛 (调用方按 last_probe 处置)。成功更新
        armed_effective = armed_intent and ok; 失败则解除生效 (悬空)。
        """
        error = ""
        ok = False
        try:
            self._gw.connect()
            asset = self._gw.query_asset()
            positions = self._gw.query_positions()
            orders = self._gw.query_orders()
            # 资产口径自洽: 不信客户端汇总字段, 本地重算比对 (THS 网关
            # 内部已重算 total; 这里只验 query_asset 返回结构完整)
            if not isinstance(asset, dict) or "total_asset" not in asset:
                raise ValueError(f"query_asset 结构异常: {asset!r}")
            if not isinstance(positions, list):
                raise ValueError(f"query_positions 结构异常: {positions!r}")
            if not isinstance(orders, list):
                raise ValueError(f"query_orders 结构异常: {orders!r}")
            if self._quote_check is not None and not self._quote_check():
                raise RuntimeError("替代行情源不可用, 禁止武装 (方案设计书 §5.8)")
            ok = True
        except Exception as e:  # 探针失败只记状态, 不外抛
            error = f"{type(e).__name__}: {e}"

        self._last_probe_ts = self._clock()
        self._last_probe_ok = ok
        self._last_probe_error = error
        self._probed = True
        if ok:
            self._consecutive_failures = 0
            self._sync_effective()
        else:
            self._set_effective(False)   # 探针不过 → 悬空 (黄灯)
        self._audit and self._audit(
            "ths_probe",
            f"同花顺通道探针 {'通过' if ok else '未过'}",
            {"ok": ok, "error": error or "",
             "armed_intent": self._armed_intent,
             "armed_effective": self._armed_effective})
        return self.last_probe

    def probe_retry_due(self) -> bool:
        """武装悬空态是否到点该自动重探 (有意图、未生效、距上次 ≥ 间隔)。"""
        if not self._armed_intent or self._armed_effective:
            return False
        if not self._probed:
            return True   # 从未探针 = 悬空, 立即补探
        return (self._clock() - self._last_probe_ts) >= self._retry_sec

    # ── 轮询活性喂入 (盘中活性探针, 兼任 sync_reports) ────────
    def on_poll_result(self, ok: bool) -> None:
        """每次 sync_reports 轮询的结果喂入。成功清零失败计数 (证明活着);
        失败累加, 达判断线判通道断 (调用方据 channel_down 触发断线路径)。
        轮询成功≠武装生效 (武装仍只认 full_probe)。"""
        if ok:
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1

    # ── 武装 / 解除 ────────────────────────────────────────────
    def arm(self) -> dict:
        """武装: 要求意图为真 + 最近一次探针通过。不符返回拒绝理由, 不抛。
        只读查询不受 armed 限制, 武装只影响下单。"""
        if not self._armed_intent:
            return {"ok": False, "reason": "武装意图未开启 (配置 ths_armed=true)"}
        if not self._last_probe_ok:
            return {"ok": False, "reason": "最近一次探针未通过, 禁止武装"}
        if self._set_effective(True):
            self._audit and self._audit(
                "ths_arm", "同花顺通道已武装 (实盘下单放行)", {})
            return {"ok": True, "reason": ""}
        return {"ok": True, "reason": ""}   # 已生效, 幂等

    def disarm(self) -> dict:
        """解除武装: 零摩擦一键 (从危险回安全永远无仪式)。"""
        self._set_effective(False)
        self._audit and self._audit(
            "ths_disarm", "同花顺通道已解除武装 (下单拒绝)", {})
        return {"ok": True, "reason": ""}

    # ── 内部 ──────────────────────────────────────────────────
    def _sync_effective(self) -> None:
        """探针通过后按意图对齐生效态 (只在探针 ok 后调用)。"""
        self._set_effective(self._armed_intent)

    def _set_effective(self, value: bool) -> bool:
        """写生效态 + 下推网关 armed。返回是否发生变更。"""
        value = bool(value)
        changed = value != self._armed_effective
        self._armed_effective = value
        try:
            self._gw.set_armed(value)
        except Exception:
            # 网关武装失败 (例如连接已断) —— 生效态仍按意图记, 但网关
            # 层 order() 的 armed 锁会兜底拒单 (fail-safe 方向)
            changed = True
        return changed
