"""均线金叉策略 — 2024.1.1至今回测

止盈止损:
  硬止损 -6%
  阶梯止盈 +3%卖50%
  移动止盈 +3%激活, -3%回撤
  时间止损 15天
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import numpy as np
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine
from backtest.stop_config import load_stop_config

TdxConnector.ensure_connected()

START, END = '20240101', '20260603'

print("=" * 90)
print("  均线多头排列突破策略 — 2024.1.1 ~ 2026.6.3")
print("=" * 90)

# ═════════════ 1. 数据 ═════════════
print("\n[1] 获取数据...", flush=True)
codes = DataFetcher.get_stock_universe('50')
k = DataFetcher.get_kline(codes, '20230101', END, dividend_type="front", period="1d")
close = k["Close"].sort_index()
high = k["High"].sort_index()
low = k["Low"].sort_index()

valid = close.notna().sum() > 200
close = close.loc[:, valid]
high = high.reindex(columns=close.columns).ffill().bfill()
low = low.reindex(columns=close.columns).ffill().bfill()
print(f"  数据: {close.shape}")

# ═════════════ 2. 公式信号 ═════════════
print("\n[2] 计算信号...", flush=True)

ma5 = close.rolling(5).mean()
ma10 = close.rolling(10).mean()
ma20 = close.rolling(20).mean()
ma60 = close.rolling(60).mean()
ma20a = ma20.rolling(10).mean()  # MA(MA20,10)

# AD: MA5>MA10 AND MA10>MA20
ad = (ma5 > ma10) & (ma10 > ma20)

# AB: 10日内无涨停(>8%)
ab = (close / close.shift(1) < 1.08).rolling(10).min() > 0

# AC: 10日内无跳空高开(L>REF(H,1))
# COUNT(L>REF(H,1),10)=0
ac = (low <= high.shift(1)).rolling(10).min() > 0

# CROSS(MA20, MA20A)
cross_up = (ma20 > ma20a) & (ma20.shift(1) <= ma20a.shift(1))

# UPNDAY(MA10,2) AND UPNDAY(MA5,2)
upnday_ma10 = (ma10 > ma10.shift(1)) & (ma10.shift(1) > ma10.shift(2))
upnday_ma5 = (ma5 > ma5.shift(1)) & (ma5.shift(1) > ma5.shift(2))

# UPNDAY(MA60,20)
upnday_ma60 = pd.DataFrame(True, index=ma60.index, columns=ma60.columns)
for i in range(1, 20):
    upnday_ma60 = upnday_ma60 & (ma60 > ma60.shift(i))

# MA20 > MA60
bullish = ma20 > ma60

signal = cross_up & ad & ab & ac & upnday_ma10 & upnday_ma5 & upnday_ma60 & bullish
sig_dates = signal.loc[START:END]
total_sig = sig_dates.sum().sum()
print(f"  信号总数: {total_sig:,}")

# 月度分布
sig_monthly = sig_dates.resample('M').sum().sum(axis=1)
print("  月度信号:")
for m, cnt in sig_monthly.items():
    if cnt > 0:
        print(f"    {m.strftime('%Y-%m')}: {int(cnt)}")

# ═════════════ 3. 回测 ═════════════
print("\n[3] 回测...", flush=True)

# 股票池：全量排除ST
def exclude_st(cl): return [c for c in cl if 'ST' not in c and '*ST' not in c]
all_clean = exclude_st([c for c in close.columns if c in sig_dates.columns])
print(f"  股票池: {len(all_clean)} 只（全量无ST）")

sel_records = []
for col in all_clean:
    for idx in sig_dates.index[sig_dates[col]]:
        sel_records.append({'stock_code': col, 'select_date': idx.strftime('%Y-%m-%d')})
sel_df = pd.DataFrame(sel_records)

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

ENGINE_CFG = {
    'initial_capital': 1000000.0, 'commission': 0.0003, 'slippage': 0.001, 'period': '1d',
    'position_sizing': {'min_buy_amount': 2000.0, 'max_buy_amount': 20000.0, 'lot_size': 100, 'min_lots': 1},
}

# 止盈止损:
# -6%硬止损, +3%卖50%, +3%激活移动止盈-3%回撤, 15天无条件卖出
STOP_CONFIG = load_stop_config()

print("\n  止盈止损:")
print("    硬止损:       -6% (stop_price)")
print("    阶梯止盈:     +3% → 卖50% (ladder_price)")
print("    移动止盈:     +3%激活, -3%回撤 (High激活, Close回撤)")
print("    时间止损:     15天")
print()

engine = BacktestEngine(ENGINE_CFG)
result = engine.run_cached(c, entries, h.values.astype(np.float64), l.values.astype(np.float64),
                           STOP_CONFIG, sel_df,
                           np.array([0.03], dtype=np.float64),
                           np.array([0.50], dtype=np.float64), 1, skip_sm=True)

# ═════════════ 4. 报告 ═════════════
trades = result['trades']
trades['entry_date'] = pd.to_datetime(trades['entry_date'])
trades['exit_date'] = pd.to_datetime(trades['exit_date'])
metrics = result['metrics']

print("=" * 90)
print("  回测结果")
print("=" * 90)
final_equity = ENGINE_CFG['initial_capital'] * (1 + result['cumulative_return'])
print(f"  累计收益率:     {result['cumulative_return']*100:+.2f}%")
print(f"  期末权益:       {final_equity:,.0f} 元")
print(f"  年化收益率:     {metrics['annualized_return']*100:+.2f}%")
print(f"  最大回撤:       {metrics['max_drawdown']*100:.2f}%")
print(f"  夏普比率:       {metrics['sharpe_ratio']:.2f}")
print(f"  胜率:           {metrics['win_rate']*100:.1f}%")
win_cnt, lose_cnt = len(trades[trades['pnl']>0]), len(trades[trades['pnl']<=0])
print(f"  总交易:         {len(trades):,} 笔")
print(f"  盈利/亏损:      {win_cnt} / {lose_cnt}")
print(f"  平均持仓:       {trades['hold_days'].mean():.1f} 天")
print(f"  平均单笔盈亏:   {trades['pnl'].mean():,.0f} 元")
print(f"  最大单笔盈:     {trades['pnl'].max():,.0f} 元")
print(f"  最大单笔亏:     {trades['pnl'].min():,.0f} 元")

# 退出原因
print("\n" + "-" * 90)
print("  退出原因分布")
print("-" * 90)
reason_counts = trades['exit_reason'].value_counts()
reason_pnl = trades.groupby('exit_reason')['pnl'].agg(['sum', 'mean', 'count'])
for reason in reason_counts.index:
    cnt = reason_counts[reason]
    total_pnl = reason_pnl.loc[reason, 'sum']
    avg_pnl = reason_pnl.loc[reason, 'mean']
    pct = cnt / len(trades) * 100
    print(f"  {reason:<16s} {cnt:>6d}笔 ({pct:>5.1f}%)  总盈亏{total_pnl:>+12,.0f}  均值{avg_pnl:>+10,.0f}")

# 季度明细
print("\n" + "=" * 90)
print("  季度收益明细")
print("=" * 90)
trades['quarter'] = trades['exit_date'].dt.to_period('Q')
quarterly = trades.groupby('quarter').agg(
    profit=('pnl', 'sum'), trades=('pnl', 'count'),
    wins=('pnl', lambda x: (x > 0).sum()),
    losses=('pnl', lambda x: (x <= 0).sum()),
)
capital_q = ENGINE_CFG['initial_capital']
peak_q = ENGINE_CFG['initial_capital']
print(f"  {'季度':<10} {'盈亏':>12} {'收益率':>8} {'交易':>7} {'胜率':>7} {'期末权益':>14} {'回撤':>7}")
print("  " + "-" * 70)
for q in quarterly.index:
    p = quarterly.loc[q, 'profit']
    cnt = int(quarterly.loc[q, 'trades'])
    w = int(quarterly.loc[q, 'wins'])
    l = int(quarterly.loc[q, 'losses'])
    wr = w/cnt*100 if cnt>0 else 0
    capital_q += p
    if capital_q > peak_q: peak_q = capital_q
    dd = (capital_q - peak_q)/peak_q*100
    ret = p/(capital_q-p)*100
    print(f"  {str(q):<10} {p:>+12,.0f} {ret:>7.2f}% {cnt:>7} {wr:>6.1f}% {capital_q:>14,.0f} {dd:>6.2f}%")

# 月度明细
print("\n" + "=" * 90)
print("  月度收益明细")
print("=" * 90)
trades['ym'] = trades['exit_date'].dt.to_period('M')
monthly = trades.groupby('ym').agg(profit=('pnl', 'sum'), trades=('pnl', 'count'), wins=('pnl', lambda x: (x>0).sum()))
capital_m = ENGINE_CFG['initial_capital']
print(f"  {'月份':<10} {'盈亏':>12} {'月收益率':>9} {'交易':>7} {'胜率':>7} {'期末权益':>14}")
print("  " + "-" * 65)
for m in monthly.index:
    p = monthly.loc[m, 'profit']
    cnt = int(monthly.loc[m, 'trades'])
    w = int(monthly.loc[m, 'wins'])
    capital_m += p
    ret = p/(capital_m-p)*100
    wr = w/cnt*100 if cnt>0 else 0
    print(f"  {str(m):<10} {p:>+12,.0f} {ret:>8.2f}% {cnt:>7} {wr:>6.1f}% {capital_m:>14,.0f}")

print("=" * 90)
