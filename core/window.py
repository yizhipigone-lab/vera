"""窗口数学纯函数 (2026-08-16 从 core/data_fetcher 挪出, 去上帝化专项)。

窗口 mask 合并 (merge_window_masks) + 窗口边界计算 (compute_window_bounds)
是纯数学, 本不该跟 TDX 取数混在一起。本模块无 TDX 依赖 —— 日历经
calendar_fetcher 回调注入 (None 默认 → 退化自然日估算), 可独立单测。
"""

from __future__ import annotations

import bisect
from typing import List, Optional

import pandas as pd

from utils.code_normalizer import normalize_list
from utils.logger import get_logger

logger = get_logger(__name__)


def merge_window_masks(mask_frames: List[pd.DataFrame]) -> pd.DataFrame:
    """合并各批窗口 mask: 时间轴取并集, 同 (行,列) 跨批取 OR。

    原 core/data_fetcher._merge_window_masks (2026-07-18 假死事件修复), 逻辑
    一字不动, 改名公开。各批 mask 列天然互斥, 逐批 reindex 到并集时间轴
    (缺口填 False) 再 axis=1 拼列即等价 —— 秒级完成, 内存峰值从 ~5GB 降到
    ~70MB。兜底: 批间列重叠或批内重复时间戳时退回 groupby 慢速路径保正确性。
    """
    col_total = sum(len(m.columns) for m in mask_frames)
    col_uniq = len({c for m in mask_frames for c in m.columns})
    fast_ok = (col_total == col_uniq) and not any(
        m.index.has_duplicates for m in mask_frames
    )
    if fast_ok:
        union_idx = mask_frames[0].index
        for m in mask_frames[1:]:
            union_idx = union_idx.union(m.index)
        return pd.concat(
            [m.reindex(union_idx, fill_value=False) for m in mask_frames],
            axis=1,
        ).sort_index().fillna(False).astype(bool)
    logger.warning("窗口 mask 批间列重叠/批内重复时间戳, 退回 groupby 慢速合并")
    window_mask = pd.concat(mask_frames, axis=0)
    return window_mask.groupby(level=0).max().sort_index().fillna(False)


def compute_window_bounds(
    selections: pd.DataFrame,
    window_trading_days: int,
    trading_days: Optional[List[pd.Timestamp]] = None,
    end_time: Optional[str] = None,
    *,
    calendar_fetcher=None,
) -> tuple:
    """每只股的稀疏窗口 [窗口起, 窗口止] = [最早信号日, 最晚信号日+N 交易日]。

    原 DataFetcher.compute_window_bounds 全文搬入, 唯一改动: 拉日历的 TDX 调用
    换成注入的 calendar_fetcher 回调 (None → 退化自然日估算), 消除 TDX 传递依赖。

    calendar_fetcher: (start_str, end_str) -> List[pd.Timestamp], 契约: **必须返回
    已升序去重的 Timestamp 列表** (内部 _window_end 用 bisect.bisect_left 依赖
    有序, 乱序会静默算错窗口终点)。薄壳传的 DataFetcher.get_trading_days 本身
    已排序去重, 满足契约。

    Returns:
        (win_start, win_end): 两个 dict {stock_code: pd.Timestamp}。
    """
    sel = selections.copy()
    sel["select_date"] = pd.to_datetime(sel["select_date"])
    sel["stock_code"] = sel["stock_code"].apply(
        lambda c: nl[0] if (nl := normalize_list([c])) else c
    )

    # 每只股的窗口起点 = 最早信号日; 窗口需覆盖到 最晚信号日 + N 交易日
    first_sig = sel.groupby("stock_code")["select_date"].min()
    last_sig = sel.groupby("stock_code")["select_date"].max()

    if trading_days is None:
        global_start = first_sig.min()
        global_end = last_sig.max()
        # 拉全区间交易日历 (往后多留 window+10 天缓冲, 保证末批窗口能推满)
        cal_end = (global_end + pd.Timedelta(days=int(window_trading_days * 1.7) + 20))
        if calendar_fetcher is not None:
            trading_days = calendar_fetcher(
                global_start.strftime("%Y%m%d"), cal_end.strftime("%Y%m%d")
            )
        if not trading_days:
            logger.warning("交易日历为空, 稀疏窗口退化为按自然日估算窗口")
            trading_days = None

    def _window_end(sig_date: pd.Timestamp) -> pd.Timestamp:
        """信号日往后 window_trading_days 个交易日的日期。"""
        if trading_days:
            idx = bisect.bisect_left(trading_days, sig_date)  # 第一个 >= sig_date 的交易日
            target = min(idx + window_trading_days, len(trading_days) - 1)
            return trading_days[target]
        # 无交易日历兜底: 自然日估算 (交易日≈自然日×5/7, 反推)
        return sig_date + pd.Timedelta(days=int(window_trading_days * 1.5) + 5)

    # 每只股的 [窗口起, 窗口止]
    win_start = {c: first_sig[c] for c in first_sig.index}
    win_end = {c: _window_end(last_sig[c]) for c in last_sig.index}
    # 请求区间终点截断 (end_time 可为非交易日); 钳制 win_end >= win_start 防倒置。
    if end_time:
        end_ts = pd.Timestamp(str(end_time))
        win_end = {c: max(win_start[c], min(w, end_ts)) for c, w in win_end.items()}
    return win_start, win_end
