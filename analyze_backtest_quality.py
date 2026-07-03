"""回测质量审计工具 - 检查价格执行、假突破率、资金利用率"""
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
print("回测质量审计 - 海龟突破策略")
print("="*80)
print(f"回测区间: {START} ~ {END}\n")

# 1. 获取数据
print("Step 1: 获取数据...", flush=True)
codes = DataFetcher.get_stock_universe('50')
k = DataFetcher.get_kline(codes, START, END, dividend_type="front", period="1d")
close = k["Close"].sort_index()
high = k["High"].sort_index()
low = k["Low"].sort_index()
open_price = k["Open"].sort_index()

valid = close.notna().sum() > 200
close = close.loc[:, valid]
high = high.reindex(columns=close.columns)
low = low.reindex(columns=close.columns)
open_price = open_price.reindex(columns=close.columns)

print(f"数据: {close.shape}\n")

# 2. 生成海龟突破信号
print("Step 2: 生成海龟突破信号...", flush=True)
high_20 = high.rolling(20, min_periods=20).max()
high_10 = high.rolling(10, min_periods=10).max()
signal = (close > high_20.shift(1)) & (close.shift(1) > high_10.shift(2))

selections = []
for col in signal.columns:
    for idx in signal.index[signal[col]]:
        selections.append({'stock_code': col, 'select_date': idx.strftime('%Y-%m-%d')})
sel_df = pd.DataFrame(selections)

print(f"信号数: {len(sel_df)}\n")

if len(sel_df['stock_code'].unique()) > 500:
    top = sel_df['stock_code'].value_counts().head(500).index
    sel_df = sel_df[sel_df['stock_code'].isin(top)]

common = sorted(set(close.columns) & set(sel_df['stock_code'].unique()))
c = close[common].ffill().bfill()
h = high.reindex(index=c.index, columns=common).ffill().bfill()
l = low.reindex(index=c.index, columns=common).ffill().bfill()
o = open_price.reindex(index=c.index, columns=common).ffill().bfill()

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

print("Step 3: 执行回测（使用收盘价）...\n", flush=True)
engine = BacktestEngine(ENGINE_CFG)
ladder_profits = np.array([0.06], dtype=np.float64)
ladder_ratios = np.array([1.0], dtype=np.float64)

result = engine.run_cached(c, entries, h.values.astype(np.float64), l.values.astype(np.float64),
                           STOP_CONFIG, sel_df, ladder_profits, ladder_ratios, 1, skip_sm=True)

# 4. 分析交易记录
trades = result['trades']
trades['entry_date'] = pd.to_datetime(trades['entry_date'])
trades['exit_date'] = pd.to_datetime(trades['exit_date'])

print("="*80)
print("审计报告 - 价格执行分析")
print("="*80)

# 4.1 重新计算：如果用High/Low价格执行会怎样
print("\n【问题1】用收盘价 vs 用High/Low执行的价差\n")

# exit_reason 是字符串，需要映射回数字
reason_to_code = {
    '成本止损': 3, '移动止损': 4, '阶梯止盈': 5,
    '时间止损': 6, '条件时盈': 7, '移动止盈': 8, '时间止盈': 9, '首日未达标': 10
}

# 成本止损和移动止损应该用Low价格
stop_loss_trades = trades[trades['exit_reason'].isin(['成本止损', '移动止损'])]
# 止盈应该用High价格
take_profit_trades = trades[trades['exit_reason'].isin(['阶梯止盈', '条件时盈', '移动止盈', '时间止盈'])]

# 计算实际价格（需要从原始数据重新获取）
close_idx = c.index
close_cols = c.columns

# 为每笔交易找到实际的High/Low价格
stop_loss_actual_prices = []
for _, trade in stop_loss_trades.iterrows():
    code = trade['stock_code']
    exit_date = trade['exit_date']
    if code in close_cols and exit_date in close_idx:
        actual_low = l.loc[exit_date, code]
        current_close = c.loc[exit_date, code]
        price_diff_pct = (current_close - actual_low) / actual_low * 100
        stop_loss_actual_prices.append({
            'code': code,
            'exit_date': exit_date,
            'close_price': current_close,
            'low_price': actual_low,
            'diff_pct': price_diff_pct,
            'reason': trade['exit_reason']
        })

tp_actual_prices = []
for _, trade in take_profit_trades.iterrows():
    code = trade['stock_code']
    exit_date = trade['exit_date']
    if code in close_cols and exit_date in close_idx:
        actual_high = h.loc[exit_date, code]
        current_close = c.loc[exit_date, code]
        price_diff_pct = (actual_high - current_close) / current_close * 100
        tp_actual_prices.append({
            'code': code,
            'exit_date': exit_date,
            'close_price': current_close,
            'high_price': actual_high,
            'diff_pct': price_diff_pct,
            'reason': trade['exit_reason']
        })

if stop_loss_actual_prices:
    sl_df = pd.DataFrame(stop_loss_actual_prices)
    print(f"止损交易（成本止损+移动止损）: {len(sl_df)} 笔")
    print(f"  收盘价 vs Low价格 平均差异: {sl_df['diff_pct'].mean():.2f}%")
    print(f"  最大差异: {sl_df['diff_pct'].max():.2f}%")
    print(f"  中位数差异: {sl_df['diff_pct'].median():.2f}%")
    print(f"  → 影响：实际亏损应该更大 {sl_df['diff_pct'].mean():.2f}%\n")

if tp_actual_prices:
    tp_df = pd.DataFrame(tp_actual_prices)
    print(f"止盈交易（阶梯止盈+移动止盈）: {len(tp_df)} 笔")
    print(f"  收盘价 vs High价格 平均差异: {tp_df['diff_pct'].mean():.2f}%")
    print(f"  最大差异: {tp_df['diff_pct'].max():.2f}%")
    print(f"  中位数差异: {tp_df['diff_pct'].median():.2f}%")
    print(f"  → 影响：实际盈利应该更高 {tp_df['diff_pct'].mean():.2f}%\n")

# 计算净影响
if stop_loss_actual_prices and tp_actual_prices:
    total_trades = len(trades)
    sl_weight = len(sl_df) / total_trades
    tp_weight = len(tp_df) / total_trades
    net_impact = tp_weight * tp_df['diff_pct'].mean() - sl_weight * sl_df['diff_pct'].mean()
    print(f"【结论】收盘价执行的净影响: {net_impact:+.2f}%")
    print(f"  原回测收益: +335.70%")
    print(f"  修正后估计: {335.70 + net_impact * 14846 / 100:.2f}%\n")

# 4.2 假突破率分析
print("="*80)
print("【问题2】假突破率分析")
print("="*80)

# 定义假突破：买入后次日收盘价低于买入价
trades['next_day'] = trades['entry_date'] + pd.Timedelta(days=1)
fake_breakouts = []

for _, trade in trades.iterrows():
    code = trade['stock_code']
    entry_date = trade['entry_date']
    next_day = trade['next_day']
    entry_price = trade['entry_price']

    # 找到下一个交易日
    if code in close_cols:
        future_dates = close_idx[close_idx > entry_date]
        if len(future_dates) > 0:
            actual_next_day = future_dates[0]
            next_close = c.loc[actual_next_day, code]
            next_return = (next_close - entry_price) / entry_price * 100

            fake_breakouts.append({
                'code': code,
                'entry_date': entry_date,
                'entry_price': entry_price,
                'next_day': actual_next_day,
                'next_close': next_close,
                'next_return': next_return,
                'is_fake': next_return < 0
            })

if fake_breakouts:
    fb_df = pd.DataFrame(fake_breakouts)
    fake_count = fb_df['is_fake'].sum()
    fake_rate = fake_count / len(fb_df) * 100

    print(f"\n总交易: {len(fb_df)} 笔")
    print(f"假突破（次日收盘 < 买入价）: {fake_count} 笔")
    print(f"假突破率: {fake_rate:.1f}%")
    print(f"次日平均收益: {fb_df['next_return'].mean():.2f}%")
    print(f"  真突破次日收益: {fb_df[~fb_df['is_fake']]['next_return'].mean():.2f}%")
    print(f"  假突破次日收益: {fb_df[fb_df['is_fake']]['next_return'].mean():.2f}%")

    # 统计假突破的最终结果
    fake_codes = fb_df[fb_df['is_fake']][['code', 'entry_date']]
    fake_final_pnl = []
    for _, row in fake_codes.iterrows():
        trade = trades[(trades['stock_code'] == row['code']) &
                      (trades['entry_date'] == row['entry_date'])]
        if not trade.empty:
            fake_final_pnl.append(trade.iloc[0]['pnl'])

    if fake_final_pnl:
        print(f"\n假突破交易的最终盈亏:")
        print(f"  平均盈亏: {np.mean(fake_final_pnl):,.2f} 元")
        print(f"  总盈亏: {np.sum(fake_final_pnl):,.2f} 元")
        print(f"  → 影响：假突破拖累总收益 {np.sum(fake_final_pnl):,.0f} 元\n")

# 4.3 资金利用率分析
print("="*80)
print("【问题3】资金利用率分析")
print("="*80)

# 按日期汇总持仓市值
daily_positions = []
all_dates = sorted(c.index)

for date in all_dates:
    # 找出在该日期持仓的交易
    active_trades = trades[(trades['entry_date'] <= date) & (trades['exit_date'] >= date)]

    if len(active_trades) > 0:
        total_value = 0
        for _, trade in active_trades.iterrows():
            code = trade['stock_code']
            shares = trade['shares']
            if code in close_cols and date in close_idx:
                price = c.loc[date, code]
                total_value += shares * price

        daily_positions.append({
            'date': date,
            'position_count': len(active_trades),
            'position_value': total_value,
            'utilization': total_value / ENGINE_CFG['initial_capital'] * 100
        })

if daily_positions:
    dp_df = pd.DataFrame(daily_positions)
    print(f"\n资金利用率统计（{len(dp_df)} 个交易日）:")
    print(f"  平均持仓数量: {dp_df['position_count'].mean():.1f} 个")
    print(f"  平均持仓市值: {dp_df['position_value'].mean():,.0f} 元")
    print(f"  平均资金利用率: {dp_df['utilization'].mean():.1f}%")
    print(f"  最大资金利用率: {dp_df['utilization'].max():.1f}%")
    print(f"  最小资金利用率: {dp_df['utilization'].min():.1f}%")
    print(f"  中位数利用率: {dp_df['utilization'].median():.1f}%")

    # 计算闲置资金
    idle_rate = 100 - dp_df['utilization'].mean()
    print(f"\n  → 平均闲置资金: {idle_rate:.1f}%")
    print(f"  → 相当于闲置: {ENGINE_CFG['initial_capital'] * idle_rate / 100:,.0f} 元")

    # 如果提高单笔上限，收益可能提升多少
    if idle_rate > 20:
        potential_boost = (100 / (100 - idle_rate) - 1) * 100
        print(f"\n  【优化建议】")
        print(f"  如果充分利用闲置资金（提高单笔上限或增加仓位）:")
        print(f"  收益可能提升: {potential_boost:.1f}%")
        print(f"  原收益 +335.70% → 潜在收益: {335.70 * (1 + potential_boost/100):.2f}%")

# 5. 保存审计报告
audit_report = {
    'price_execution': {
        'stop_loss': sl_df[['code', 'close_price', 'low_price', 'diff_pct', 'reason']].to_dict('records') if stop_loss_actual_prices else [],
        'take_profit': tp_df[['code', 'close_price', 'high_price', 'diff_pct', 'reason']].to_dict('records') if tp_actual_prices else [],
        'summary': {
            'stop_loss_avg_diff': float(sl_df['diff_pct'].mean()) if stop_loss_actual_prices else 0,
            'take_profit_avg_diff': float(tp_df['diff_pct'].mean()) if tp_actual_prices else 0,
            'net_impact': float(net_impact) if (stop_loss_actual_prices and tp_actual_prices) else 0,
        }
    },
    'fake_breakout': {
        'total_trades': len(fb_df) if fake_breakouts else 0,
        'fake_count': int(fake_count) if fake_breakouts else 0,
        'fake_rate': float(fake_rate) if fake_breakouts else 0,
        'fake_total_pnl': float(np.sum(fake_final_pnl)) if fake_final_pnl else 0,
    },
    'capital_utilization': {
        'avg_positions': float(dp_df['position_count'].mean()) if daily_positions else 0,
        'avg_utilization': float(dp_df['utilization'].mean()) if daily_positions else 0,
        'idle_rate': float(idle_rate) if daily_positions else 0,
    }
}

with open('output/audit_report.json', 'w', encoding='utf-8') as f:
    json.dump(audit_report, f, ensure_ascii=False, indent=2)

print("\n" + "="*80)
print("审计报告已保存: output/audit_report.json")
print("="*80)
