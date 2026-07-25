"""L2 按日缓存 e2e 实测 v2 (2026-07-26): 真实 TDX 全A QUANTQQ 1d。

策略 (实测修正): 任一缺失 → 整段一次重算 (与直跑同价); 价值在按天命中。
验证:
1. run1 全冷 (20260601~0630): 全价
2. run2 扩区间 (~0724): 整段重算, 与直跑同价 (≤1.3x), parity 逐条一致
3. run3 子区间 (0605~0620, 并集覆盖): 零公式调用, 亚秒级
4. run4 同 run2 区间再跑: 全命中, 亚秒级
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from core.connector import TdxConnector
from core.formula_runner import FormulaRunner
from selection.selector import StockSelector

SEL_CFG = {"formula_name": "QUANTQQ", "formula_arg": "",
           "universe": {"type": "50", "exclude_st": True},
           "period": "1d", "dividend_type": 1}


def main():
    TdxConnector.initialize()
    try:
        sel = StockSelector(SEL_CFG)
        t0 = time.perf_counter()
        r1 = sel.run(start_time="20260601", end_time="20260630")
        t1 = time.perf_counter() - t0
        print(f"run1 (0601~0630, 全冷):        {t1:6.1f}s  {len(r1)} 条")

        t0 = time.perf_counter()
        r2 = sel.run(start_time="20260601", end_time="20260724")
        t2 = time.perf_counter() - t0
        print(f"run2 (0601~0724, 扩区间重算):  {t2:6.1f}s  {len(r2)} 条")

        t0 = time.perf_counter()
        r3 = sel.run(start_time="20260605", end_time="20260620")
        t3 = time.perf_counter() - t0
        print(f"run3 (0605~0620, 子区间命中):  {t3:6.2f}s  {len(r3)} 条")

        t0 = time.perf_counter()
        r4 = sel.run(start_time="20260601", end_time="20260724")
        t4 = time.perf_counter() - t0
        print(f"run4 (=run2, 全命中):          {t4:6.2f}s  {len(r4)} 条")

        pool = StockSelector(SEL_CFG).resolve_universe()
        t0 = time.perf_counter()
        direct = FormulaRunner.run_stock_selection_with_dates(
            formula_name="QUANTQQ", formula_arg="", stock_list=pool,
            start_time="20260601", end_time="20260724",
            stock_period="1d", dividend_type=1)
        t5 = time.perf_counter() - t0
        print(f"直跑 (0601~0724 对照):        {t5:6.1f}s  {len(direct)} 条")

        a = r2[["stock_code", "select_date"]].sort_values(
            ["select_date", "stock_code"]).reset_index(drop=True)
        b = direct[["stock_code", "select_date"]].sort_values(
            ["select_date", "stock_code"]).reset_index(drop=True)
        same = (a["stock_code"].tolist() == b["stock_code"].tolist()
                and a["select_date"].tolist() == b["select_date"].tolist())
        print(f"\nparity (run2 vs 直跑): {same}")
        print(f"run2/直跑 = {t2/t5:.2f}  run3 子区间 {t3:.2f}s  run4 全命中 {t4:.2f}s")
        assert same, "L2 结果与直跑不一致!"
        assert t2 < t5 * 1.3, f"重算不应显著慢于直跑: {t2:.1f} vs {t5:.1f}"
        assert t3 < 2.0, f"子区间命中应亚秒级: {t3:.2f}s"
        assert t4 < 2.0, f"全命中应亚秒级: {t4:.2f}s"
        print("L2 E2E PASS")
    finally:
        TdxConnector.close()


if __name__ == "__main__":
    main()
