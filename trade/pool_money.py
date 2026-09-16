"""trade/pool_money.py — ETF池 vs 股票池资金口径 单一真相源 (治理III W2-1, 2026-09-05)。

背景: 8-21 实盘风控误拒事件 —— 当日卖出回款口径 (在途补回) 散在组合根
(trade_main) 且与 rotation 的市值口径各自实现, "同一件事两块手表"导致
日亏闸误拒 (53.5万 < 87.8万基准显示亏 48%)。本模块把三道保命闸共用的
资金口径收成纯函数:
  - 池拆分 pool_split          (ETF 池/股票池比例)
  - 市值口径 position_value / stock_pool_value   (最新价优先, 无价回退成本价)
  - 预算帽 stock_budget_cap    (股票池还能花的钱)
  - 在途回款 in_flight_sell_returns (8-21/8-24 两轮现场修复的 max 语义)

纪律: 纯函数, 依赖全部参数化注入 (quote_of/positions/gateway 数据由调用方
传值), 不加 IO、不持状态 —— 与 trade_main/rotation 解耦, 可直接单测。
"""
from __future__ import annotations

from trade.book import DIRECTION_SELL

__all__ = [
    "pool_split", "position_value", "stock_pool_value",
    "stock_budget_cap", "in_flight_sell_returns", "rotation_codes",
]


def pool_split(etf_ratio: float) -> tuple[float, float]:
    """双池比例 → (ETF 池比例, 股票池比例)。唯一拆分语义, 相加恒为 1。"""
    return etf_ratio, 1.0 - etf_ratio


def position_value(code: str, volume: int, quote_of,
                   positions: dict) -> float:
    """单标的市值 = 量 × (最新价优先, 无行情回退成本价)。量 ≤ 0 → 0。

    quote_of(code) 返回行情 dict (可能 None/无 last); positions 为
    {代码: 持仓对象(含 volume/avg_cost)}。与对账同口径。"""
    if volume <= 0:
        return 0.0
    q = quote_of(code)
    last = q.get("last") if q else None
    if last and last > 0:
        return float(last) * volume
    pos = positions.get(code)
    cost = getattr(pos, "avg_cost", 0.0) if pos else 0.0
    return cost * volume if cost > 0 else 0.0


def rotation_codes(rot_cfg) -> set:
    """轮动池全部代码 (主腿 + 风险腿2 + 避险腿1/2), 从股票池中排除。
    公开唯一真相源 (2026-09-16 收口: rotation.py 曾手写第二份, 设计评审收编)。"""
    codes = {rot_cfg.cyb_etf, rot_cfg.gold_etf}
    if getattr(rot_cfg, "risk_etf2", None):
        codes.add(rot_cfg.risk_etf2)
    if getattr(rot_cfg, "hedge_etf2", None):
        codes.add(rot_cfg.hedge_etf2)
    return codes


def stock_pool_value(rot_cfg, positions: dict, quote_of) -> float:
    """股票池市值 = 非轮动 ETF 的持仓市值 (逐仓 position_value 汇总)。"""
    rot_codes = rotation_codes(rot_cfg)
    total = 0.0
    for code, pos in positions.items():
        vol = getattr(pos, "volume", 0)
        if code in rot_codes or vol <= 0:
            continue
        total += position_value(code, vol, quote_of, positions)
    return total


def stock_budget_cap(rot_cfg, total_asset: float, stock_value: float) -> float | None:
    """股票池买入预算帽 = max(0, (1−etf_ratio)×总资产 − 股票市值)。

    轮动关闭 → None (auto_buy 走原 cash×0.95, 行为不变);
    total_asset ≤ 0 → None。None 语义 = fail-open 回退原口径 (预算帽是
    软隔离非安全闸, 与 2026-08-14 手册一致)。"""
    if not rot_cfg.enabled:
        return None
    if total_asset <= 0:
        return None
    _, stock_ratio = pool_split(rot_cfg.etf_ratio)
    return max(0.0, stock_ratio * total_asset - stock_value)


def in_flight_sell_returns(trades: list, orders: list, day_start: float) -> float:
    """今日已成交卖出、回款未计入 QMT cash 的在途金额 (日亏闸容错, 只读)。

    口径 (8-21 实盘 + 8-24 现场复现两轮修复, max 语义):
      from_trades = Σ 当日卖出成交 amount (query_trades)
      from_orders = Σ 当日卖出已成交量×委托价 (query_orders)
      return max(from_orders, from_trades)
    两路回报都齐时二者一致 (不重复计, 方向安全偏松); orders 领先的
    窗口 (成交回报晚 ~1s) 用 orders 值兜住低估。仍只读不改账。

    trades/orders 由调用方查询后传入 (本函数无 IO); day_start 为当日
    0 点时间戳 (过滤跨日回报)。卖出方向常量来自 trade.book。
    """
    from_trades = sum(
        float(t.get("amount") or 0.0)
        for t in (trades or [])
        if (t.get("ts") or 0.0) >= day_start
        and t.get("direction") == DIRECTION_SELL
    )
    from_orders = sum(
        float(o.get("price") or 0.0) * float(o.get("filled_qty") or 0.0)
        for o in (orders or [])
        if (o.get("ts") or 0.0) >= day_start
        and o.get("direction") == DIRECTION_SELL
    )
    return max(from_orders, from_trades)
