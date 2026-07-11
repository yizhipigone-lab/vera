"""探索 TDX 市值/股本数据源, 确认怎么取每只股的市值用于分桶。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.connector import TdxConnector
TdxConnector.initialize()
from tqcenter import tq

print("=== 1. get_stock_info(300001.SZ) ===", flush=True)
try:
    info = tq.get_stock_info('300001.SZ')
    if isinstance(info, dict):
        for k, v in info.items():
            print(f"  {k}: {v}", flush=True)
    else:
        print(f"  type={type(info)}, val={info}", flush=True)
except Exception as e:
    print(f"  失败: {e}", flush=True)

print("\n=== 2. get_market_data 常见市值/股本字段 ===", flush=True)
for field in ['流通市值', '流通股本', '总市值', '总股本', ' circulating_value', 'circulating_market_cap', 'turnover', '换手率']:
    field = field.strip()
    try:
        r = tq.get_market_data(field_list=[field], stock_list=['300001.SZ'], start_time='20240102', end_time='20240105', period='1d')
        if r and field in r and r[field] is not None:
            try:
                val = r[field].dropna().iloc[0]
                print(f"  [{field}] 支持, 样例={val}", flush=True)
            except Exception:
                print(f"  [{field}] 支持(空)", flush=True)
        else:
            print(f"  [{field}] 不支持/空", flush=True)
    except Exception as e:
        print(f"  [{field}] 异常: {e}", flush=True)

print("\n=== 3. get_financial_data 股本字段 ===", flush=True)
for field in ['总股本', '流通股本', '流通A股', '市值', '流通市值', 'total_share', 'circulating_share']:
    try:
        r = tq.get_financial_data(stock_list=['300001.SZ'], field_list=[field], start_time='20230101', end_time='20231231')
        if r and r.get(field) is not None:
            print(f"  [{field}] 支持, 样例={r[field]}", flush=True)
        else:
            print(f"  [{field}] 不支持/空", flush=True)
    except Exception as e:
        print(f"  [{field}] 异常: {e}", flush=True)

TdxConnector.close()
print("\n=== 完成 ===", flush=True)
