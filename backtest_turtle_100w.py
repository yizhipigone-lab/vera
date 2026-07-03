"""使用 VERA 标准流程回测海龟突破策略（100万资金，打印月度收益）"""
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

# 海龟突破通达信公式（Python实现）
FORMULA = """
HIGH20:=HHV(HIGH,20);
HIGH10:=HHV(HIGH,10);
BREAKOUT:=CLOSE > REF(HIGH20,1);
PREV_BREAK:=REF(CLOSE,1) > REF(HIGH10,2);
ZP:BREAKOUT AND PREV_BREAK;
"""

START = '20220101'
END = '20260528'

print("="*80)
print("VERA 标准回测 - 海龟突破策略")
print("="*80)
print(f"回测区间: {START} ~ {END}")
print(f"初始资金: 1,000,000 元")
print(f"公式: {FORMULA.strip()}")
print("="*80 + "\n")

# 1. 获取数据
print("Step 1: 获取数据...", flush=True)
codes = DataFetcher.get_stock_universe('50')
k = DataFetcher.get_kline(codes, START, END, dividend_type="front", period="1d")
close = k["Close"].sort_index()
high = k["High"].sort_index()
low = k["Low"].sort_index()

# 过滤：至少200天数据
valid = close.notna().sum() > 200
close = close.loc[:, valid]
high = high.reindex(columns=close.columns)
low = low.reindex(columns=close.columns)

print(f"数据形状: {close.shape} (日期 x 股票)")
print(f"日期范围: {close.index[0].strftime('%Y-%m-%d')} ~ {close.index[-1].strftime('%Y-%m-%d')}\n")

# 2. 生成选股信号
print("Step 2: 生成选股信号...", flush=True)
high_20 = high.rolling(20, min_periods=20).max()
high_10 = high.rolling(10, min_periods=10).max()

# 海龟突破信号
breakout = close > high_20.shift(1)
prev_break = close.shift(1) > high_10.shift(2)
signal = breakout & prev_break

# 转换为选股记录
selections = []
for col in signal.columns:
    for idx in signal.index[signal[col]]:
        selections.append({
            'stock_code': col,
            'select_date': idx.strftime('%Y-%m-%d')
        })
sel_df = pd.DataFrame(selections)

print(f"信号数量: {len(sel_df)}")
print(f"触发股票数: {len(sel_df['stock_code'].unique())}\n")

if len(sel_df) == 0:
    print("无信号，退出。")
    sys.exit(0)

# 限制股票数量（避免过大）
if len(sel_df['stock_code'].unique()) > 500:
    top_codes = sel_df['stock_code'].value_counts().head(500).index
    sel_df = sel_df[sel_df['stock_code'].isin(top_codes)]
    print(f"限制到 500 只最活跃股票\n")

# 3. 准备回测数据
common_codes = sorted(set(close.columns) & set(sel_df['stock_code'].unique()))
c = close[common_codes].ffill().bfill()
h = high.reindex(index=c.index, columns=common_codes).ffill().bfill()
l = low.reindex(index=c.index, columns=common_codes).ffill().bfill()

# 生成 entry 矩阵
entries = pd.DataFrame(False, index=c.index, columns=c.columns)
for _, row in sel_df.iterrows():
    code = row['stock_code']
    dt = pd.to_datetime(row['select_date'])
    if code not in entries.columns:
        continue
    if dt in entries.index:
        entries.loc[dt, code] = True
    else:
        # 找到第一个大于等于dt的日期
        mask = entries.index >= dt
        if mask.any():
            entries.loc[entries.index[mask][0], code] = True

print(f"Step 3: 准备回测数据完成\n")

# 4. 配置回测引擎（100万资金）
ENGINE_CFG = {
    'initial_capital': 1000000.0,  # 100万
    'commission': 0.0003,           # 万3手续费
    'slippage': 0.001,              # 0.1%滑点
    'period': '1d',
    'position_sizing': {
        'min_buy_amount': 2000.0,
        'max_buy_amount': 20000.0,  # 单笔最大2万（控制风险）
        'lot_size': 100,
        'min_lots': 1,
    },
}

# 止盈止损配置（最优配置）
STOP_CONFIG = load_stop_config()

print("Step 4: 配置回测引擎")
print(f"  初始资金: {ENGINE_CFG['initial_capital']:,.0f} 元")
print(f"  手续费: {ENGINE_CFG['commission']*100:.2f}%")
print(f"  滑点: {ENGINE_CFG['slippage']*100:.1f}%")
print(f"  单笔金额: {ENGINE_CFG['position_sizing']['min_buy_amount']:.0f} ~ {ENGINE_CFG['position_sizing']['max_buy_amount']:.0f} 元")
print(f"  止盈: 6% 全卖")
print(f"  止损: -12% 成本止损, 5%/3% 移动止损, 20天时间止损\n")

# 5. 执行回测
print("Step 5: 执行回测...\n", flush=True)
engine = BacktestEngine(ENGINE_CFG)

ladder_profits = np.array([0.06], dtype=np.float64)
ladder_ratios = np.array([1.0], dtype=np.float64)

result = engine.run_cached(
    c, entries,
    h.values.astype(np.float64),
    l.values.astype(np.float64),
    STOP_CONFIG, sel_df,
    ladder_profits, ladder_ratios, 1,
    skip_sm=True
)

# 6. 输出结果
print("\n" + "="*80)
print("回测结果")
print("="*80)

metrics = result['metrics']
print(f"累计收益率: {result['cumulative_return']*100:+.2f}%")
print(f"年化收益率: {metrics['annualized_return']*100:+.2f}%")
print(f"最大回撤: {metrics['max_drawdown']*100:.2f}%")
print(f"夏普比率: {metrics['sharpe_ratio']:.2f}")
print(f"胜率: {metrics['win_rate']*100:.1f}%")
print(f"总交易次数: {len(result['trades'])}")
trades_df = pd.DataFrame(result['trades'], columns=['stock_code','entry_date','exit_date','entry_price','exit_price','shares','profit','return','reason'])
win_trades = len(trades_df[trades_df['profit'] > 0])
lose_trades = len(trades_df[trades_df['profit'] <= 0])
print(f"盈利次数: {win_trades}")
print(f"亏损次数: {lose_trades}")
if 'avg_hold_days' in metrics:
    print(f"平均持仓天数: {metrics['avg_hold_days']:.1f} 天")

# 7. 月度收益分析
print("\n" + "="*80)
print("月度收益明细")
print("="*80)

equity_curve = result['equity_curve']
equity_curve['year_month'] = equity_curve.index.to_period('M')

monthly = equity_curve.groupby('year_month').agg({
    'equity': ['first', 'last']
})
monthly.columns = ['start_equity', 'end_equity']
monthly['return_pct'] = (monthly['end_equity'] - monthly['start_equity']) / monthly['start_equity'] * 100
monthly['profit'] = monthly['end_equity'] - monthly['start_equity']

print(f"{'月份':<12} {'期初权益':>15} {'期末权益':>15} {'收益率':>10} {'盈利金额':>15}")
print("-"*80)
for idx, row in monthly.iterrows():
    print(f"{str(idx):<12} {row['start_equity']:>15,.2f} {row['end_equity']:>15,.2f} {row['return_pct']:>9.2f}% {row['profit']:>15,.2f}")

print("\n" + "="*80)
print("年度收益汇总")
print("="*80)

yearly = equity_curve.groupby(equity_curve.index.year).agg({
    'equity': ['first', 'last']
})
yearly.columns = ['start_equity', 'end_equity']
yearly['return_pct'] = (yearly['end_equity'] - yearly['start_equity']) / yearly['start_equity'] * 100
yearly['profit'] = yearly['end_equity'] - yearly['start_equity']

print(f"{'年份':<8} {'期初权益':>15} {'期末权益':>15} {'收益率':>10} {'盈利金额':>15}")
print("-"*80)
for idx, row in yearly.iterrows():
    print(f"{idx:<8} {row['start_equity']:>15,.2f} {row['end_equity']:>15,.2f} {row['return_pct']:>9.2f}% {row['profit']:>15,.2f}")

# 8. 保存结果
output = {
    'config': {
        'formula': FORMULA.strip(),
        'start_date': START,
        'end_date': END,
        'initial_capital': ENGINE_CFG['initial_capital'],
        'commission': ENGINE_CFG['commission'],
        'slippage': ENGINE_CFG['slippage'],
    },
    'metrics': {k: float(v) if isinstance(v, (np.floating, np.integer)) else v for k, v in metrics.items()},
    'monthly': monthly.to_dict('index'),
    'yearly': yearly.to_dict('index'),
}

with open('output/turtle_backtest_100w.json', 'w', encoding='utf-8') as f:
    # 处理 Period 对象
    def convert(obj):
        if isinstance(obj, pd.Period):
            return str(obj)
        raise TypeError
    json.dump(output, f, ensure_ascii=False, indent=2, default=convert)

print("\n结果已保存: output/turtle_backtest_100w.json")
