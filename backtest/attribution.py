"""backtest/attribution.py — 业绩归因 (图表分析深挖包 Phase 2, 2026-08-13)。

纯函数: 输入归一后的 [{code, pnl}] + 票→行业映射, 输出行业聚合 + 个股 Top-N。
不碰网络不碰 TDX — 映射由调用侧构建 (回测侧 policy_kb.build_stock_sector_index,
实盘侧复用同一构建器)。

契约 (前端钉死):
- by_sector: [{"name", "pnl", "pct"}] 按 pnl 降序; 未映射行业 → name="未标"
- by_stock_top: [{"code", "name", "pnl", "pct"}] 同票多笔合并, 按 |pnl| 降序取前 10
- pct = pnl / 总净盈亏; 总和为 0 → pct 为 None
- name 取不到 → 空串 (前端兜底显示 code); 行业名缺映射 → 回退为映射值本身
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

UNMAPPED_SECTOR = "未标"

DEFAULT_TOP_N = 10


def attribute_returns(
    items: List[dict],
    sector_index: Dict[str, str],
    sector_names: Optional[Dict[str, str]] = None,
    stock_names: Optional[Dict[str, str]] = None,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """归一后的盈亏条目 → 行业/个股归因。

    items: [{"code": str, "pnl": float}] (金额口径, 调用侧负责归一:
    回测侧从 trades 抽 stock_code+pnl; 实盘侧从 deals 抽 code+pnl_amount)。
    sector_index: {票代码: 行业键} (build_stock_sector_index 的产物)。
    sector_names: {行业键: 行业名} 可选; 缺省/缺失时 name 回退为行业键本身。
    stock_names: {票代码: 简称} 可选; 缺失时 name 为空串。
    pnl 非数值/NaN/inf 的行跳过, 不抛。
    """
    sector_names = sector_names or {}
    stock_names = stock_names or {}
    sector_index = sector_index or {}

    sector_pnl: Dict[str, float] = {}
    stock_pnl: Dict[str, float] = {}
    total = 0.0
    for it in items or []:
        code = str(it.get("code", "") or "")
        try:
            pnl = float(it.get("pnl"))
        except (TypeError, ValueError):
            continue
        if math.isnan(pnl) or math.isinf(pnl):
            continue
        key = sector_index.get(code)
        name = sector_names.get(key, key) if key else UNMAPPED_SECTOR
        sector_pnl[name] = sector_pnl.get(name, 0.0) + pnl
        stock_pnl[code] = stock_pnl.get(code, 0.0) + pnl
        total += pnl

    def _pct(p: float):
        return p / total if total != 0 else None

    by_sector = [{"name": n, "pnl": p, "pct": _pct(p)} for n, p in sector_pnl.items()]
    # pnl 降序; 并列按 name 定序 (输出确定性)
    by_sector.sort(key=lambda d: (-d["pnl"], d["name"]))

    top = sorted(stock_pnl.items(), key=lambda kv: (-abs(kv[1]), kv[0]))[:top_n]
    by_stock_top = [
        {"code": c, "name": stock_names.get(c, ""), "pnl": p, "pct": _pct(p)}
        for c, p in top
    ]
    return {"by_sector": by_sector, "by_stock_top": by_stock_top}
