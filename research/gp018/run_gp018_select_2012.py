# -*- coding: utf-8 -*-
"""GUPIAO_018 选股 2012-2026 全区间 (为参数寻优补跑 2012-2018 段)

用户 2026-08-08 要求参数寻优目标年化≥30%, 区间扩到 2012-01-01 起。
现有缓存只到 2019, 故补跑。L2 信号按日缓存: 2019-2026 段应命中, 2012-2018 段补算入库。
选股只跑一次, 后续所有参数组合 (探测/贝叶斯) 复用此 selections。

口径不变: GUPIAO_018 / 全A非ST(type50) / 1d / 前复权(dividend_type=1)。
用法: python -X utf8 research/gp018/run_gp018_select_2012.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from selection.selector import StockSelector

FORMULA = "GUPIAO_018"
START = "20120101"
END = "20260808"
TAG = "gp018_1d_real_2012_2026"
OUT_DIR = "research/gp018/results"

SEL_CFG = {
    "formula_name": FORMULA,
    "formula_arg": "",
    "universe": {"type": "50", "exclude_st": True},   # 全A非ST
    "period": "1d",
    "dividend_type": 1,                                 # 前复权
}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    t0 = time.time()
    print(f"[INFO] 选股 {FORMULA} 全A非ST {START}->{END} (补 2012-2018 段)", flush=True)
    selections = StockSelector(SEL_CFG).run(start_time=START, end_time=END)
    n = 0 if selections is None else len(selections)
    print(f"[INFO] 信号数 = {n}  (选股耗时 {time.time()-t0:.0f}s)", flush=True)
    if not n:
        print("[FAIL] 无信号, 终止", flush=True)
        return

    selections.to_parquet(f"{OUT_DIR}/{TAG}_selections.parquet")

    # 逐年信号分布 (确认 2012-2018 段确实补上了)
    sd = pd.to_datetime(selections["select_date"])
    by_year = selections.groupby(sd.dt.year).size()
    print("[INFO] 逐年信号数:", flush=True)
    print(by_year.to_string(), flush=True)

    print(f"[OK] 落盘 {OUT_DIR}/{TAG}_selections.parquet  (总耗时 {time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
