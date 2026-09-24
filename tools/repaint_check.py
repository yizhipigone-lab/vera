# -*- coding: utf-8 -*-
"""信号重画 (repaint) 检测 (2026-07-25) — 未来函数的决定性判定。

原理: 因果公式在日期 D 之前的信号, 不随"未来数据加入"而改变。
以 end=D1 截断跑一次选股, 再以 end=今天跑全量, 比较共同时段的信号集合:
  - 截断窗口边缘的信号若在两次运行间消失/新增 → 信号被未来数据改写 → 重画
  - 完全一致 → 因果公式 (移位测试的衰减属"时点敏感型合法策略")

比较窗口: 截断日前 N 个交易日 (重画发生在窗口边缘, 深处信号两法一致)。

用法: python tools/repaint_check.py --formulas 超赢王牛股,绝底 --cutoff 20260630
"""
import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from core.connector import TdxConnector  # noqa: E402
from core.data_fetcher import DataFetcher  # noqa: E402
from selection.selector import StockSelector  # noqa: E402

PAD_DAYS = 70
#: 重画判定阈值: 不一致率 > 2% = 重画实锤 (farm_verify 复核判定引用本常量, F5)
REPAINT_MAX_RATE = 0.02


def select(formula, stocks, start, end):
    cfg = {"formula_name": formula, "formula_arg": "",
           "universe": {"type": "5", "exclude_st": True},
           "period": "1d", "dividend_type": 1}
    df = StockSelector(cfg).run(start_time=start, end_time=end,
                                stock_list=stocks)
    if df is None or len(df) == 0:
        return set()
    return set(zip(df["stock_code"],
                   pd.to_datetime(df["select_date"]).dt.strftime("%Y%m%d")))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--formulas", required=True)
    ap.add_argument("--cutoff", required=True, help="截断日 YYYYMMDD")
    ap.add_argument("--edge-days", type=int, default=15,
                    help="比较截断日前 N 个交易日的信号")
    ap.add_argument("--start", default="20260101")
    ap.add_argument("--end", default=pd.Timestamp.now().strftime("%Y%m%d"))
    args = ap.parse_args()

    TdxConnector.initialize()
    stocks = StockSelector({"formula_name": "_", "universe": {
        "type": "5", "exclude_st": True}}).resolve_universe()
    print(f"[INFO] 股票池 {len(stocks)} 只, 截断日 {args.cutoff}")

    padded_start = (pd.Timestamp(args.start) - pd.Timedelta(days=PAD_DAYS)
                    ).strftime("%Y%m%d")
    cal = pd.DatetimeIndex(pd.to_datetime(DataFetcher.get_calendar_days(
        "SH", start_time=padded_start, end_time=args.end)))
    cut = pd.Timestamp(args.cutoff)
    ci = cal.searchsorted(cut)
    edge_lo = cal[max(0, ci - args.edge_days)].strftime("%Y%m%d")
    print(f"[INFO] 比较窗口 {edge_lo} ~ {args.cutoff} "
          f"(截断日前 {args.edge_days} 个交易日)")

    for f in [s.strip() for s in args.formulas.split(",") if s.strip()]:
        print(f"[INFO] {f} 截断选股 (end={args.cutoff})...")
        a = select(f, stocks, padded_start, args.cutoff)
        print(f"[INFO] {f} 全量选股 (end={args.end})...")
        b = select(f, stocks, padded_start, args.end)
        a_edge = {s for s in a if edge_lo <= s[1] <= args.cutoff}
        b_edge = {s for s in b if edge_lo <= s[1] <= args.cutoff}
        vanished = a_edge - b_edge   # 截断时有、全量时没了 = 被未来数据抹掉
        appeared = b_edge - a_edge   # 截断时没有、全量时新增 = 事后补画
        total = len(a_edge | b_edge) or 1
        rate = (len(vanished) + len(appeared)) / total
        print(f"  {f}: 窗口信号 截断={len(a_edge)} 全量={len(b_edge)} "
              f"消失={len(vanished)} 新增={len(appeared)} "
              f"不一致率={rate*100:.2f}%")
        for s in sorted(vanished)[:5]:
            print(f"    消失: {s[0]} {s[1]}")
        for s in sorted(appeared)[:5]:
            print(f"    新增: {s[0]} {s[1]}")
        verdict = ("重画实锤 (不一致率>2%)" if rate > REPAINT_MAX_RATE
                   else "边缘微差" if rate > 0 else "完全一致, 因果公式")
        print(f"  → {verdict}")

    TdxConnector.close()


if __name__ == "__main__":
    main()
