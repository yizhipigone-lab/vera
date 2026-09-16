# -*- coding: utf-8 -*-
"""trade/quote_check_tdx.py — 通达信 TQ 替代行情源验枪适配器 (2026-09-07 T7)。

设计意图:
    ChannelManager 的 quote_check 接缝 (方案设计书 §5.8) 需要一个
    零参 callable() -> bool 回答"替代行情源活着吗"。本模块把
    core/tdx_tq.snapshot (通达信 TQ get_market_snapshot) 包成该形状。
    用户拍板方案 A: 验枪与盘中轮询同源走通达信 (单源最简, 2026-09-07)。

口径:
    验枪只验证"通道活 + 价格有效" (now 或 prev_close > 0),
    不做时间新鲜度判断 —— snapshot 归一化字段不含时间戳,
    且盘前/盘后 now 退化为昨收属正常 (活着就行, 不必在跳)。
    fail-soft: tdx_tq.snapshot 任何异常返回 None → 验枪不通过
    → 探针失败 → 禁止武装 (fail-closed, 宁错杀不放过)。

接缝测试:
    monkeypatch core.tdx_tq.snapshot 即可, 无需通达信客户端。
"""
from __future__ import annotations

from typing import Callable

__all__ = ["DEFAULT_PROBE_CODE", "make_tdx_quote_check"]

# 默认验枪标的: 沪深300ETF, 常年高流动性, 交易日必有快照
DEFAULT_PROBE_CODE = "510300.SH"


def make_tdx_quote_check(code: str = DEFAULT_PROBE_CODE) -> Callable[[], bool]:
    """返回零参验枪 callable: 通达信 TQ 快照活着且价格有效 → True。

    code: 验枪标的 (默认 510300.SH 沪深300ETF)。模块导入零 TDX
    依赖 (tdx_tq 内部懒加载), 可安全在组合根无条件调用本工厂。
    """
    probe = (code or "").strip() or DEFAULT_PROBE_CODE

    def _check() -> bool:
        from core import tdx_tq  # noqa: PLC0415 (模块级也零依赖, 这里再保险)
        try:
            snap = tdx_tq.snapshot(probe)
        except Exception:
            return False  # 双保险: tdx_tq 自身 fail-soft, 这里仍不抛给探针
        if not snap:
            return False
        price = snap.get("now") or snap.get("prev_close")
        return bool(price and price > 0)

    return _check
