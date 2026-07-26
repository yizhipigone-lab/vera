"""给票列表打十五五行业优先级标签(政策匹配 A 层,计划书 §4.1/§5)。

查 tongdaxin_priority.json(行业→优先级)+ build_sector_index(票→行业)。
ETF / 可转债 / 不在 128 行业的票 → "UNTAGGED"(未标)。
失败返 None(松耦合,Pipeline 接缝 serialize "有才加 key",None 不加)。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from policy_kb.build_sector_index import build_stock_sector_index
from utils.logger import get_logger

logger = get_logger(__name__)

_JSON_PATH = Path(__file__).resolve().parent / "tongdaxin_priority.json"
_PRIORITY_CACHE: Optional[Dict[str, dict]] = None  # {sector_code: {"name","priority"}}

UNTAGGED = "UNTAGGED"  # 未标(ETF/可转债/不在 128 行业)


def _load_priority_map(force_refresh: bool = False) -> Dict[str, dict]:
    """加载 tongdaxin_priority.json 的 sectors → {code: {"name","priority"}}。带缓存。"""
    global _PRIORITY_CACHE
    if _PRIORITY_CACHE is not None and not force_refresh:
        return _PRIORITY_CACHE
    with open(_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    sectors = data.get("sectors", [])
    _PRIORITY_CACHE = {
        s["code"]: {"name": s.get("name", ""), "priority": s["priority"]}
        for s in sectors
        if s.get("code") and s.get("priority")
    }
    return _PRIORITY_CACHE


def tag_stock(stock_code: str, index: Optional[Dict[str, str]] = None) -> str:
    """单票 → 优先级(P1/P2/P3/AVOID/UNTAGGED)。

    index: 可选预构建反向索引(批量场景省重复构建)。不在索引或 JSON → UNTAGGED。
    """
    pm = _load_priority_map()
    if index is None:
        index = build_stock_sector_index()
    sector_code = index.get(stock_code)
    if sector_code is None or sector_code not in pm:
        return UNTAGGED
    return pm[sector_code]["priority"]


def tag_stocks(stock_codes: List[str]) -> Optional[Dict[str, str]]:
    """批量:票列表 → {票: 优先级}。失败返 None(松耦合,Pipeline 兜底)。

    返回 None 而非 {} 以区分"成功但无票"(→ {} 不该)和"失败"(→ None,
    serialize 不加 key)。空输入 → {} 。
    """
    try:
        index = build_stock_sector_index()
        return {c: tag_stock(c, index) for c in stock_codes if c}
    except Exception as e:
        logger.warning(f"tag_stocks 失败(松耦合返 None): {e}", exc_info=True)
        return None


def tag_selections(selections: pd.DataFrame, code_col: str = "stock_code") -> Optional[Dict[str, str]]:
    """给选股结果 DataFrame 打标签 → {票: 优先级}。

    Pipeline 接缝用这个(pipeline.py 因子过滤后、回测前调用)。
    失败 / 空 / 无 code 列 → None(serialize 不加 key,选股/回测不受影响)。
    """
    if selections is None or selections.empty:
        return None
    if code_col not in selections.columns:
        logger.warning(f"selections 无 {code_col} 列,policy_priority 返 None")
        return None
    codes = selections[code_col].astype(str).tolist()
    return tag_stocks(codes)


def clear_cache() -> None:
    """清优先级 JSON 缓存(测试用)。"""
    global _PRIORITY_CACHE
    _PRIORITY_CACHE = None
