"""找与创业板指数「反着来」的 ETF —— 数据实证。

对一批候选 ETF 算与创业板指(399006)的日收益相关系数, 找出负相关最强者。
两个口径: 全区间相关系数 + 「创业板下跌日」的条件相关(创业板跌时它是否涨)。
数据源: TDX (DataFetcher), 窗口取 2019-07 至今 (覆盖所有候选上市后)。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from core.data_fetcher import DataFetcher

# 候选: (代码, 名称, 类别)
CANDIDATES = [
    ("518880.SH", "黄金ETF", "避险"),
    ("518800.SH", "黄金基金", "避险"),
    ("511010.SH", "国债ETF(5-10年)", "债券"),
    ("511260.SH", "十年国债ETF", "债券"),
    ("511090.SH", "30年国债ETF", "债券"),
    ("510880.SH", "红利ETF", "价值"),
    ("512890.SH", "红利低波ETF", "价值"),
    ("512800.SH", "银行ETF", "价值"),
    ("510050.SH", "上证50ETF", "大盘价值"),
    ("513100.SH", "纳指ETF", "海外"),
    ("513030.SH", "德国30ETF", "海外"),
    ("159985.SZ", "豆粕ETF", "商品"),
    ("162411.SZ", "华宝油气", "商品"),
    ("511990.SH", "华宝添益(货币)", "现金"),
    # 正相关对照 (应明显为正)
    ("159915.SZ", "创业板ETF", "成长(对照)"),
    ("512480.SH", "半导体ETF", "成长(对照)"),
]

INDEX_CODE = "399006.SZ"  # 创业板指
WINDOW_START = "20190701"


def main():
    codes = [c[0] for c in CANDIDATES]
    kl = DataFetcher.get_kline(codes + [INDEX_CODE], WINDOW_START, "20260818",
                               period="1d", dividend_type="front", use_cache=True)
    close = kl["Close"]
    idx = close[INDEX_CODE].dropna()

    rows = []
    for code, name, cat in CANDIDATES:
        if code not in close.columns:
            continue
        s = close[code].dropna()
        df = pd.concat([idx, s], axis=1, join="inner").dropna()
        df.columns = ["idx", "x"]
        if len(df) < 250:
            continue
        ri = df["idx"].pct_change().dropna()
        rx = df["x"].pct_change().dropna()
        df = pd.concat([ri, rx], axis=1).dropna()
        df.columns = ["idx", "x"]
        full_corr = df["idx"].corr(df["x"])
        # 创业板下跌日, X 的平均收益 (正=真能对冲, 负=一起跌)
        down = df[df["idx"] < 0]["x"]
        down_mean = down.mean() * 100
        down_win = (down > 0).mean() * 100  # 创业板跌日 X 上涨占比
        rows.append((name, cat, full_corr, down_mean, down_win, len(df)))

    rows.sort(key=lambda r: r[2])  # 按相关系数升序 (最负在前)
    print(f"窗口: {WINDOW_START} ~ 2026-08 (共 {len(df) if 'df' in dir() else '-'} 交易日/对)\n")
    print(f"{'名称':<16}{'类别':<8}{'全区间相关':>10}{'创业板跌日X均涨':>16}{'跌日X上涨占比':>14}")
    print("-" * 66)
    for name, cat, c, dm, dw, nd in rows:
        print(f"{name:<16}{cat:<8}{c:>+10.3f}{dm:>+15.2f}%{dw:>13.0f}%")


if __name__ == "__main__":
    main()
