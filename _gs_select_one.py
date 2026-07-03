"""
gs_txt 单公式选股子脚本 (被 run_gs_txt_batch_v2.py 用 subprocess 调用)

用法:
    python _gs_select_one.py <formula_name> <start> <end>

输出 (stdout JSON):
    {"status": "ok", "records": [{"stock_code": "...", "select_date": "YYYYMMDD"}, ...]}
    {"status": "no_signals", "records": []}
    {"status": "error", "msg": "..."}

设计目的:
    - 独立进程, 被 subprocess.run(timeout=300) 调用
    - 超时被 kill, 不会卡死主进程
    - 主进程预取 K 线后, 此脚本独立连 TDX 跑选股
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
from core.connector import TdxConnector
from core.formula_runner import FormulaRunner


def main():
    if len(sys.argv) < 4:
        print(json.dumps({'status': 'error', 'msg': 'usage: formula start end'}))
        return
    formula = sys.argv[1]
    start = sys.argv[2]
    end = sys.argv[3]

    try:
        TdxConnector.ensure_connected()
        sel = FormulaRunner.run_stock_selection_with_dates(
            formula_name=formula, formula_arg='',
            stock_list=None, start_time=start, end_time=end,
            stock_period='1d', dividend_type=1,
        )
        if sel is None or len(sel) == 0:
            print(json.dumps({'status': 'no_signals', 'records': []}))
            return
        records = [{
            'stock_code': str(r['stock_code']),
            'select_date': pd.to_datetime(r['select_date']).strftime('%Y%m%d'),
        } for _, r in sel.iterrows()]
        print(json.dumps({'status': 'ok', 'records': records}))
    except Exception as e:
        print(json.dumps({'status': 'error', 'msg': f'{type(e).__name__}: {str(e)[:100]}'}))


if __name__ == '__main__':
    main()
