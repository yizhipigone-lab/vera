"""诊断: 黑马起步 5m 选股的 count 覆盖范围 + TDX 连通 + 公式存在。

回答三个问题:
  1. TDX 客户端在线吗?
  2. "黑马起步" 公式在 TDX 里存在吗?
  3. count=3000 (formula_runner 硬编码, 日线口径) 在 5m 下实际覆盖多久?
     若 5m 信号只集中在最近 ~2.5 月而日线覆盖 2019-2026, 即确认 5m 信号被截断。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.connector import TdxConnector
from core.formula_runner import FormulaRunner

print("=== 1. TDX 连接 ===", flush=True)
try:
    TdxConnector.initialize()
    print("TDX 连接 OK", flush=True)
except Exception as e:
    print(f"TDX 连接失败: {e}", flush=True)
    sys.exit(1)

# 创业板元老股 (上市早, 2019 有完整数据)
TEST_CODES = ['300001.SZ', '300002.SZ', '300003.SZ', '300004.SZ', '300005.SZ']

for period in ['5m', '1d']:
    print(f"\n=== {period}: 黑马起步 20190601~20260710, {len(TEST_CODES)} 只创业板 ===", flush=True)
    try:
        df = FormulaRunner.run_stock_selection_with_dates(
            formula_name='黑马起步',
            formula_arg='',
            stock_list=TEST_CODES,
            start_time='20190601',
            end_time='20260710',
            stock_period=period,
        )
        if df.empty:
            print("  信号为空 (公式无命中 / 公式名错 / arg 缺失)", flush=True)
        else:
            print(f"  信号数: {len(df)}", flush=True)
            print(f"  日期范围: {df['select_date'].min()} ~ {df['select_date'].max()}", flush=True)
            print(f"  涉及股票: {df['stock_code'].nunique()} 只", flush=True)
            print("  按年分布:", flush=True)
            print(df.groupby(df['select_date'].dt.year).size().to_string(), flush=True)
    except Exception as e:
        print(f"  失败: {e}", flush=True)

try:
    TdxConnector.close()
except Exception:
    pass
print("\n=== 诊断完成 ===", flush=True)
