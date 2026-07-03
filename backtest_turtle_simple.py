"""海龟突破策略 - VERA标准回测（100万资金，简化版）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import numpy as np
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine
from backtest.stop_config import load_stop_config
import json

TdxConnector.ensure_connected()

START = '20220101'
END = '20260528'

print("="*80)
print("VERA 标准回测 - 海龟突破策略（100万资金）")
print("="*80)
print(f"回测区间: {START} ~ {END} (4年+)")
print(f"初始资金: 1,000,000 元\n")

# 1. 获取数据
print("获取数据...", flush=True)
codes = DataFetcher.get_stock_universe('50')
k = DataFetcher.get_kline(codes, START, END, dividend_type="front", period="1d")
close = k["Close"].sort_index()
high = k["High"].sort_index()
low = k["Low"].sort_index()

valid = close.notna().sum() > 200
close = close.loc[:, valid]
high = high.reindex(columns=close.columns)
low = low.reindex(columns=close.columns)

print(f"数据: {close.shape}\n")

# 2. 生成海龟突破信号
print("生成海龟突破信号...", flush=True)
high_20 = high.rolling(20, min_periods=20).max()
high_10 = high.rolling(10, min_periods=10).max()
signal = (close > high_20.shift(1)) & (close.shift(1) > high_10.shift(2))

selections = []
for col in signal.columns:
    for idx in signal.index[signal[col]]:
        selections.append({'stock_code': col, 'select_date': idx.strftime('%Y-%m-%d')})
sel_df = pd.DataFrame(selections)

print(f"信号数: {len(sel_df)}, 股票数: {len(sel_df['stock_code'].unique())}\n")

# 限制到500只最活跃股票
if len(sel_df['stock_code'].unique()) > 500:
    top = sel_df['stock_code'].value_counts().head(500).index
    sel_df = sel_df[sel_df['stock_code'].isin(top)]

common = sorted(set(close.columns) & set(sel_df['stock_code'].unique()))
c = close[common].ffill().bfill()
h = high.reindex(index=c.index, columns=common).ffill().bfill()
l = low.reindex(index=c.index, columns=common).ffill().bfill()

entries = pd.DataFrame(False, index=c.index, columns=c.columns)
for _, row in sel_df.iterrows():
    code = row['stock_code']; dt = pd.to_datetime(row['select_date'])
    if code not in entries.columns: continue
    if dt in entries.index: entries.loc[dt, code] = True
    else:
        m = entries.index >= dt
        if m.any(): entries.loc[entries.index[m][0], code] = True

# 3. 配置回测引擎
ENGINE_CFG = {
    'initial_capital': 1000000.0,
    'commission': 0.0003,
    'slippage': 0.001,
    'period': '1d',
    'position_sizing': {
        'min_buy_amount': 2000.0,
        'max_buy_amount': 20000.0,
        'lot_size': 100,
        'min_lots': 1,
    },
}

STOP_CONFIG = load_stop_config()

print("执行回测...\n", flush=True)
engine = BacktestEngine(ENGINE_CFG)
ladder_profits = np.array([0.06], dtype=np.float64)
ladder_ratios = np.array([1.0], dtype=np.float64)

result = engine.run_cached(c, entries, h.values.astype(np.float64), l.values.astype(np.float64),
                           STOP_CONFIG, sel_df, ladder_profits, ladder_ratios, 1, skip_sm=True)

# 4. 输出结果
print("="*80)
print("回测结果（2022.1.1 ~ 2026.5.28）")
print("="*80)
m = result['metrics']
print(f"累计收益率: {result['cumulative_return']*100:+.2f}%")
print(f"年化收益率: {m['annualized_return']*100:+.2f}%")
print(f"最大回撤: {m['max_drawdown']*100:.2f}%")
print(f"夏普比率: {m['sharpe_ratio']:.2f}")
print(f"胜率: {m['win_rate']*100:.1f}%")
print(f"总交易: {len(result['trades'])}笔")

trades = result['trades']
win = len(trades[trades['profit'] > 0])
lose = len(trades[trades['profit'] <= 0])
print(f"盈利: {win}笔")
print(f"亏损: {lose}笔")
print(f"平均持仓: {trades['hold_days'].mean():.1f}天")

# 5. 月度分析
print("\n" + "="*80)
print("月度收益（2022.1 ~ 2026.5）")
print("="*80)

# 从trades重建权益曲线（按exit_date汇总）
trades['exit_date'] = pd.to_datetime(trades['exit_date'])
trades['year_month'] = trades['exit_date'].dt.to_period('M')

monthly_profit = trades.groupby('year_month')['profit'].sum()
monthly_trades = trades.groupby('year_month').size()

# 计算累计权益
capital = ENGINE_CFG['initial_capital']
monthly_equity = []
for ym in monthly_profit.index:
    capital += monthly_profit[ym]
    monthly_equity.append(capital)

monthly_df = pd.DataFrame({
    'month': monthly_profit.index.astype(str),
    'profit': monthly_profit.values,
    'trades': monthly_trades.values,
    'equity': monthly_equity,
})
monthly_df['return_pct'] = (monthly_df['profit'] / (monthly_df['equity'] - monthly_df['profit'])) * 100

print(f"{'月份':<12} {'盈利金额':>15} {'收益率':>10} {'交易数':>8} {'期末权益':>15}")
print("-"*80)
for _, row in monthly_df.iterrows():
    print(f"{row['month']:<12} {row['profit']:>15,.2f} {row['return_pct']:>9.2f}% {row['trades']:>8} {row['equity']:>15,.2f}")

# 6. 年度汇总
print("\n" + "="*80)
print("年度收益汇总")
print("="*80)
trades['year'] = trades['exit_date'].dt.year
yearly = trades.groupby('year')['profit'].sum()
yearly_trades = trades.groupby('year').size()

capital = ENGINE_CFG['initial_capital']
yearly_equity = []
for y in yearly.index:
    capital += yearly[y]
    yearly_equity.append(capital)

yearly_df = pd.DataFrame({
    'year': yearly.index,
    'profit': yearly.values,
    'trades': yearly_trades.values,
    'equity': yearly_equity,
})
yearly_df['return_pct'] = (yearly_df['profit'] / (yearly_df['equity'] - yearly_df['profit'])) * 100

print(f"{'年份':<8} {'盈利金额':>15} {'收益率':>10} {'交易数':>8} {'期末权益':>15}")
print("-"*80)
for _, row in yearly_df.iterrows():
    print(f"{int(row['year']):<8} {row['profit']:>15,.2f} {row['return_pct']:>9.2f}% {row['trades']:>8} {row['equity']:>15,.2f}")

print("\n" + "="*80)
print(f"最终权益: {yearly_equity[-1]:,.2f} 元")
print(f"总盈利: {yearly_equity[-1] - ENGINE_CFG['initial_capital']:,.2f} 元")
print(f"累计收益率: {(yearly_equity[-1]/ENGINE_CFG['initial_capital'] - 1)*100:+.2f}%")
print("="*80)

# 7. 保存
output = {'metrics': m, 'monthly': monthly_df.to_dict('records'), 'yearly': yearly_df.to_dict('records')}
with open('output/turtle_100w_result.json', 'w', encoding='utf-8') as f:
    json.dump(output, f, ensure_ascii=False, indent=2, default=str)

print("\n结果已保存: output/turtle_100w_result.json")
