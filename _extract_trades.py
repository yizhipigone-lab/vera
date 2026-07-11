"""提取换股明细"""
import sys, os
sys.path.insert(0, '.')
import ast, pandas as pd, json

with open('output/results/ab_compare/quantqq_ab_compare_20260706_094815.json','r',encoding='utf-8') as f:
    data = json.load(f)

# trades 字段是嵌套字符串, 用 ast.literal_eval 解析
trades_str = data['B_trailing_first']['trades']
print(f'trades_str 类型: {type(trades_str)}, 长度: {len(trades_str)}')
print(f'前 200 字符: {trades_str[:200]}')
print()

# 尝试用 json.loads 失败的话, 用 ast.literal_eval
try:
    trades_raw = json.loads(trades_str)
    print('用 json.loads 成功')
except Exception as e:
    print(f'json.loads 失败: {e}')
    try:
        trades_raw = ast.literal_eval(trades_str)
        print('用 ast.literal_eval 成功')
    except Exception as e2:
        print(f'ast.literal_eval 也失败: {e2}')
        # 最后一个办法: 用正则或 strip pandas 的格式
        import re
        lines = trades_str.strip().split('\n')
        print(f'行数: {len(lines)}')
        print(f'第一行: {lines[0]}')
        print(f'第二行: {lines[1]}')
        print(f'最后行: {lines[-1]}')

# 不管哪种, 直接看 trades_raw 是否为 list
if 'trades_raw' in dir():
    print(f'trades_raw 类型: {type(trades_raw)}, 长度: {len(trades_raw) if hasattr(trades_raw, "__len__") else "?"}')
    if isinstance(trades_raw, list) and len(trades_raw) > 0:
        print(f'第一行 keys: {list(trades_raw[0].keys())}')
        print(f'第一行: {trades_raw[0]}')