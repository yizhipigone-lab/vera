"""票 → 行业 反向索引(政策匹配 A 层,计划书 §4.2)。

VERA 的 DataFetcher.get_sector_stocks 是正向(行业→票),无反查(已读
data_fetcher.py:564-607 全方法确认)。本模块遍历 128 行业构建
{stock_code: sector_code} 反向映射,带进程级缓存。

首次构建 ~17s(128 次 get_sector_stocks,有 sector 缓存),之后命中。
任一行业拉取失败记 warning 跳过,不抛(松耦合,返部分结果)。
"""
from __future__ import annotations

from typing import Dict, Optional

from core.data_fetcher import DataFetcher
from utils.logger import get_logger

logger = get_logger(__name__)

# 进程级缓存(None = 未构建);配 clear_cache() 供测试/强制刷新
_cache: Optional[Dict[str, str]] = None


def build_stock_sector_index(force_refresh: bool = False) -> Dict[str, str]:
    """构建 {stock_code: sector_code} 反向映射。

    遍历 DataFetcher.get_sector_list() 的 128 行业 × get_sector_stocks。
    一只票多归属取第一个 + debug 日志(计划书 §9 风险;通达信 881 通常
    一票一行业,实施时验证)。带进程级缓存;force_refresh=True 强制重建。
    任一行业拉取失败记 warning 跳过,不抛(返部分结果,松耦合)。
    """
    global _cache
    if _cache is not None and not force_refresh:
        return _cache

    sectors = DataFetcher.get_sector_list()
    index: Dict[str, str] = {}
    for s in sectors:
        sector_code = s.get("code", "")
        if not sector_code:
            continue
        try:
            stocks = DataFetcher.get_sector_stocks(sector_code)
        except Exception as e:
            logger.warning(f"拉板块成份股失败 [{sector_code} {s.get('name', '')}]: {e}")
            continue
        for stock in stocks:
            if not stock:
                continue
            if stock in index:
                # 多归属:保留首个(计划书 §9 风险控制)
                logger.debug(
                    f"多归属: {stock} 已属 {index[stock]}, 当前 {sector_code}, 保留首个"
                )
                continue
            index[stock] = sector_code

    _cache = dict(index)  # 快照(防外部 mutate)
    logger.info(f"反向索引构建完成: {len(index)} 只票 × {len(sectors)} 行业")
    return _cache


def get_sector_of(stock_code: str) -> Optional[str]:
    """查单只票的行业代码;不在索引返回 None。"""
    idx = build_stock_sector_index()
    return idx.get(stock_code)


def clear_cache() -> None:
    """清进程级缓存(测试隔离 / 强制刷新用)。"""
    global _cache
    _cache = None
