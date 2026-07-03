"""海龟突破策略 - 多档阶梯止盈完整回测（2022-2026）

变更说明（2026-06-03 engine v3.2）：
  硬止损(3)：Low检测 → stop_price 简化模式执行 (ep*(1+threshold))
  阶梯止盈(5)：High检测 → ladder_price 简化模式执行 (ep*(1+profit))
  移动止盈(8)：High激活 + Close回撤 → Close执行
  移动止损(4)：High激活 + Close回撤 → Close执行
  其他：Close执行

STOP_CONFIG:
  硬止损 -12%
  阶梯止盈 +6%卖30%, +15%卖30%（剩余40%由移动止盈接管）
  移动止盈 +8%激活, -5%回撤（平衡模式）
  时间止损 20天
  条件时间止盈 7天+2%
"""
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
END   = '20260531'

print("=" * 90)
print("  VERA 海龟突破策略 — 多档阶梯止盈 + 移动止盈回测")
print("  Engine v3.2: stop_price / ladder_price / Close 分别执行")
print("=" * 90)
print(f"  回测区间: {START} ~ {END}")
print(f"  初始资金: 1,000,000 元\n")

# ═══════════════════════════════════════════════════════════════
# 1. 获取数据
# ═══════════════════════════════════════════════════════════════
print("[1/4] 获取K线数据...", flush=True)
codes = DataFetcher.get_stock_universe('50')
k = DataFetcher.get_kline(codes, START, END, dividend_type="front", period="1d")
close = k["Close"].sort_index()
high  = k["High"].sort_index()
low   = k["Low"].sort_index()

valid = close.notna().sum() > 200
close = close.loc[:, valid]
high  = high.reindex(columns=close.columns)
low   = low.reindex(columns=close.columns)
print(f"      数据: {close.shape[0]} 交易日 × {close.shape[1]} 只股票\n")

# ═══════════════════════════════════════════════════════════════
# 2. 生成海龟突破信号
# ═══════════════════════════════════════════════════════════════
print("[2/4] 生成海龟突破信号...", flush=True)
high_20 = high.rolling(20, min_periods=20).max()
high_10 = high.rolling(10, min_periods=10).max()
signal = (close > high_20.shift(1)) & (close.shift(1) > high_10.shift(2))

selections = []
for col in signal.columns:
    for idx in signal.index[signal[col]]:
        selections.append({'stock_code': col, 'select_date': idx.strftime('%Y-%m-%d')})
sel_df = pd.DataFrame(selections)
print(f"      信号数: {len(sel_df)}, 股票数: {len(sel_df['stock_code'].unique())}\n")

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
    if dt in entries.index:
        entries.loc[dt, code] = True
    else:
        m = entries.index >= dt
        if m.any(): entries.loc[entries.index[m][0], code] = True

# ═══════════════════════════════════════════════════════════════
# 3. 配置回测引擎
# ═══════════════════════════════════════════════════════════════
print("[3/4] 配置 & 运行回测...\n", flush=True)

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

print("   止盈止损配置:")
print(f"    ├─ 硬止损(简化模式): -12% → stop_price执行")
print(f"    ├─ 阶梯止盈(简化模式): +6%卖30%, +15%卖30% → ladder_price执行")
print(f"    ├─ 移动止盈(平衡Close回撤): +8%激活, -5%回撤 → Close执行")
print(f"    ├─ 时间止损: 20天 → Close执行")
print(f"    └─ 条件时间止盈: 7天+2% → Close执行")
print()

engine = BacktestEngine(ENGINE_CFG)
ladder_profits = np.array([0.06, 0.15], dtype=np.float64)
ladder_ratios  = np.array([0.30, 0.30], dtype=np.float64)

result = engine.run_cached(c, entries,
                           h.values.astype(np.float64),
                           l.values.astype(np.float64),
                           STOP_CONFIG, sel_df,
                           ladder_profits, ladder_ratios, 2,
                           skip_sm=True)

# ═══════════════════════════════════════════════════════════════
# 4. 报告输出
# ═══════════════════════════════════════════════════════════════
trades = result['trades']
trades['entry_date'] = pd.to_datetime(trades['entry_date'])
trades['exit_date']  = pd.to_datetime(trades['exit_date'])
metrics = result['metrics']

print("=" * 90)
print("  【回测总览】")
print("=" * 90)
print(f"  累计收益率:     {result['cumulative_return']*100:+.2f}%")
final_equity = ENGINE_CFG['initial_capital'] * (1 + result['cumulative_return'])
print(f"  期末权益:       {final_equity:,.0f} 元")
print(f"  年化收益率:     {metrics['annualized_return']*100:+.2f}%")
print(f"  最大回撤:       {metrics['max_drawdown']*100:.2f}%")
print(f"  夏普比率:       {metrics['sharpe_ratio']:.2f}")
print(f"  胜率:           {metrics['win_rate']*100:.1f}%")
print(f"  总交易:         {len(trades):,} 笔")
print(f"  盈利/亏损:      {len(trades[trades['pnl']>0])} / {len(trades[trades['pnl']<=0])}")
print(f"  盈亏比:         {trades[trades['pnl']>0]['pnl'].mean() / abs(trades[trades['pnl']<=0]['pnl'].mean()):.2f}" if len(trades[trades['pnl']<=0]) > 0 else "  盈亏比: N/A")
print(f"  平均持仓:       {trades['hold_days'].mean():.1f} 天")
print(f"  平均单笔盈亏:   {trades['pnl'].mean():,.0f} 元")
print(f"  最大单笔盈利:   {trades['pnl'].max():,.0f} 元")
print(f"  最大单笔亏损:   {trades['pnl'].min():,.0f} 元")

# ── 退出原因统计 ──
print("\n" + "-" * 90)
print("  【退出原因分布】")
print("-" * 90)
reason_counts = trades['exit_reason'].value_counts()
reason_pnl = trades.groupby('exit_reason')['pnl'].agg(['sum', 'mean', 'count'])
for reason in reason_counts.index:
    cnt = reason_counts[reason]
    total_pnl = reason_pnl.loc[reason, 'sum']
    avg_pnl = reason_pnl.loc[reason, 'mean']
    pct = cnt / len(trades) * 100
    print(f"  {reason:<16s} {cnt:>6d}笔 ({pct:>5.1f}%)  总盈亏{total_pnl:>+12,.0f}  均值{avg_pnl:>+10,.0f}")

# ── 年度汇总 ──
print("\n" + "=" * 90)
print("  【年度收益汇总】")
print("=" * 90)
trades['year'] = trades['exit_date'].dt.year
yearly = trades.groupby('year')['pnl'].sum()
yearly_trades = trades.groupby('year').size()
yearly_wins = trades[trades['pnl']>0].groupby('year').size()
yearly_loss = trades[trades['pnl']<=0].groupby('year').size()

capital = ENGINE_CFG['initial_capital']
print(f"  {'年份':<8} {'盈利':>14} {'收益率':>10} {'交易数':>8} {'胜率':>8} {'期末权益':>15} {'当年盈亏比':>10}")
print("  " + "-" * 80)
for y in yearly.index:
    profit = yearly[y]
    cnt = yearly_trades[y]
    win_cnt = yearly_wins.get(y, 0)
    lose_cnt = yearly_loss.get(y, 0)
    win_rate = win_cnt / cnt * 100 if cnt > 0 else 0
    capital += profit
    # 当年盈亏比
    year_trades = trades[trades['year']==y]
    avg_win = year_trades[year_trades['pnl']>0]['pnl'].mean() if win_cnt > 0 else 0
    avg_loss = abs(year_trades[year_trades['pnl']<=0]['pnl'].mean()) if lose_cnt > 0 else 1
    pl_ratio = avg_win / avg_loss if avg_loss > 0 else float('inf')
    ret = profit / (capital - profit) * 100
    print(f"  {int(y):<8} {profit:>+14,.0f} {ret:>9.2f}% {cnt:>8} {win_rate:>7.1f}% {capital:>15,.0f} {pl_ratio:>10.2f}")

total_profit = capital - ENGINE_CFG['initial_capital']
print("  " + "-" * 80)
print(f"  {'合计':<8} {total_profit:>+14,.0f} {result['cumulative_return']*100:>9.2f}% {len(trades):>8}")

# ── 季度明细 ──
print("\n" + "=" * 90)
print("  【季度收益明细】")
print("=" * 90)
trades['quarter'] = trades['exit_date'].dt.to_period('Q')
quarterly = trades.groupby('quarter').agg(
    profit=('pnl', 'sum'),
    trades=('pnl', 'count'),
    wins=('pnl', lambda x: (x > 0).sum()),
    losses=('pnl', lambda x: (x <= 0).sum()),
    avg_hold=('hold_days', 'mean'),
    max_win=('pnl', 'max'),
    max_loss=('pnl', 'min'),
    avg_pnl=('pnl', 'mean'),
)

# 重建权益曲线（按季度累计）
capital_q = ENGINE_CFG['initial_capital']
quarterly_equity = []
quarterly_dd = []
peak_equity = ENGINE_CFG['initial_capital']
for q in quarterly.index:
    capital_q += quarterly.loc[q, 'profit']
    quarterly_equity.append(capital_q)
    if capital_q > peak_equity:
        peak_equity = capital_q
    q_dd = (capital_q - peak_equity) / peak_equity * 100
    quarterly_dd.append(q_dd)

quarterly['equity'] = quarterly_equity
quarterly['dd_pct'] = quarterly_dd
quarterly['ret_pct'] = quarterly['profit'] / (quarterly['equity'] - quarterly['profit']) * 100

print(f"  {'季度':<10} {'盈亏':>12} {'收益率':>8} {'交易':>6} {'胜率':>7} {'均值盈亏':>10} {'最大盈利':>10} {'最大亏损':>10} {'期末权益':>14} {'回撤':>7}")
print("  " + "-" * 90)
for q in quarterly.index:
    row = quarterly.loc[q]
    wr = row['wins'] / row['trades'] * 100 if row['trades'] > 0 else 0
    print(f"  {str(q):<10} {row['profit']:>+12,.0f} {row['ret_pct']:>7.2f}% {int(row['trades']):>6} {wr:>6.1f}% {row['avg_pnl']:>+10,.0f} {row['max_win']:>+10,.0f} {row['max_loss']:>+10,.0f} {row['equity']:>14,.0f} {row['dd_pct']:>6.2f}%")

# ── 年度回撤分析 ──
print("\n" + "=" * 90)
print("  【年度回撤分析】")
print("=" * 90)
# 从equity_curve计算年度最大回撤
eq = result.get('equity_curve', None)
if eq is not None and not eq.empty:
    eq['year'] = pd.to_datetime(eq['date']).dt.year
    for y in sorted(eq['year'].unique()):
        ye = eq[eq['year'] == y]
        peak_y = ye['equity'].expanding().max()
        dd_y = (ye['equity'] - peak_y) / peak_y
        max_dd_y = dd_y.min() * 100
        print(f"  {int(y)} 年最大回撤: {max_dd_y:.2f}%")

# ── 月度最大回撤 ──
print("\n" + "=" * 90)
print("  【月度收益】")
print("=" * 90)
trades['year_month'] = trades['exit_date'].dt.to_period('M')
monthly = trades.groupby('year_month').agg(
    profit=('pnl', 'sum'),
    trades=('pnl', 'count'),
    wins=('pnl', lambda x: (x > 0).sum()),
)
capital_m = ENGINE_CFG['initial_capital']
monthly_eq = []
for m in monthly.index:
    capital_m += monthly.loc[m, 'profit']
    monthly_eq.append(capital_m)
monthly['equity'] = monthly_eq
monthly['ret_pct'] = monthly['profit'] / (monthly['equity'] - monthly['profit']) * 100

# 只显示赢/亏较大的月份
print(f"  {'月份':<10} {'盈亏':>12} {'月收益率':>9} {'交易数':>7} {'胜率':>7} {'期末权益':>14}")
print("  " + "-" * 70)
for m in monthly.index:
    row = monthly.loc[m]
    wr = row['wins'] / row['trades'] * 100 if row['trades'] > 0 else 0
    print(f"  {str(m):<10} {row['profit']:>+12,.0f} {row['ret_pct']:>8.2f}% {int(row['trades']):>7} {wr:>6.1f}% {row['equity']:>14,.0f}")

# ── 盈亏分布直方图数据 ──
print("\n" + "=" * 90)
print("  【盈亏分布】")
print("=" * 90)
bins = [-float('inf'), -10000, -5000, -2000, -1000, 0, 1000, 2000, 5000, 10000, 20000, float('inf')]
labels = ['<-10k', '-10k~-5k', '-5k~-2k', '-2k~-1k', '-1k~0', '0~1k', '1k~2k', '2k~5k', '5k~10k', '10k~20k', '>20k']
pnl_bins = pd.cut(trades['pnl'], bins=bins, labels=labels)
dist = pnl_bins.value_counts().sort_index()
for label in labels:
    cnt = dist.get(label, 0)
    bar = '█' * (cnt // max(1, dist.max() // 40))
    print(f"  {label:>12s}: {cnt:>5d} {bar}")

# ── 保存完整报告 ──
output = {
    'config': {
        'engine_version': 'v3.2-exec-price-20260603',
        'stop_config': {
            'cost_stop': '-12% 简化模式(stop_price)',
            'ladder_tp': '+6%卖30%, +15%卖30% 简化模式(ladder_price)',
            'trailing_stop': '+8%激活 -5%回撤 Close检测(平衡模式)',
            'time_stop': '20天',
            'cond_time_stop': '7天+2%',
        },
    },
    'summary': {
        'cumulative_return': f"{result['cumulative_return']*100:+.2f}%",
        'final_equity': f"{final_equity:,.0f}",
        'annualized_return': f"{metrics['annualized_return']*100:+.2f}%",
        'max_drawdown': f"{metrics['max_drawdown']*100:.2f}%",
        'sharpe_ratio': f"{metrics['sharpe_ratio']:.2f}",
        'win_rate': f"{metrics['win_rate']*100:.1f}%",
        'total_trades': len(trades),
    },
    'yearly': yearly_df.to_dict('records') if 'yearly_df' in dir() else [],
    'quarterly': quarterly.reset_index().to_dict('records'),
    'monthly': monthly.reset_index().to_dict('records'),
    'exit_reasons': reason_counts.to_dict(),
}

import json as _json
with open('output/backtest_multitier_2026.json', 'w', encoding='utf-8') as f:
    _json.dump(output, f, ensure_ascii=False, indent=2, default=str)

print(f"\n  报告已保存: output/backtest_multitier_2026.json")
print("=" * 90)
