"""DataCache — 进程级数据缓存（C6 从 DataFetcher 抽出）。

把板块列表 / 板块成份股 / 股票简称三类缓存从 DataFetcher 的类属性
抽成独立类, 让缓存职责单一、可独立测试。DataFetcher 持有一个 DataCache
实例并委托 clear 操作。

2026-08-01 (D6): 加 TTL 惰性过期 —— 长驻进程 (server/scheduler/trade)
此前纯 dict 永不过期, 只有手动 clear_*。现 set 记时间戳, has_* 读取侧
判过期: 板块列表/成份股 24h、简称映射 7d。过期即 miss → 调用方走既有
回源路径重拉 (实盘盘中代价 = 一次额外 TDX 查询, 可接受)。get_* 语义不变
(仍返回原值, 不返回 None), 判过期只在 has_* —— 不碰既有 has→get 契约。
"""

from __future__ import annotations

import time
from typing import Dict, List

# 板块列表/成份股 24h (成份调整日级频率), 简称映射 7d (更名罕见)
SECTOR_TTL_SEC = 24 * 3600
NAME_MAP_TTL_SEC = 7 * 24 * 3600


class DataCache:
    """进程级缓存: 板块列表、板块成份股、股票简称映射 (带 TTL 惰性过期)。"""

    def __init__(self, *, sector_ttl: float = SECTOR_TTL_SEC,
                 name_ttl: float = NAME_MAP_TTL_SEC, clock=time.time):
        self.sector_list: List[dict] = []        # [{"code","name"}, ...]
        self.sector_stocks: Dict[str, List[str]] = {}  # {sector_code: [成份股代码]}
        self.name_map: Dict[str, str] = {}       # {stock_code: name}
        self._sector_ttl = sector_ttl
        self._name_ttl = name_ttl
        self._clock = clock                      # 注入时钟, 测试可控
        self._sector_list_ts: float | None = None
        self._sector_stocks_ts: Dict[str, float] = {}
        self._name_map_ts: float | None = None

    def _expired(self, ts: float | None, ttl: float) -> bool:
        return ts is None or self._clock() - ts > ttl

    # ── 板块列表 ──
    def has_sector_list(self) -> bool:
        return bool(self.sector_list) and not self._expired(
            self._sector_list_ts, self._sector_ttl)

    def get_sector_list(self) -> List[dict]:
        return self.sector_list

    def set_sector_list(self, value: List[dict]) -> None:
        self.sector_list = value
        self._sector_list_ts = self._clock()

    # ── 板块成份股 ──
    def has_sector_stocks(self, sector_code: str) -> bool:
        return sector_code in self.sector_stocks and not self._expired(
            self._sector_stocks_ts.get(sector_code), self._sector_ttl)

    def get_sector_stocks(self, sector_code: str) -> List[str]:
        return self.sector_stocks[sector_code]

    def set_sector_stocks(self, sector_code: str, value: List[str]) -> None:
        self.sector_stocks[sector_code] = value
        self._sector_stocks_ts[sector_code] = self._clock()

    # ── 简称映射 ──
    def has_name_map(self) -> bool:
        return bool(self.name_map) and not self._expired(
            self._name_map_ts, self._name_ttl)

    def get_name_map(self) -> Dict[str, str]:
        return self.name_map

    def set_name_map(self, value: Dict[str, str]) -> None:
        self.name_map = value
        self._name_map_ts = self._clock()

    # ── 清理 ──
    def clear_sector(self) -> None:
        """清空板块列表 + 成份股缓存。"""
        self.sector_list.clear()
        self.sector_stocks.clear()
        self._sector_list_ts = None
        self._sector_stocks_ts.clear()

    def clear_name(self) -> None:
        """清空简称缓存。"""
        self.name_map.clear()
        self._name_map_ts = None

    def clear_all(self) -> None:
        self.clear_sector()
        self.clear_name()
