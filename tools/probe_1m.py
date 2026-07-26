"""1m 周期可行性探针 (2026-07-26, 阶段0) — 只读, 不改生产代码。

回答三个问题:
1. TDX tqcenter 是否接受 period="1m" (不行就试 "1min"/"m1")
2. 数据完整性: 是否 240 根/日, 深度是否真从 2026-01 开始 (还是更深)
3. 成本: 单股拉取耗时 + 估算全市场 (~5000只) 缓存体积与拉取时间
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd

from core.connector import TdxConnector
from core.data_fetcher import DataFetcher

SAMPLES = ["600519.SH", "000001.SZ", "300750.SZ", "002594.SZ", "688981.SH"]
FIELDS = ["Open", "High", "Low", "Close", "Volume", "Amount"]


def try_fetch(codes, start, end, period):
    t0 = time.perf_counter()
    try:
        data = DataFetcher.get_kline(
            codes, start_time=start, end_time=end,
            period=period, dividend_type="front", fill_data=False,
            use_cache=False)
        dt = time.perf_counter() - t0
        if not data or "Close" not in data:
            return dt, None
        return dt, data
    except Exception as e:
        return time.perf_counter() - t0, e


def analyze(data, label):
    close = data["Close"]
    close.index = pd.to_datetime(close.index)
    print(f"\n== {label} ==")
    print(f"总行数: {len(close)}, 股票: {list(close.columns)}")
    print(f"时间范围: {close.index.min()} → {close.index.max()}")
    # 每日 bar 数分布
    per_day = close.groupby(close.index.date).size()
    print(f"每日 bar 数: min={per_day.min()} median={int(per_day.median())} "
          f"max={per_day.max()} 天数={len(per_day)}")
    # 每天首末 bar 时刻
    first_last = close.groupby(close.index.date).apply(
        lambda g: (g.index[0].strftime("%H:%M"), g.index[-1].strftime("%H:%M")))
    fl = pd.DataFrame(first_last.tolist(), columns=["首", "末"])
    print("首 bar 时刻分布:", fl["首"].value_counts().head(3).to_dict())
    print("末 bar 时刻分布:", fl["末"].value_counts().head(3).to_dict())
    # 体积估算: 单股单日内存占用
    one = close.iloc[:480, :1]
    est_bytes = one.memory_usage(deep=True).sum() if hasattr(one, 'memory_usage') else 0
    per_stock_day = close.memory_usage(deep=True).sum() / max(1, len(close.columns)) / max(1, len(per_day))
    print(f"单股单日内存 ≈ {per_stock_day/1024:.1f} KB (未压缩)")
    return per_day, per_stock_day


def main():
    TdxConnector.initialize()
    try:
        # 1. 周期参数探测
        print("=== Q1: TDX 是否接受 1m 周期 ===")
        period_ok = None
        for p in ("1m", "1min", "m1"):
            dt, data = try_fetch(SAMPLES[:1], "20260720", "20260724", p)
            if isinstance(data, Exception):
                print(f"  period={p!r}: 异常 {str(data)[:80]}")
            elif data is None:
                print(f"  period={p!r}: 无数据返回 ({dt:.1f}s)")
            else:
                print(f"  period={p!r}: OK ({dt:.1f}s)")
                period_ok = p
                break
        if not period_ok:
            print("!! 三种周期名都不行, 阶段0结论: TDX 不支持或需其他参数名")
            return

        # 2. 完整性: 近 5 个交易日, 5 只样本
        print(f"\n=== Q2: 数据完整性 (period={period_ok!r}) ===")
        dt, data = try_fetch(SAMPLES, "20260720", "20260724", period_ok)
        per_day, psd = analyze(data, "近5日 5只样本")

        # 3. 深度探测: 2026-01-01 之前有没有
        print("\n=== Q2b: 深度探测 (2025-10-01 起拉 600519) ===")
        dt, deep = try_fetch(["600519.SH"], "20251001", "20260724", period_ok)
        if isinstance(deep, Exception) or deep is None:
            print(f"  深拉失败/无数据: {deep if isinstance(deep, Exception) else 'None'}")
        else:
            c = deep["Close"]
            c.index = pd.to_datetime(c.index)
            print(f"  最早 bar: {c.index.min()}  最晚: {c.index.max()}  总行数: {len(c)}")

        # 4. 成本估算
        print("\n=== Q3: 成本与体积估算 ===")
        dt, one = try_fetch(["600519.SH"], "20260601", "20260724", period_ok)
        days = 39
        print(f"  单股 ~39 交易日拉取: {dt:.2f}s")
        print(f"  全市场 5000 只串行估算: {dt*5000/60:.0f} 分钟 (理想化)")
        # 体积: 按实测内存估 parquet (压缩比 ~3x)
        daily_kb = psd / 1024
        print(f"  单股单日 ≈ {daily_kb:.1f} KB 内存 → parquet 估 ~{daily_kb/3:.1f} KB")
        print(f"  全市场 5000 只 × 22 交易日/月 ≈ {daily_kb/3*5000*22/1024/1024:.2f} GB/月")
        print(f"  全市场 5000 只 × 7 个月 (2026-01至今) ≈ {daily_kb/3*5000*22*7/1024/1024:.1f} GB")
        print(f"  回测矩阵 (5000 股 × 240 bar × 22 天 × 8B × ~6 矩阵) "
              f"≈ {5000*240*22*8*6/1e9:.2f} GB/份")
    finally:
        TdxConnector.close()


if __name__ == "__main__":
    main()
