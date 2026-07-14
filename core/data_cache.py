"""DataCache — 进程级数据缓存（C6 从 DataFetcher 抽出）。

把板块列表 / 板块成份股 / 股票简称三类缓存从 DataFetcher 的类属性
抽成独立类, 让缓存职责单一、可独立测试。DataFetcher 持有一个 DataCache
实例并委托 clear 操作。
"""

from __future__ import annotations

from typing import Dict, List


class DataCache:
    """进程级缓存: 板块列表、板块成份股、股票简称映射。"""

    def __init__(self):
        self.sector_list: List[dict] = []        # [{"code","name"}, ...]
        self.sector_stocks: Dict[str, List[str]] = {}  # {sector_code: [成份股代码]}
        self.name_map: Dict[str, str] = {}       # {stock_code: name}

    # ── 板块列表 ──
    def has_sector_list(self) -> bool:
        return bool(self.sector_list)

    def get_sector_list(self) -> List[dict]:
        return self.sector_list

    def set_sector_list(self, value: List[dict]) -> None:
        self.sector_list = value

    # ── 板块成份股 ──
    def has_sector_stocks(self, sector_code: str) -> bool:
        return sector_code in self.sector_stocks

    def get_sector_stocks(self, sector_code: str) -> List[str]:
        return self.sector_stocks[sector_code]

    def set_sector_stocks(self, sector_code: str, value: List[str]) -> None:
        self.sector_stocks[sector_code] = value

    # ── 简称映射 ──
    def has_name_map(self) -> bool:
        return bool(self.name_map)

    def get_name_map(self) -> Dict[str, str]:
        return self.name_map

    def set_name_map(self, value: Dict[str, str]) -> None:
        self.name_map = value

    # ── 清理 ──
    def clear_sector(self) -> None:
        """清空板块列表 + 成份股缓存。"""
        self.sector_list.clear()
        self.sector_stocks.clear()

    def clear_name(self) -> None:
        """清空简称缓存。"""
        self.name_map.clear()

    def clear_all(self) -> None:
        self.clear_sector()
        self.clear_name()
