"""海龟突破全量回测 + TOP500股票共性分析

1. 全量股票（无500限制）回测 2022-01-01 ~ 2025-12-31
2. 季度/年度收益、回撤明细
3. TOP500股票共性：成分股归属、市值区间、行业分布
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import numpy as np
import json
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine
from backtest.stop_config import load_stop_config
from utils.logger import get_logger
logger = get_logger(__name__)

TdxConnector.ensure_connected()

START = '20220101'
END   = '20251231'

print("=" * 100)
print("  VERA 海龟突破策略 — 全量股票回测 + TOP500 共性分析")
print("  Engine v3.2 | 无前视偏差 | 2022-2025 全区间")
print("=" * 100)

# ═══════════════════════════════════════════════════════════════
# 1. 获取全量数据
# ═══════════════════════════════════════════════════════════════
print("\n[1/5] 获取K线数据...", flush=True)
codes = DataFetcher.get_stock_universe('50')
k = DataFetcher.get_kline(codes, START, END, dividend_type="front", period="1d")
close = k["Close"].sort_index()
high = k["High"].sort_index()
low = k["Low"].sort_index()

valid = close.notna().sum() > 200
close = close.loc[:, valid]
high = high.reindex(columns=close.columns)
low = low.reindex(columns=close.columns)
print(f"      数据: {close.shape[0]} 交易日 × {close.shape[1]} 只股票")

# ═══════════════════════════════════════════════════════════════
# 2. 生成海龟突破信号（Python实现 = QUANTQQ同款逻辑）
# ═══════════════════════════════════════════════════════════════
print("\n[2/5] 生成海龟突破信号...", flush=True)
high_20 = high.rolling(20, min_periods=20).max()
high_10 = high.rolling(10, min_periods=10).max()
signal = (close > high_20.shift(1)) & (close.shift(1) > high_10.shift(2))

selections = []
for col in signal.columns:
    for idx in signal.index[signal[col]]:
        selections.append({'stock_code': col, 'select_date': idx.strftime('%Y-%m-%d')})
sel_df = pd.DataFrame(selections)

# 统计每只股票信号次数（用于后续TOP500分析）
signal_counts = sel_df['stock_code'].value_counts()
total_stocks = len(signal_counts)
total_signals = len(sel_df)
print(f"      信号总数: {total_signals:,}")
print(f"      股票总数: {total_stocks}")

# ═══════════════════════════════════════════════════════════════
# 3. 全量回测（不做任何限制）
# ═══════════════════════════════════════════════════════════════
print("\n[3/5] 运行全量回测...", flush=True)

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

STOP_CONFIG = {
    'cost_stop':      {'enabled': True,  'threshold': -0.12},
    'trailing_stop':  {'enabled': True,  'activation': 0.08, 'drawdown': 0.05},
    'ladder_tp':      {'enabled': True,  'levels': [
        {'profit': 0.06, 'sell_ratio': 0.3},
        {'profit': 0.15, 'sell_ratio': 0.3},
    ]},
    'time_stop':      {'enabled': True,  'max_hold_days': 20},
    'cond_time_stop': {'enabled': True,  'days': 7, 'profit': 0.02},
}

engine = BacktestEngine(ENGINE_CFG)
ladder_profits = np.array([0.06, 0.15], dtype=np.float64)
ladder_ratios  = np.array([0.30, 0.30], dtype=np.float64)
n_ladder = 2

result = engine.run_cached(c, entries,
                           h.values.astype(np.float64),
                           l.values.astype(np.float64),
                           STOP_CONFIG, sel_df,
                           ladder_profits, ladder_ratios, n_ladder,
                           skip_sm=True)

# ═══════════════════════════════════════════════════════════════
# 4. 报告输出
# ═══════════════════════════════════════════════════════════════
trades = result['trades']
trades['entry_date'] = pd.to_datetime(trades['entry_date'])
trades['exit_date']  = pd.to_datetime(trades['exit_date'])
metrics = result['metrics']

print("\n" + "=" * 100)
print("  【全量回测总览】2022.1.1 ~ 2025.12.31")
print("=" * 100)
final_equity = ENGINE_CFG['initial_capital'] * (1 + result['cumulative_return'])
print(f"  累计收益率:     {result['cumulative_return']*100:+.2f}%")
print(f"  期末权益:       {final_equity:,.0f} 元")
print(f"  年化收益率:     {metrics['annualized_return']*100:+.2f}%")
print(f"  最大回撤:       {metrics['max_drawdown']*100:.2f}%")
print(f"  夏普比率:       {metrics['sharpe_ratio']:.2f}")
print(f"  胜率:           {metrics['win_rate']*100:.1f}%")
print(f"  盈亏比:         {metrics.get('profit_loss_ratio', 0):.2f}")
print(f"  总交易:         {len(trades):,} 笔")
win_cnt = len(trades[trades['pnl']>0])
lose_cnt = len(trades[trades['pnl']<=0])
print(f"  盈利/亏损:      {win_cnt} / {lose_cnt}")
print(f"  平均持仓:       {trades['hold_days'].mean():.1f} 天")
print(f"  平均单笔盈亏:   {trades['pnl'].mean():,.0f} 元")
print(f"  最大单笔盈:     {trades['pnl'].max():,.0f} 元")
print(f"  最大单笔亏:     {trades['pnl'].min():,.0f} 元")
print(f"  参与股票数:     {trades['stock_code'].nunique()}")

# ── 退出原因 ──
print("\n" + "-" * 100)
print("  【退出原因分布】")
print("-" * 100)
reason_counts = trades['exit_reason'].value_counts()
reason_pnl = trades.groupby('exit_reason')['pnl'].agg(['sum', 'mean', 'count'])
for reason in reason_counts.index:
    cnt = reason_counts[reason]
    if reason not in reason_pnl.index: continue
    total_pnl = reason_pnl.loc[reason, 'sum']
    avg_pnl = reason_pnl.loc[reason, 'mean']
    pct = cnt / len(trades) * 100
    print(f"  {reason:<16s} {cnt:>6d}笔 ({pct:>5.1f}%)  总盈亏{total_pnl:>+12,.0f}  均值{avg_pnl:>+10,.0f}")

# ── 年度汇总 ──
print("\n" + "=" * 100)
print("  【年度收益汇总】")
print("=" * 100)
trades['year'] = trades['exit_date'].dt.year
yearly = trades.groupby('year')['pnl'].sum()
yearly_trades = trades.groupby('year').size()
yearly_wins = trades[trades['pnl']>0].groupby('year').size()
yearly_loss = trades[trades['pnl']<=0].groupby('year').size()

capital = ENGINE_CFG['initial_capital']
print(f"  {'年份':<8} {'盈利':>14} {'收益率':>10} {'交易数':>8} {'胜率':>8} {'期末权益':>15}")
print("  " + "-" * 70)
for y in yearly.index:
    profit = yearly[y]
    cnt = yearly_trades[y]
    win_cnt_y = yearly_wins.get(y, 0)
    win_rate = win_cnt_y / cnt * 100 if cnt > 0 else 0
    capital += profit
    ret = profit / (capital - profit) * 100
    print(f"  {int(y):<8} {profit:>+14,.0f} {ret:>9.2f}% {cnt:>8} {win_rate:>7.1f}% {capital:>15,.0f}")

# ── 季度明细 ──
print("\n" + "=" * 100)
print("  【季度收益明细】")
print("=" * 100)
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

capital_q = ENGINE_CFG['initial_capital']
peak_equity = ENGINE_CFG['initial_capital']
quarterly_equity = []
quarterly_dd = []
quarterly_peak = []
for q in quarterly.index:
    capital_q += quarterly.loc[q, 'profit']
    quarterly_equity.append(capital_q)
    if capital_q > peak_equity:
        peak_equity = capital_q
    q_dd = (capital_q - peak_equity) / peak_equity * 100
    quarterly_dd.append(q_dd)
    quarterly_peak.append(peak_equity)

quarterly['equity'] = quarterly_equity
quarterly['dd_pct'] = quarterly_dd
quarterly['peak'] = quarterly_peak
quarterly['ret_pct'] = quarterly['profit'] / (quarterly['equity'] - quarterly['profit']) * 100

print(f"  {'季度':<10} {'盈亏':>12} {'收益率':>8} {'交易':>7} {'胜率':>7} {'均值盈亏':>10} {'最大盈利':>10} {'最大亏损':>10} {'期末权益':>14} {'回撤':>7}")
print("  " + "-" * 100)
for q in quarterly.index:
    row = quarterly.loc[q]
    wr = row['wins'] / row['trades'] * 100 if row['trades'] > 0 else 0
    print(f"  {str(q):<10} {row['profit']:>+12,.0f} {row['ret_pct']:>7.2f}% {int(row['trades']):>7} {wr:>6.1f}% {row['avg_pnl']:>+10,.0f} {row['max_win']:>+10,.0f} {row['max_loss']:>+10,.0f} {row['equity']:>14,.0f} {row['dd_pct']:>6.2f}%")

# ── 月度明细 ──
print("\n" + "=" * 100)
print("  【月度收益明细】")
print("=" * 100)
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

print(f"  {'月份':<10} {'盈亏':>12} {'月收益率':>9} {'交易数':>7} {'胜率':>7} {'期末权益':>14}")
print("  " + "-" * 65)
for m in monthly.index:
    row = monthly.loc[m]
    wr = row['wins'] / row['trades'] * 100 if row['trades'] > 0 else 0
    print(f"  {str(m):<10} {row['profit']:>+12,.0f} {row['ret_pct']:>8.2f}% {int(row['trades']):>7} {wr:>6.1f}% {row['equity']:>14,.0f}")

# ── 年度回撤 ──
print("\n" + "=" * 100)
print("  【年度最大回撤】")
print("=" * 100)
eq = result.get('equity_curve', None)
if eq is not None and not eq.empty:
    eq['year'] = pd.to_datetime(eq['date']).dt.year
    for y in sorted(eq['year'].unique()):
        ye = eq[eq['year'] == y]
        peak_y = ye['equity'].expanding().max()
        dd_y = (ye['equity'] - peak_y) / peak_y
        max_dd_y = dd_y.min() * 100
        print(f"  {int(y)} 年最大回撤: {max_dd_y:.2f}%")

# ═══════════════════════════════════════════════════════════════
# 5. TOP500 股票共性分析
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 100)
print("  【TOP500 股票共性分析】")
print("=" * 100)

top500 = signal_counts.head(500)
top500_codes = top500.index.tolist()
top500_counts = top500.values
print(f"\n  TOP500 信号频次范围: {top500_counts.min()} ~ {top500_counts.max()} 次")
print(f"  TOP500 平均频次:     {top500_counts.mean():.1f} 次")
print(f"  TOP500 占总信号:     {top500_counts.sum() / total_signals * 100:.1f}%")

# 5.1 成分股分析
print("\n  [5.1] 指数成分股归属分析...", flush=True)

# 获取各指数成分股
index_map = {}
for idx_name, idx_code in DataFetcher.INDEX_CODES.items():
    try:
        from tqcenter import tq
        # 尝试获取指数成分
        pass
    except Exception as e:
        logger.warning("Index lookup failed: %s", e)

# 用 TDX 获取板块分类
from tqcenter import tq

# 板块→代码映射（TDX板块代码）
sector_map = {
    '上证主板': 'SH600',  # 近似
    '深证主板': 'SZ000',
    '创业板': 'SZ300',
    '科创板': 'SH688',
}

# 按代码前缀统计板块分布
def classify_board(code):
    if code.startswith('688'):
        return '科创板'
    elif code.startswith('300') or code.startswith('301'):
        return '创业板'
    elif code.startswith('60'):
        return '上证主板'
    elif code.startswith('00') or code.startswith('001') or code.startswith('002') or code.startswith('003'):
        return '深证主板'
    else:
        return '其他'

top500_board = pd.Series([classify_board(c) for c in top500_codes]).value_counts()
all_board = pd.Series([classify_board(c) for c in signal_counts.index]).value_counts()

print(f"  {'板块':<12} {'TOP500':>10} {'TOP500占比':>12} {'全量':>10} {'全量占比':>12} {'富集度':>10}")
print("  " + "-" * 65)
for board in ['上证主板', '深证主板', '创业板', '科创板', '其他']:
    t5 = top500_board.get(board, 0)
    al = all_board.get(board, 0)
    t5_pct = t5 / 500 * 100
    al_pct = al / len(all_board) * 100
    enrichment = t5_pct / al_pct if al_pct > 0 else 0
    print(f"  {board:<12} {t5:>10} {t5_pct:>11.1f}% {al:>10} {al_pct:>11.1f}% {enrichment:>10.2f}x")

# 5.2 市值分析（获取最新市值）
print("\n  [5.2] 市值区间分析（需要获取市值数据）...", flush=True)

# 用TDX获取市值数据
try:
    # 获取股票基本信息（含市值）
    stock_info = tq.get_security_info(top500_codes[:100] + signal_counts.index[:100].tolist())
    print(f"  get_security_info 返回: {type(stock_info)}")
    if stock_info:
        print(f"  keys: {list(stock_info.keys())[:5]}")
        # 查看第一个股票的数据结构
        first_key = list(stock_info.keys())[0]
        print(f"  示例 [{first_key}]: {stock_info[first_key]}")
except Exception as e:
    print(f"  get_security_info 失败: {e}")

# 尝试用tdx.get_stock_basic_info
try:
    # 另一种方式
    info = tq.get_stock_basic_info(top500_codes[:10])
    print(f"  get_stock_basic_info: {type(info)}")
    if info and len(info) > 0:
        print(f"  columns: {list(info[0].keys()) if isinstance(info[0], dict) else 'not dict'}")
except Exception as e:
    print(f"  get_stock_basic_info 失败: {e}")

# 尝试用财务数据获取市值
try:
    # 获取市值数据
    cap_result = tq.get_financial_data(
        stock_list=top500_codes[:20],
        field_list=['TOTALSHARES'],  # 总股本
        start_time='20251201',
        end_time='20251231',
        report_type='announce_time'
    )
    print(f"  财务数据返回: ErrorId={cap_result.get('ErrorId','?')}")
    # 看结构
    for k, v in list(cap_result.items())[:3]:
        if k == 'ErrorId': continue
        print(f"  [{k}]: {type(v)}")
        if isinstance(v, dict):
            for k2, v2 in list(v.items())[:2]:
                print(f"    {k2}: {type(v2)} len={len(v2) if isinstance(v2, list) else 'N/A'}")
                if isinstance(v2, list) and len(v2) > 0:
                    print(f"      first: {v2[0]}")
        break
except Exception as e:
    print(f"  财务数据失败: {e}")

# ═══════════════════════════════════════════════════════════════
# 5.3 替代方案：基于代码段和已知特征的分析
# ═══════════════════════════════════════════════════════════════
print("\n  [5.3] 基于代码特征的深度分析", flush=True)

# 交易市场分析
top500_exchange = pd.Series([
    'SH' if c.startswith('6') or c.startswith('5') else 'SZ'
    for c in top500_codes
]).value_counts()
all_exchange = pd.Series([
    'SH' if c.startswith('6') or c.startswith('5') else 'SZ'
    for c in signal_counts.index
]).value_counts()

print(f"  交易所分布:")
print(f"    沪市 TOP500: {top500_exchange.get('SH',0)} ({top500_exchange.get('SH',0)/5:.1f}%)")
print(f"    深市 TOP500: {top500_exchange.get('SZ',0)} ({top500_exchange.get('SZ',0)/5:.1f}%)")
print(f"    沪市 全量: {all_exchange.get('SH',0)} ({all_exchange.get('SH',0)/len(all_exchange)*100:.1f}%)")
print(f"    深市 全量: {all_exchange.get('SZ',0)} ({all_exchange.get('SZ',0)/len(all_exchange)*100:.1f}%)")

# 代码段分析（002xxx vs 000xxx vs 300xxx vs 688xxx vs 60xxxx）
top500_prefix = pd.Series([c[:3] for c in top500_codes]).value_counts()
all_prefix = pd.Series([c[:3] for c in signal_counts.index]).value_counts()

print(f"\n  代码前缀分布:")
print(f"  {'前缀':<8} {'板块含义':<15} {'TOP500':>8} {'TOP500%':>9} {'全量':>8} {'全量%':>9} {'差异':>8}")
print("  " + "-" * 65)
prefix_label = {
    '600': '上证主板', '601': '上证主板', '603': '上证主板', '605': '上证主板',
    '000': '深证主板', '001': '深证主板', '002': '中小板', '003': '中小板',
    '300': '创业板', '301': '创业板',
    '688': '科创板',
}
for prefix in ['600','601','603','605','000','001','002','003','300','301','688']:
    t5 = top500_prefix.get(prefix, 0)
    al = all_prefix.get(prefix, 0)
    if al == 0: continue
    t5p = t5 / 500 * 100
    alp = al / len(all_prefix) * 100
    diff = t5p - alp
    label = prefix_label.get(prefix, '未知')
    print(f"  {prefix:<8} {label:<15} {t5:>8} {t5p:>8.1f}% {al:>8} {alp:>8.1f}% {diff:>+8.1f}%")

# 汇总板块
print(f"\n  板块汇总:")
cats = {'上证主板': ['600','601','603','605'], '深证主板': ['000','001'],
        '中小板': ['002','003'], '创业板': ['300','301'], '科创板': ['688']}
for cat, prefixes in cats.items():
    t5_sum = sum(top500_prefix.get(p, 0) for p in prefixes)
    al_sum = sum(all_prefix.get(p, 0) for p in prefixes)
    t5p = t5_sum / 500 * 100
    alp = al_sum / len(all_prefix) * 100
    diff = t5p - alp
    print(f"  {cat:<12} TOP500={t5_sum:>4} ({t5p:>5.1f}%)  全量={al_sum:>5} ({alp:>5.1f}%)  差异={diff:>+6.1f}%")

# 5.4 TOP500中的知名股票 vs 冷门股
print(f"\n  [5.4] TOP10 股票详情")
for i, (code, cnt) in enumerate(top500.head(10).items(), 1):
    # 尝试获取股票名称
    try:
        code_str = str(code)
        name = code_str  # fallback
        print(f"  {i:>2}. {code:<12} 信号{cnt}次")
    except Exception as e:
        logger.warning("Stock name lookup failed: %s", e)
        print(f"  {i:>2}. {code:<12} 信号{cnt}次")

# ═══════════════════════════════════════════════════════════════
# 保存报告
# ═══════════════════════════════════════════════════════════════
output = {
    'meta': {
        'engine': 'v3.2-exec-price-20260603',
        'period': f'{START}~{END}',
        'universe': '全量（无500限制）',
        'signal_method': 'Python滚动HHV = TDX QUANTQQ同款',
    },
    'summary': {
        'cumulative_return': result['cumulative_return'],
        'final_equity': final_equity,
        'annualized_return': metrics['annualized_return'],
        'max_drawdown': metrics['max_drawdown'],
        'sharpe_ratio': metrics['sharpe_ratio'],
        'win_rate': metrics['win_rate'],
        'total_trades': len(trades),
        'win_trades': win_cnt,
        'lose_trades': lose_cnt,
    },
    'yearly': [{'year': int(y), 'profit': float(yearly[y]), 'trades': int(yearly_trades[y])} for y in yearly.index],
    'quarterly': quarterly.reset_index().to_dict('records'),
    'monthly': monthly.reset_index().to_dict('records'),
    'top500_analysis': {
        'total_stocks': total_stocks,
        'total_signals': int(total_signals),
        'top500_signal_range': [int(top500_counts.min()), int(top500_counts.max())],
        'top500_signal_pct': float(top500_counts.sum() / total_signals * 100),
        'board_distribution': {
            'top500': top500_board.to_dict(),
            'all': all_board.to_dict(),
        },
        'prefix_distribution': {
            'top500': top500_prefix.to_dict(),
            'all': all_prefix.to_dict(),
        },
    },
}

with open('output/backtest_full_analysis.json', 'w', encoding='utf-8') as f:
    json.dump(output, f, ensure_ascii=False, indent=2, default=str)

print(f"\n  报告已保存: output/backtest_full_analysis.json")
print("=" * 100)
