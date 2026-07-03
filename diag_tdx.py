"""TDX 选股连通性诊断脚本 — 逐层测试选股链路。"""
import sys, os
TDX_HOME = os.environ.get('TDX_HOME', r'E:\NEW_TDX')
sys.path.insert(0, os.path.join(TDX_HOME, r"PYPlugins\user"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tqcenter import tq

# 1. 初始化连接
print("=" * 60)
print("Step 1: 初始化 TDX 连接")
print("=" * 60)
tq.initialize(os.path.join(TDX_HOME, r"PYPlugins\user\tqcenter.py"))
print(f"  run_id={tq.run_id}, initialized={tq._initialized}")

# 2. 获取股票列表
print("\nStep 2: 获取沪深A股列表")
all_stocks = tq.get_stock_list("50", list_type=1)
print(f"  股票数量: {len(all_stocks)}")
if all_stocks:
    print(f"  前5只: {all_stocks[:5]}")

# 3. 测试 formula_process_mul_xg — 方式1: return_date=True
print("\nStep 3: 测试 formula_process_mul_xg (return_date=True)")
result1 = tq.formula_process_mul_xg(
    formula_name="UPN",
    formula_arg="3",
    return_count=0,
    return_date=True,
    stock_list=all_stocks[:20],  # 只用20只测试
    stock_period="1d",
    start_time="20240601",
    end_time="20240630",
    dividend_type=1,
)
print(f"  ErrorId={result1.get('ErrorId', 'MISSING')}")
print(f"  keys count={len(result1)}")
# 打印前3个有结果的股票
found = 0
for k, v in result1.items():
    if k == "ErrorId":
        continue
    if v:
        print(f"  {k}: {type(v).__name__} = {str(v)[:200]}")
        found += 1
        if found >= 3:
            break
if found == 0:
    print("  无选股结果！")

# 4. 测试 formula_process_mul_xg — 方式2: return_date=False (官方示例方式)
print("\nStep 4: 测试 formula_process_mul_xg (return_date=False, 官方示例)")
result2 = tq.formula_process_mul_xg(
    formula_name="UPN",
    formula_arg="3",
    return_count=1,
    return_date=False,
    stock_list=all_stocks[:20],
    stock_period="1d",
    count=100,
    dividend_type=1,
)
print(f"  ErrorId={result2.get('ErrorId', 'MISSING')}")
print(f"  keys count={len(result2)}")
# 打印前3个
found = 0
for k, v in result2.items():
    if k == "ErrorId":
        continue
    print(f"  {k}: {type(v).__name__} = {str(v)[:300]}")
    found += 1
    if found >= 3:
        break

# 5. 全量测试（官方示例方式）
print("\nStep 5: 全量选股 (return_date=False, 全部A股)")
result3 = tq.formula_process_mul_xg(
    formula_name="UPN",
    formula_arg="3",
    return_count=1,
    return_date=False,
    stock_list=all_stocks,
    stock_period="1d",
    count=100,
    dividend_type=1,
)
error_id = result3.get("ErrorId", "MISSING")
print(f"  ErrorId={error_id}")
upn_stocks = []
for k, v in result3.items():
    if k == "ErrorId":
        continue
    if v and isinstance(v, dict):
        # 检查 UPN 指标值
        for indicator_name, values in v.items():
            if values and len(values) > 0 and str(values[-1]) == '1':
                upn_stocks.append(k)
                break
    elif v and isinstance(v, list):
        if len(v) > 0 and str(v[-1]) == '1':
            upn_stocks.append(k)
print(f"  符合条件股票数: {len(upn_stocks)}")
if upn_stocks:
    print(f"  前10只: {upn_stocks[:10]}")

tq.close()
print("\n诊断完成")
