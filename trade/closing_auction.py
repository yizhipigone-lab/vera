"""trade/closing_auction.py — 收盘集合竞价限价规则 (单一实现, 2026-09-15 审计收口)。

背景 (实测驱动): 沪深 14:57 后进入收盘集合竞价, 只收限价单 (单一价格撮合,
成交价=收盘价); 市价类报单被券商柜台禁用 (废单码 63596)。此时:
- 买侧挂涨停价 = 最大买入优先权, 成交价仍是收盘价, 不吃亏;
  但 20% 涨跌幅品种 (创业 300/301 + 科创 688) 涨停价超 2% 价格笼子,
  被主机暂存不废不成交 (07-31 实测 0/12) → 贴笼子上限 min(涨停, 卖一×1.02)。
  688 是沪市, 不能靠市场判定, 只用代码前缀 (08-01 实测 688099 同死法)。
- 卖侧挂跌停价 = 最大卖出优先权, 同理成交价仍是收盘价 (沪市 2018 起同制)。

此前该知识写两份 (auto_buy 买侧 / executor 卖侧), 时段边界/笼子系数/
板块前缀各自硬编码 —— 同一条规则手写 N 份必然漂移, 收口于此。

公开接口 (2): auction_buy_price / auction_sell_price。
"""
from __future__ import annotations

from core.limit_ratio import limit_ratio
from trade.book import round_price

#: 20% 涨跌幅品种代码前缀 (创业 300/301 + 科创 688)
_20PCT_PREFIXES = ("300", "301", "688")
#: 价格笼子系数: 买侧贴卖一上限 / 卖侧贴买一下限 (2% 笼子)
CAGE_BUY_FACTOR = 1.02
CAGE_SELL_FACTOR = 0.98


def auction_buy_price(code: str, ask1: float, limit_up: float) -> tuple[float, str]:
    """收盘竞价买入限价 → (价格, 中文定价说明)。

    20% 品种贴笼子上限 min(涨停价, 卖一×1.02); 主板直接挂涨停价。
    """
    code_num = code.split(".")[0]
    if code_num.startswith(_20PCT_PREFIXES):
        return round_price(min(limit_up, ask1 * CAGE_BUY_FACTOR)), "笼子上限限价(收盘竞价)"
    return round_price(limit_up), "涨停价限价(收盘竞价)"


def auction_sell_price(code: str, prev_close: float, st: bool = False) -> float:
    """收盘竞价卖出限价 = 跌停价 (限价单优先权最大, 成交价=收盘价)。"""
    return round_price(prev_close * (1 - limit_ratio(code, st)))
