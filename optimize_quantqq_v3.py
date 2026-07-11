"""QUANTQQ 参数优化 v3 — 2000 组精选 (扩大边界)

v1: 144 组 (4 参数)
v2: 864 组 (+ 时间参数)
v3: ~2000 组 (+ 阶梯止盈展开 + 条件时间展开 + 首日未达标 + 触发模式)
"""
import sys, os, json, time, logging, ast
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import numpy as np
from datetime import datetime
from itertools import product

from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from core.formula_runner import FormulaRunner
from backtest.engine import BacktestEngine
from backtest.stop_config import load_stop_config

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger('optimize_v3')

TdxConnector.ensure_connected()

START = '20200101'
END   = '20260706'
FORMULA = 'QUANTQQ'

# 1. 复用 v2 的选股 + K 线 (用 run_cached 避免重复拉)
logger.info(f'[{FORMULA}] 调 TDX 选股 {START}~{END}...')
t0 = time.time()
sel_df = FormulaRunner.run_stock_selection_with_dates(
    formula_name=FORMULA, formula_arg='',
    stock_list=None, start_time=START, end_time=END,
    stock_period='1d', dividend_type=1,
)
logger.info(f'信号: {len(sel_df):,}, 股票: {sel_df["stock_code"].nunique():,}, 用时 {time.time()-t0:.0f}s')

signal_codes = sel_df['stock_code'].unique().tolist()
logger.info(f'拉 K 线 {len(signal_codes)} 只...')
t0 = time.time()
k = DataFetcher.get_kline(signal_codes, START, END, dividend_type='front', period='1d')
close = k['Close'].sort_index()
high = k['High'].sort_index()
low = k['Low'].sort_index()
logger.info(f'K 线: {close.shape[0]} 天 × {close.shape[1]} 只, 用时 {time.time()-t0:.0f}s')

common = sorted(set(close.columns) & set(signal_codes))
c = close[common].ffill().bfill()
h = high.reindex(columns=common).ffill().bfill()
l = low.reindex(columns=common).ffill().bfill()

entries = pd.DataFrame(False, index=c.index, columns=c.columns)
for _, row in sel_df.iterrows():
    code, dt = row['stock_code'], pd.to_datetime(row['select_date'])
    if code not in entries.columns: continue
    if dt in entries.index:
        entries.loc[dt, code] = True
    else:
        mask = entries.index >= dt
        if mask.any(): entries.loc[entries.index[mask][0], code] = True

c_np = c.values.astype(np.float64)
h_np = h.values.astype(np.float64)
l_np = l.values.astype(np.float64)
e_np = entries.values

# =========================================================================
# 2. 参数网格 v3 — 扩大边界
# =========================================================================
# 阶梯止盈: 5 档 (2 档盈利点 × 不同比例组合, 避免 n_ladder 变化)
LADDER_LEVELS = [
    # 基础 (2 档 - v2 已扫)
    [{'profit': 0.06, 'sell_ratio': 0.30}, {'profit': 0.15, 'sell_ratio': 0.30}],
    [{'profit': 0.08, 'sell_ratio': 0.40}, {'profit': 0.20, 'sell_ratio': 0.40}],
    # 新增: 利润点更低
    [{'profit': 0.04, 'sell_ratio': 0.30}, {'profit': 0.10, 'sell_ratio': 0.30}],
    # 新增: 利润点更高
    [{'profit': 0.10, 'sell_ratio': 0.35}, {'profit': 0.25, 'sell_ratio': 0.35}],
    # 新增: 比例更大
    [{'profit': 0.05, 'sell_ratio': 0.50}, {'profit': 0.12, 'sell_ratio': 0.50}],
]
# 条件时间止盈: 6 档 (3 天数 × 2 盈利)
COND_TIME_CFGS = [
    {'days': 5,  'profit': 0.02},  # 5d/2%
    {'days': 7,  'profit': 0.02},  # 7d/2% (用户提到的原值)
    {'days': 10, 'profit': 0.03},  # 10d/3% (v2 最优)
    {'days': 10, 'profit': 0.05},  # 10d/5%
    {'days': 15, 'profit': 0.03},  # 15d/3%
    {'days': 15, 'profit': 0.05},  # 15d/5%
]
# 时间止损: 4 档
TIME_STOP_DAYS = [15, 20, 30, 40]
# 首日未达标: 2 档 (关 + 5%)
FIRST_DAY_CFGS = [
    {'enabled': False, 'target': 0.0},   # 关
    {'enabled': True,  'target': 0.05},  # 5%
]
# 触发模式: 2 档 (新参数, 决定 trailing 触发用 Low 还是 Close)
# 注意: 当前 engine 没有这个开关, 这是新增需求. 暂用 priority 代替
# (trailing_first ≈ Low 触发, ladder_tp_first ≈ Close 触发)
# 实际新触发模式需要改 engine, 这次不扫, 用现有 priority 2 值

GRID = {
    'activation':      [0.025, 0.035, 0.05, 0.065],
    'drawdown':        [0.005, 0.01, 0.02],
    'cost_stop':       [-0.08, -0.12, -0.15],
    'ladder_levels':   LADDER_LEVELS,
    'time_stop_days':  TIME_STOP_DAYS,
    'cond_time_cfg':   COND_TIME_CFGS,
    'first_day_cfg':   FIRST_DAY_CFGS,
}
priorities = ['trailing_first', 'ladder_tp_first']
# 4 × 3 × 3 × 5 × 4 × 6 × 2 × 2 = 17,280 组合 (太多)
# 抽样: 阶梯止盈 5 档 × 其它参数 = 4×3×3×4×6×2×2 = 3,456 组合
# 再抽样 60% = ~2,000 组
import random
random.seed(42)

all_combos = list(product(
    GRID['activation'], GRID['drawdown'], GRID['cost_stop'],
    GRID['ladder_levels'], GRID['time_stop_days'], GRID['cond_time_cfg'],
    GRID['first_day_cfg'], priorities,
))
logger.info(f'全量参数组合: {len(all_combos)} 组')
# 抽样 (保留 4 个核心参数的所有组合, 只在时间维度抽样)
# 核心 = 激活线/回撤/硬止损/阶梯档位(用 tuple 表示因 dict 不可哈希)
def ladder_key(lv):
    return tuple((l['profit'], l['sell_ratio']) for l in lv)

core_keys = set()
for combo in all_combos:
    core_keys.add((combo[0], combo[1], combo[2], ladder_key(combo[3])))

# 抽样: 对每个核心组合, 随机抽 1-2 个时间参数组合
combos = []
for core in core_keys:
    matched = [combo for combo in all_combos if (combo[0], combo[1], combo[2], ladder_key(combo[3])) == core]
    if len(matched) > 3:
        sampled = random.sample(matched, min(3, len(matched)))
    else:
        sampled = matched
    combos.extend(sampled)

logger.info(f'抽样后: {len(combos)} 组')

# =========================================================================
# 3. 跑回测
# =========================================================================
ENGINE_CFG = {
    'initial_capital': 1000000.0,
    'commission': 0.0003,
    'slippage': 0.001,
    'period': '1d',
    'position_sizing': {
        'min_buy_amount': 2000.0, 'max_buy_amount': 20000.0,
        'lot_size': 100, 'min_lots': 1,
    },
}

base_cfg = load_stop_config()
results = []
t_total = time.time()

for i, (act, dd, cs, lv, mhd, ctcfg, fdcfg, pri) in enumerate(combos, 1):
    cfg = {**base_cfg, 'priority': pri,
           'cost_stop': {'enabled': True, 'threshold': cs},
           'trailing_stop': {'enabled': True, 'activation': act, 'drawdown': dd},
           'ladder_tp': {'enabled': True, 'levels': lv},
           'time_stop': {'enabled': True, 'max_hold_days': mhd},
           'cond_time_stop': {'enabled': True, 'days': ctcfg['days'], 'profit': ctcfg['profit']},
           'first_day': {'enabled': fdcfg['enabled'], 'target': fdcfg['target']}}
    levels = lv
    ladder_profits = np.array([l['profit'] for l in levels], dtype=np.float64)
    ladder_ratios = np.array([l['sell_ratio'] for l in levels], dtype=np.float64)
    n_ladder = len(levels)

    try:
        engine = BacktestEngine(ENGINE_CFG)
        # 2026-07-06: DEBUG
        if i <= 2:
            print(f'[DEBUG-{i}] close type={type(c)}, entries type={type(entries)}, h_np shape={h_np.shape if hasattr(h_np, "shape") else "?"}, sel_df type={type(sel_df)}')
            print(f'[DEBUG-{i}] ladder_profits={ladder_profits}, ladder_ratios={ladder_ratios}, n_ladder={n_ladder}')
        result = engine.run_cached(
            close=c, entries=entries, high_np=h_np, low_np=l_np,
            stop_config=cfg, selections=sel_df,
            ladder_profits=ladder_profits, ladder_ratios=ladder_ratios,
            n_ladder=n_ladder, skip_sm=True,
        )
        m = result['metrics']
        ladder_str = '+'.join([f"{l['profit']*100:.0f}%/{l['sell_ratio']*100:.0f}%" for l in levels])
        results.append({
            'idx': i,
            'activation': act,
            'drawdown': dd,
            'cost_stop': cs,
            'ladder': ladder_str,
            'time_stop_days': mhd,
            'cond_time': f"{ctcfg['days']}d/{ctcfg['profit']*100:.1f}%",
            'first_day': f"{fdcfg['enabled']}/{fdcfg['target']*100:.0f}%",
            'priority': pri,
            'cum_ret': m.get('cumulative_return', 0) * 100,
            'ann_ret': m.get('annualized_return', 0) * 100,
            'max_dd': m.get('max_drawdown', 0) * 100,
            'sharpe': m.get('sharpe_ratio', 0),
            'win_rate': m.get('win_rate', 0) * 100,
            'plr': m.get('profit_loss_ratio', 0),
            'n_trades': len(result['trades']),
        })
    except Exception as e:
        import traceback
        logger.warning(f'  [{i}/{len(combos)}] 失败: {e}')
        if i <= 3:  # 只对前 3 个失败详细打印
            traceback.print_exc()

    if i % 100 == 0:
        elapsed = time.time() - t_total
        rate = i / elapsed if elapsed > 0 else 0
        eta = (len(combos) - i) / rate if rate > 0 else 0
        logger.info(f'  [{i}/{len(combos)}] 用时 {elapsed:.0f}s, 预计还需 {eta:.0f}s')

# =========================================================================
# 4. 输出报告
# =========================================================================
df = pd.DataFrame(results)
df = df.sort_values('ann_ret', ascending=False).reset_index(drop=True)

logger.info('=' * 100)
logger.info(f'  QUANTQQ 参数优化报告 v3 — {START[:4]}.{START[4:6]}.{START[6:]} ~ {END[:4]}.{END[4:6]}.{END[6:]}')
logger.info(f'  抽样后 {len(combos)} 组, 用时 {time.time()-t_total:.0f}s')
logger.info('=' * 100)

logger.info(f'\n【全量统计】')
logger.info(f'  跑成功: {len(df)}/{len(combos)} 组')
logger.info(f'  年化最高: {df["ann_ret"].max():.2f}%')
logger.info(f'  年化最低: {df["ann_ret"].min():.2f}%')
logger.info(f'  年化中位: {df["ann_ret"].median():.2f}%')
logger.info(f'  年化均值: {df["ann_ret"].mean():.2f}%')
logger.info(f'  平均最大回撤: {df["max_dd"].mean():.2f}%')
logger.info(f'  平均夏普: {df["sharpe"].mean():.2f}%')
logger.info(f'  年化 >= 25%: {(df["ann_ret"]>=25).sum()} 组')
logger.info(f'  年化 >= 22%: {(df["ann_ret"]>=22).sum()} 组')
logger.info(f'  年化 >= 20%: {(df["ann_ret"]>=20).sum()} 组')

logger.info(f'\n【Top 15 配置 (按年化排序)】')
logger.info(f'{"#":<3} {"激活线":<7} {"回撤":<7} {"硬止损":<8} {"阶梯档位":<22} {"时间":<5} {"条件时间":<10} {"首日":<10} {"优先级":<16} {"年化%":<7} {"回撤%":<7} {"夏普":<6} {"交易":<5}')
for _, r in df.head(15).iterrows():
    logger.info(f'{r.name+1:<3} {r["activation"]:<7.3f} {r["drawdown"]:<7.3f} {r["cost_stop"]:<8.2f} {r["ladder"]:<22} {r["time_stop_days"]:<5} {r["cond_time"]:<10} {r["first_day"]:<10} {r["priority"]:<16} {r["ann_ret"]:<7.2f} {r["max_dd"]:<7.2f} {r["sharpe"]:<6.2f} {r["n_trades"]:<5.0f}')

# 年化 >= 25% 的配置
df_25 = df[df['ann_ret'] >= 25]
logger.info(f'\n【年化 >= 25% 的配置: {len(df_25)} 组】')
if len(df_25) > 0:
    logger.info(f'{"#":<3} {"激活线":<7} {"回撤":<7} {"硬止损":<8} {"阶梯档位":<22} {"时间":<5} {"条件时间":<10} {"首日":<10} {"优先级":<16} {"年化%":<7} {"回撤%":<7} {"夏普":<6} {"交易":<5}')
    for _, r in df_25.head(20).iterrows():
        logger.info(f'{r.name+1:<3} {r["activation"]:<7.3f} {r["drawdown"]:<7.3f} {r["cost_stop"]:<8.2f} {r["ladder"]:<22} {r["time_stop_days"]:<5} {r["cond_time"]:<10} {r["first_day"]:<10} {r["priority"]:<16} {r["ann_ret"]:<7.2f} {r["max_dd"]:<7.2f} {r["sharpe"]:<6.2f} {r["n_trades"]:<5.0f}')

# 单变量分析
for k, label in [
    ('activation','移动止盈激活线'),
    ('drawdown','移动止盈回撤'),
    ('cost_stop','硬止损'),
    ('time_stop_days','时间止损天数'),
    ('cond_time','条件时间止盈'),
    ('first_day','首日未达标'),
]:
    logger.info(f'\n按 {label} 分组:')
    for v, g in df.groupby(k):
        logger.info(f'  {v}: 年化 {g["ann_ret"].mean():.2f}%, 夏普 {g["sharpe"].mean():.2f}, 交易 {g["n_trades"].mean():.0f}')

# 保存
out_dir = 'output/optimize'
os.makedirs(out_dir, exist_ok=True)
ts = datetime.now().strftime('%Y%m%d_%H%M%S')
df.to_csv(f'{out_dir}/quantqq_v3_{ts}.csv', index=False)
df.head(10).to_csv(f'{out_dir}/quantqq_v3_top10_{ts}.csv', index=False)
logger.info(f'\n全量结果: {out_dir}/quantqq_v3_{ts}.csv')
logger.info(f'Top 10: {out_dir}/quantqq_v3_top10_{ts}.csv')