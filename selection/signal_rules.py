"""信号层规则 (2026-07-23 公式批量排名任务)。

filter_first_signal_in_window: 30 日滚动窗口首信号过滤 — 一只票近 N 个
交易日内已有过信号 (无论当时是否成交/是否被保留), 本次信号丢弃不买。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd


def filter_first_signal_in_window(
    selections: pd.DataFrame,
    window_td: int = 30,
    real_start: Optional[str] = None,
    calendar=None,
) -> pd.DataFrame:
    """每只票只保留"前 window_td 个交易日内无同票信号"的信号。

    距离按交易日历索引差计算: 当前信号与上一信号 (不论其自身是否被保留)
    的日历距离 > window_td 才放行。

    Args:
        selections: DataFrame, 至少含 [stock_code, select_date] 两列
        window_td: 冷却窗口 (交易日)
        real_start: 可选 (YYYYMMDD/Timestamp) — 规则应用完再丢弃早于该日的
            信号。调用方用 padded 区间取信号以覆盖窗口回溯期, 再裁回真实起点,
            保证回测前 window_td 个交易日内的历史信号也能压制开局信号。
        calendar: 有序交易日历 (可 pd.to_datetime); None 时用 selections
            自身的 select_date 去重排序代替 (测试/降级路径)

    Returns:
        过滤后的 DataFrame (保留原列, 按 stock_code/select_date 排序, 重置索引)
    """
    if selections is None or len(selections) == 0:
        return selections

    df = selections.copy()
    df["select_date"] = pd.to_datetime(df["select_date"])

    if calendar is None:
        cal = pd.DatetimeIndex(sorted(df["select_date"].unique()))
    else:
        cal = pd.DatetimeIndex(pd.to_datetime(list(calendar)))
        cal = cal.sort_values().unique()

    # 日期 → 日历序号 (searchsorted: 非交易日信号取插入位, 单调递增即可比较距离)
    df["_pos"] = cal.searchsorted(df["select_date"].values)
    df = df.sort_values(["stock_code", "select_date"])

    prev_pos = df.groupby("stock_code")["_pos"].shift(1)
    keep = prev_pos.isna() | ((df["_pos"] - prev_pos) > window_td)
    out = df[keep].drop(columns=["_pos"])

    if real_start is not None:
        rs = pd.to_datetime(str(real_start))
        out = out[out["select_date"] >= rs]

    return out.reset_index(drop=True)
