"""拉取 TDX 服务端 QUANTQQ 信号并落盘 parquet (供基准复现与因果对比)。"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import pandas as pd
from selection.selector import StockSelector
from utils.config_loader import ConfigLoader

def run(start, end, out):
    sel_cfg = {"formula_name": "QUANTQQ", "formula_arg": "",
               "universe": {"type": "50", "exclude_st": True},
               "period": "1d", "dividend_type": 1}
    t0 = time.time()
    sel = StockSelector(sel_cfg).run(start_time=start, end_time=end)
    n = 0 if sel is None else len(sel)
    if n:
        sel.to_parquet(out)
    print(f"[DONE] {start}~{end} signals={n} stocks={0 if sel is None else sel['stock_code'].nunique()} "
          f"elapsed={time.time()-t0:.0f}s -> {out}", flush=True)

if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], sys.argv[3])
