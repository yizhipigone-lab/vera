"""核心循环拆解的数据结构层。

候选 A 阶段 2 — 把 _simulate_core_v3 的裸 numpy 数组原地操作, 抽成可测试的 dataclass。

类型约定（v3 计划书 §2.1 M1）:
- np.float64: 价格 / 份额 / 盈亏（shares, entry_px, high_px, high_hi, 所有价格）
- np.int32:   索引 / bitmask（code, entry_idx, ladder_done）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np


# ─────────────────────────────────────────────────────────────
# 列常量: trades 数组 9 列（HR5 锁定, engine.py:76/110-118）
# ─────────────────────────────────────────────────────────────
class TradeColumns:
    """raw_trades 数组列索引常量。9 列, 顺序不可变。"""

    CODE = 0          # 股票列索引 ci
    ENTRY_IDX = 1     # 入场 bar
    EXIT_IDX = 2      # 出场 bar
    ENTRY_PX = 3      # 入场价
    SELL_PX = 4       # 成交价
    SHARES = 5        # 成交份额
    PROFIT = 6        # 绝对盈亏 (gross - cost)
    RETURN = 7        # 相对盈亏 (sell-entry)/entry
    REASON = 8        # 退出原因码
    NCOLS = 9


# ─────────────────────────────────────────────────────────────
# 全局回测参数（CA2）
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class BacktestParams:
    """承载不属于任何策略的全局回测参数。frozen, 不可变。"""

    initial_capital: float
    commission: float
    slippage: float
    stamp_tax: float
    min_buy_amount: float
    max_buy_amount: float
    lot_size: int
    min_lots: int
    bpday: int = 1               # bars per day
    max_position_pct: float = 1.0  # 单票占比上限（1.0=不约束, 老行为）

    def __post_init__(self):
        # M2: 启动期 fail-fast, 防 bpday=0 除零
        if self.bpday < 1:
            raise ValueError(f"bpday 必须 >= 1, 收到 {self.bpday}")
        if self.lot_size < 1:
            raise ValueError(f"lot_size 必须 >= 1, 收到 {self.lot_size}")


# ─────────────────────────────────────────────────────────────
# 单 bar 市场数据
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Bar:
    """单只股票单根 bar 的 OHLC。frozen。"""

    close: float
    high: float
    low: float
    open: float


# ─────────────────────────────────────────────────────────────
# 评估上下文（CA3）
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Context:
    """单次 check() 调用的上下文（per bar per position）。frozen, 策略不得修改。

    价格/盈亏快照由 BacktestLoop 在调用前算好传入, 策略不重算
    （与 engine.py:146-165 的预计算对齐）。
    ladder_profits / ladder_ratios 为只读视图（R9）。
    """

    bar_index: int
    ci: int
    bpday: int
    hold_days: int
    # 持仓衍生量:
    entry_px: float
    pp: float            # (close-entry)/entry
    hp_profit: float     # (max(high_px,close)-entry)/entry
    peak_hi: float       # pos_high_hi 持仓期最高价
    peak_hi_profit: float
    pos_high_px: float
    pos_high_hi: float
    hi_pp: float         # (high-entry)/entry
    lo_pp: float         # (low-entry)/entry
    # 阶梯止盈只读视图:
    ladder_profits: np.ndarray
    ladder_ratios: np.ndarray
    n_ladder: int


# ─────────────────────────────────────────────────────────────
# 持仓（HA4: mutable, 跨 bar 累计状态）
# ─────────────────────────────────────────────────────────────
@dataclass(slots=True)
class Position:
    """单笔持仓。mutable — ladder_done bitmask 与 high_px/high_hi 跨 bar 累计。

    与 engine.py:64-70 的 pos_* 数组对齐:
      pos_code→code(int32) pos_shares→shares(float64) pos_entry_px→entry_px(float64)
      pos_entry_idx→entry_idx(int32) pos_high_px→high_px(float64)
      pos_high_hi→high_hi(float64) pos_ladder_done→ladder_done(int32 bitmask)
    """

    code: int            # np.int32
    shares: float        # np.float64
    entry_px: float      # np.float64
    entry_idx: int       # np.int32
    high_px: float       # np.float64, 持仓期最高收盘价
    high_hi: float       # np.float64, 持仓期最高价(来自 high_np)
    ladder_done: int = 0  # np.int32, 阶梯止盈已触发档位 bitmask, 跨 bar 累计


# ─────────────────────────────────────────────────────────────
# dtype 断言（R3/M1）
# ─────────────────────────────────────────────────────────────
def assert_state_dtype(pos: Position, bar: Bar) -> None:
    """断言 Position / Bar 的字段 dtype 约定 (T-CR-2 修复, 2026-07-15).

    接收外部传入的 Position 和 Bar 对象做真实校验,
    不再内部构造自检 (旧版是 tautology, 永远不可能失败).
    """
    # Position 字段校验 (int 字段: code/entry_idx/ladder_done; float 字段: shares/entry_px/high_px/high_hi)
    if not isinstance(pos.code, (int, np.integer)):
        raise TypeError("Position.code 必须 int")
    if not isinstance(pos.shares, (float, np.floating)):
        raise TypeError("Position.shares 必须 float")
    if not isinstance(pos.entry_px, (float, np.floating)):
        raise TypeError("Position.entry_px 必须 float")
    if not isinstance(pos.entry_idx, (int, np.integer)):
        raise TypeError("Position.entry_idx 必须 int")
    if not isinstance(pos.high_px, (float, np.floating)):
        raise TypeError("Position.high_px 必须 float")
    if not isinstance(pos.high_hi, (float, np.floating)):
        raise TypeError("Position.high_hi 必须 float")
    if not isinstance(pos.ladder_done, (int, np.integer)):
        raise TypeError("Position.ladder_done 必须 int(bitmask)")
    # Bar 字段校验 (全部 float)
    for f in ("close", "high", "low", "open"):
        if not isinstance(getattr(bar, f), (float, np.floating)):
            raise TypeError(f"Bar.{f} 必须 float")


# ─────────────────────────────────────────────────────────────
# TradeBuffer: 封装 raw_trades 数组 + 动态扩容（MA4）
# ─────────────────────────────────────────────────────────────
class TradeBuffer:
    """封装 raw_trades 数组写入 + 动态扩容。

    对齐 engine.py:75-87 的 max_trades 估算与 _grow_trades() 扩容逻辑。
    列顺序由 TradeColumns 锁定。
    """

    def __init__(self, n_dates: int, n_stocks: int):
        max_trades = n_dates * n_stocks // 4 + 1000
        self._arr = np.empty((max_trades, TradeColumns.NCOLS), dtype=np.float64)
        self._count = 0

    @property
    def dtype(self) -> np.dtype:
        return self._arr.dtype

    @property
    def count(self) -> int:
        return self._count

    @property
    def capacity(self) -> int:
        return self._arr.shape[0]

    def _grow(self) -> None:
        new_max = self.capacity * 2
        new_arr = np.empty((new_max, TradeColumns.NCOLS), dtype=np.float64)
        new_arr[: self._count] = self._arr[: self._count]
        self._arr = new_arr

    def append(self, code: int, entry_idx: int, exit_idx: int, entry_px: float,
               sell_px: float, shares: float, profit: float, ret: float,
               reason: int) -> None:
        """写一行 trade。列顺序见 TradeColumns。"""
        if self._count >= self.capacity:
            self._grow()
        r = self._count
        self._arr[r, TradeColumns.CODE] = float(code)
        self._arr[r, TradeColumns.ENTRY_IDX] = float(entry_idx)
        self._arr[r, TradeColumns.EXIT_IDX] = float(exit_idx)
        self._arr[r, TradeColumns.ENTRY_PX] = float(entry_px)
        self._arr[r, TradeColumns.SELL_PX] = float(sell_px)
        self._arr[r, TradeColumns.SHARES] = float(shares)
        self._arr[r, TradeColumns.PROFIT] = float(profit)
        self._arr[r, TradeColumns.RETURN] = float(ret)
        self._arr[r, TradeColumns.REASON] = float(reason)
        self._count += 1

    def to_array(self) -> np.ndarray:
        """返回已写入部分的 (count, 9) 数组（副本）。"""
        return self._arr[: self._count].copy()


# ─────────────────────────────────────────────────────────────
# PositionBook: 持仓数组管理 + swap-and-pop + 退市驱逐（CR2/HA5）
# ─────────────────────────────────────────────────────────────
class PositionBook:
    """持仓数组管理。对齐 engine.py:64-71 的 pos_* 并行数组 + swap-and-pop 重排。

    阶段 1 只提供最小接口（构造/添加/遍历/删除）; evict_delisted() 在阶段 3 补。
    """

    def __init__(self, max_pos: int = 5000):
        self._max = max_pos
        self._code = np.full(max_pos, -1, dtype=np.int32)
        self._shares = np.zeros(max_pos, dtype=np.float64)
        self._entry_px = np.zeros(max_pos, dtype=np.float64)
        self._entry_idx = np.full(max_pos, -1, dtype=np.int32)
        self._high_px = np.zeros(max_pos, dtype=np.float64)
        self._high_hi = np.zeros(max_pos, dtype=np.float64)
        self._ladder_done = np.zeros(max_pos, dtype=np.int32)
        self._count = 0

    @property
    def count(self) -> int:
        return self._count

    @property
    def dtype_code(self) -> np.dtype:
        return self._code.dtype

    @property
    def dtype_shares(self) -> np.dtype:
        return self._shares.dtype

    def add(self, code: int, shares: float, entry_px: float,
            entry_idx: int, high_px: float, high_hi: float) -> int:
        """新增持仓, 返回槽位 index。"""
        if self._count >= self._max:
            raise RuntimeError("PositionBook 满 (MAX_POS=5000)")
        p = self._count
        self._code[p] = code
        self._shares[p] = shares
        self._entry_px[p] = entry_px
        self._entry_idx[p] = entry_idx
        self._high_px[p] = high_px
        self._high_hi[p] = high_hi
        self._ladder_done[p] = 0
        self._count += 1
        return p

    def get(self, p: int) -> Position:
        """读槽位 p 为 Position 快照。

        float 字段保留 np.float64（不 cast Python float）, 保证策略算术与 legacy
        裸 np.float64 数组操作字节级一致（防 1-ULP 漂移）。
        """
        return Position(
            code=int(self._code[p]), shares=self._shares[p],
            entry_px=self._entry_px[p], entry_idx=int(self._entry_idx[p]),
            high_px=self._high_px[p], high_hi=self._high_hi[p],
            ladder_done=int(self._ladder_done[p]),
        )

    def set(self, p: int, pos: Position) -> None:
        """把 Position 写回槽位 p。"""
        self._code[p] = pos.code
        self._shares[p] = pos.shares
        self._entry_px[p] = pos.entry_px
        self._entry_idx[p] = pos.entry_idx
        self._high_px[p] = pos.high_px
        self._high_hi[p] = pos.high_hi
        self._ladder_done[p] = pos.ladder_done

    def update_high(self, p: int, high_px: float, high_hi: float) -> None:
        """更新持仓期最高价（loop 每 bar 调）。"""
        if high_px > self._high_px[p]:
            self._high_px[p] = high_px
        if high_hi > self._high_hi[p]:
            self._high_hi[p] = high_hi

    def set_shares(self, p: int, shares: float) -> None:
        self._shares[p] = shares

    def set_ladder_done(self, p: int, ladder_done: int) -> None:
        self._ladder_done[p] = ladder_done

    # ── 数组访问器（hot loop 直接索引, 对齐 engine.py 的 pos_* 裸数组操作）──
    @property
    def code_arr(self) -> np.ndarray:
        return self._code

    @property
    def shares_arr(self) -> np.ndarray:
        return self._shares

    @property
    def entry_px_arr(self) -> np.ndarray:
        return self._entry_px

    @property
    def entry_idx_arr(self) -> np.ndarray:
        return self._entry_idx

    @property
    def high_px_arr(self) -> np.ndarray:
        return self._high_px

    @property
    def high_hi_arr(self) -> np.ndarray:
        return self._high_hi

    @property
    def ladder_done_arr(self) -> np.ndarray:
        return self._ladder_done

    def remove_swap_pop(self, p: int) -> None:
        """swap-and-pop 删除槽位 p（对齐 engine.py:120-128）。

        把最后一个槽位挪到 p, count-=1。遍历中删除时调用方负责不 p+=1。
        """
        last = self._count - 1
        if p < last:
            self._code[p] = self._code[last]
            self._shares[p] = self._shares[last]
            self._entry_px[p] = self._entry_px[last]
            self._entry_idx[p] = self._entry_idx[last]
            self._high_px[p] = self._high_px[last]
            self._high_hi[p] = self._high_hi[last]
            self._ladder_done[p] = self._ladder_done[last]
        self._count -= 1
