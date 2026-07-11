"""QUANTQQ 参数优化器 — 2020.1.1 ~ 2026.7.6 全区间
扫 144 组参数组合, 找出年化 >= 25% 的最优配置.
"""
import sys, os, json, time, logging
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

logging.basicConfig(level=logging.WARNING)  # 减少 TDX 日志噪音
logger = logging.getLogger('optimize')

TdxConnector.ensure_connected()

START = '20200101'
END   = '20260706'  # 固定到 7/6, 避免每次跑时区不同
FORMULA = 'QUANTQQ'

# =========================================================================
# 1. 拉一次 TDX 选股 + K 线, 后续 144 组回测全部复用 (省时)
# =========================================================================
logger.info(f'[{FORMULA}] 调 TDX 选股 {START}~{END}...')
t0 = time.time()
sel_df = FormulaRunner.run_stock_selection_with_dates(
    formula_name=FORMULA, formula_arg='',
    stock_list=None, start_time=START, end_time=END,
    stock_period='1d', dividend_type=1,
)
logger.info(f'信号: {len(sel_df):,} 笔, 股票: {sel_df["stock_code"].nunique():,} 只, 用时 {time.time()-t0:.0f}s')

# 拉 K 线 (5200 只太慢, 用信号里出现的股票 ~487 只)
signal_codes = sel_df['stock_code'].unique().tolist()
logger.info(f'拉 K 线 {len(signal_codes)} 只...')
t0 = time.time()
k = DataFetcher.get_kline(signal_codes, START, END, dividend_type='front', period='1d')
close = k['Close'].sort_index()
high = k['High'].sort_index()
low = k['Low'].sort_index()
logger.info(f'K 线: {close.shape[0]} 天 × {close.shape[1]} 只, 用时 {time.time()-t0:.0f}s')

# 准备 entries 矩阵
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
# 2. 参数网格: 4个核心参数 × 2 priority = 144 组合
# =========================================================================
GRID = {
    'activation':      [0.025, 0.035, 0.05, 0.065],           # 4 档
    'drawdown':        [0.005, 0.01, 0.02],                    # 3 档
    'cost_stop':       [-0.08, -0.12, -0.15],                  # 3 档
    'ladder_levels':   [                                      # 2 档
        [{'profit': 0.06, 'sell_ratio': 0.30}, {'profit': 0.15, 'sell_ratio': 0.30}],
        [{'profit': 0.08, 'sell_ratio': 0.40}, {'profit': 0.20, 'sell_ratio': 0.40}],
    ],
    'time_stop_days':  [15, 20, 30],                          # 3 档 (新增)
    'cond_time_cfg':   [                                      # 2 档 (新增)
        {'days': 5,  'profit': 0.02},                        # 持仓 5 天 + 盈利 2%
        {'days': 10, 'profit': 0.03},                        # 持仓 10 天 + 盈利 3%
    ],
}
priorities = ['trailing_first', 'ladder_tp_first']
# 4 × 3 × 3 × 2 × 3 × 2 × 2 = 864 组合

combos = list(product(
    GRID['activation'], GRID['drawdown'], GRID['cost_stop'],
    GRID['ladder_levels'], GRID['time_stop_days'], GRID['cond_time_cfg'],
    priorities,
))
logger.info(f'参数组合: {len(combos)} 组')

# =========================================================================
# 3. 跑回测, 收集结果
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

for i, (act, dd, cs, lv, mhd, ctcfg, pri) in enumerate(combos, 1):
    cfg = {**base_cfg, 'priority': pri,
           'cost_stop': {'enabled': True, 'threshold': cs},
           'trailing_stop': {'enabled': True, 'activation': act, 'drawdown': dd},
           'ladder_tp': {'enabled': True, 'levels': lv},
           'time_stop': {'enabled': True, 'max_hold_days': mhd},
           'cond_time_stop': {'enabled': True, 'days': ctcfg['days'], 'profit': ctcfg['profit']}}
    levels = lv
    ladder_profits = np.array([l['profit'] for l in levels], dtype=np.float64)
    ladder_ratios = np.array([l['sell_ratio'] for l in levels], dtype=np.float64)
    n_ladder = len(levels)

    try:
        engine = BacktestEngine(ENGINE_CFG)
        result = engine.run_cached(
            close=c, entries=entries, high_np=h_np, low_np=l_np,
            stop_config=cfg, selections=sel_df,
            ladder_profits=ladder_profits, ladder_ratios=ladder_ratios,
            n_ladder=n_ladder, skip_sm=True,
        )
        m = result['metrics']
        results.append({
            'idx': i,
            'activation': act,
            'drawdown': dd,
            'cost_stop': cs,
            'ladder': f"{lv[0]['profit']:.2f}/{lv[0]['sell_ratio']:.2f}+{lv[1]['profit']:.2f}/{lv[1]['sell_ratio']:.2f}",
            'time_stop_days': mhd,
            'cond_time': f"{ctcfg['days']}d/{ctcfg['profit']*100:.1f}%",
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
        logger.warning(f'  [{i}/{len(combos)}] 失败: {e}')

    if i % 20 == 0:
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
logger.info(f'  QUANTQQ 参数优化报告 — {START[:4]}.{START[4:6]}.{START[6:]} ~ {END[:4]}.{END[4:6]}.{END[6:]}')
logger.info(f'  共 {len(combos)} 组参数组合, 用时 {time.time()-t_total:.0f}s')
logger.info('=' * 100)

logger.info(f'\n【Top 15 配置 (按年化收益排序)】')
logger.info(f'{"排名":<4} {"激活线":<7} {"回撤":<7} {"硬止损":<8} {"阶梯":<14} {"时间止损":<8} {"条件时间":<10} {"优先级":<18} {"年化%":<8} {"回撤%":<8} {"夏普":<6} {"交易":<5}')
for _, r in df.head(15).iterrows():
    logger.info(f'{r.name+1:<4} {r["activation"]:<7.3f} {r["drawdown"]:<7.3f} {r["cost_stop"]:<8.2f} {r["ladder"]:<14} {r["time_stop_days"]:<8} {r["cond_time"]:<10} {r["priority"]:<18} {r["ann_ret"]:<8.2f} {r["max_dd"]:<8.2f} {r["sharpe"]:<6.2f} {r["n_trades"]:<5.0f}')

# 年化 >= 25% 的配置
df_25 = df[df['ann_ret'] >= 25]
logger.info(f'\n【年化 >= 25% 的配置: {len(df_25)} 组】')
if len(df_25) > 0:
    logger.info(f'{"排名":<4} {"激活线":<7} {"回撤":<7} {"硬止损":<8} {"阶梯":<14} {"时间止损":<8} {"条件时间":<10} {"优先级":<18} {"年化%":<8} {"回撤%":<8} {"夏普":<6} {"交易":<5}')
    for _, r in df_25.head(20).iterrows():
        logger.info(f'{r.name+1:<4} {r["activation"]:<7.3f} {r["drawdown"]:<7.3f} {r["cost_stop"]:<8.2f} {r["ladder"]:<14} {r["time_stop_days"]:<8} {r["cond_time"]:<10} {r["priority"]:<18} {r["ann_ret"]:<8.2f} {r["max_dd"]:<8.2f} {r["sharpe"]:<6.2f} {r["n_trades"]:<5.0f}')

logger.info(f'\n【全量结果统计】')
logger.info(f'  跑成功: {len(df)}/{len(combos)} 组')
logger.info(f'  年化最高: {df["ann_ret"].max():.2f}% (idx {df["ann_ret"].idxmax()+1})')
logger.info(f'  年化最低: {df["ann_ret"].min():.2f}%')
logger.info(f'  年化中位: {df["ann_ret"].median():.2f}%')
logger.info(f'  年化 >=25%: {len(df_25)} 组 ({len(df_25)/len(df)*100:.1f}%)')

# 保存
out_dir = 'output/optimize'
os.makedirs(out_dir, exist_ok=True)
ts = datetime.now().strftime('%Y%m%d_%H%M%S')
df.to_csv(f'{out_dir}/quantqq_optimize_{ts}.csv', index=False)
df.head(10).to_csv(f'{out_dir}/quantqq_top10_{ts}.csv', index=False)
logger.info(f'\n全量结果: {out_dir}/quantqq_optimize_{ts}.csv')
logger.info(f'Top 10: {out_dir}/quantqq_top10_{ts}.csv')