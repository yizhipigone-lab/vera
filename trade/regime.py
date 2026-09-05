"""trade/regime.py — 弱市择时闸门 (2026-08-16)。

规则 (用户拍板): 指数最新价 (收盘/实时) 站上 MA 均线才允许开新仓,
跌破则当日不买 (fail-closed)。

【当前边界 (2026-09-05 治理III W4 修正)】生产唯一调用方是 trade/auto_buy.py
(尾盘选股闸门: 14:5x 用指数实时价判断, 跌破当日不买; 已持仓照常管理)。
回测引擎 (backtest/) **不消费本模块** —— 早期 docstring "回测与实盘共用
同一份判断" 不实 (曾引起双实现假设); 实盘侧如需回测对照, 走独立研究脚本
research/quantqq_sweep/regime_filter_test.py (按日掩码, 自成一套), 与本
模块无关。若未来回测要接入同口径闸门, 应显式立项并在此登记。
"""
from __future__ import annotations


def index_above_ma(daily_closes, ma_window: int = 200):
    """指数最新价是否站上 MA 均线。

    Args:
        daily_closes: 升序收盘价序列 (float), 最后一个元素是"当前判断价"
            (回测 = T 日收盘; 实盘 = 14:5x 实时价, 前面接最近 N-1 根已收盘日线)。
        ma_window: 均线窗口 (默认 200 = 年线)。

    Returns:
        True  = 站上均线 (放行开新仓)
        False = 跌破均线 (禁买)
        None  = 数据不足 (fail-closed, 按禁买处理)
    """
    if not daily_closes:
        return None
    closes = [float(x) for x in daily_closes if x == x and x > 0]
    if len(closes) < ma_window:
        return None
    ma = sum(closes[-ma_window:]) / ma_window
    return closes[-1] >= ma
