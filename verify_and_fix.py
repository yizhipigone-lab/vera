"""复盘验证 + 均线策略优化

1. 海龟复盘: 将TOP500最优配置放到全量股票池验证，揭露生存偏差
2. 均线策略: 放宽MA60条件，重新回测
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import numpy as np
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine
from utils.logger import get_logger
logger = get_logger(__name__)

TdxConnector.ensure_connected()
START, END = '20220101', '20251231'
START_SHORT, END_SHORT = '20240101', '20260603'

# ══════════════════════════════════════
# 公用引擎
# ══════════════════════════════════════
ENGINE_CFG = {
    'initial_capital': 1000000.0, 'commission': 0.0003, 'slippage': 0.001, 'period': '1d',
    'position_sizing': {'min_buy_amount': 2000.0, 'max_buy_amount': 20000.0, 'lot_size': 100, 'min_lots': 1},
}

def build_sel(sig_df, code_list):
    recs = []
    for col in code_list:
        if col not in sig_df.columns: continue
        for idx in sig_df.index[sig_df[col]]:
            recs.append({'stock_code': col, 'select_date': idx.strftime('%Y-%m-%d')})
    return pd.DataFrame(recs)

def run_bt(close_df, high_df, low_df, sig_df, codes_list, stop_config, ladder_p, ladder_r, n_ladder):
    sel = build_sel(sig_df, codes_list)
    if sel.empty or len(sel) < 50: return None
    common = sorted(set(close_df.columns) & set(sel['stock_code'].unique()))
    if len(common) < 10: return None
    cs = close_df[common].ffill().bfill()
    hs = high_df.reindex(index=cs.index, columns=common).ffill().bfill()
    ls = low_df.reindex(index=cs.index, columns=common).ffill().bfill()
    entries = pd.DataFrame(False, index=cs.index, columns=cs.columns)
    for _, row in sel.iterrows():
        code, dt = row['stock_code'], pd.to_datetime(row['select_date'])
        if code not in entries.columns: continue
        if dt in entries.index: entries.loc[dt, code] = True
        else:
            m = entries.index >= dt
            if m.any(): entries.loc[entries.index[m][0], code] = True
    engine = BacktestEngine(ENGINE_CFG)
    try:
        result = engine.run_cached(cs, entries, hs.values.astype(np.float64),
                                   ls.values.astype(np.float64), stop_config, sel,
                                   ladder_p, ladder_r, n_ladder, skip_sm=True)
    except Exception as e: logger.warning("run_cached failed: %s", e); return None
    m = result['metrics']
    return {
        'cumret': result['cumulative_return'], 'annret': m['annualized_return'],
        'maxdd': m['max_drawdown'], 'sharpe': m['sharpe_ratio'],
        'winrate': m['win_rate'], 'trades': len(result['trades']), 'signals': len(sel),
    }

# ══════════════════════════════════════
# 加载全量数据
# ══════════════════════════════════════
print("=" * 100)
print("  复盘验证 + 均线策略优化")
print("=" * 100)

print("\n加载数据...", flush=True)
codes = DataFetcher.get_stock_universe('50')
k = DataFetcher.get_kline(codes, '20200101', max(END, END_SHORT), dividend_type="front", period="1d")
close = k["Close"].sort_index()
high = k["High"].sort_index()
low = k["Low"].sort_index()
volume = k.get("Volume", pd.DataFrame()).sort_index()
valid = close.notna().sum() > 200
close = close.loc[:, valid]
high = high.reindex(columns=close.columns)
low = low.reindex(columns=close.columns)
volume = volume.reindex(columns=close.columns)

all_cols = close.columns.tolist()
print(f"  数据: {close.shape}")

def exclude_st(cl): return [c for c in cl if 'ST' not in c and '*ST' not in c]

# ═══════════════════════════════════════════════════════════
# PART A: 海龟复盘 — 最优配置在全量池上验证
# ═══════════════════════════════════════════════════════════
print("\n" + "=" * 100)
print("  PART A: 海龟复盘 — TOP500最优配置 → 全量股票池验证")
print("=" * 100)

# 计算海龟信号
print("  计算海龟信号...", flush=True)
hi20 = high.rolling(20, min_periods=20).max()
hi10 = high.rolling(10, min_periods=10).max()
turtle_sig = ((close > hi20.shift(1)) & (close.shift(1) > hi10.shift(2)))

ma60_t = close.rolling(60).mean()
ma120_t = close.rolling(120).mean()

turtle_signals = {
    'S0:基础': turtle_sig,
    'S5:>MA60': turtle_sig & (close > ma60_t),
    'S5:>MA120': turtle_sig & (close > ma120_t),
}

# TOP500 (生存偏差)
sig_counts_4y = turtle_sig.sum()
top500_codes = exclude_st(sig_counts_4y.nlargest(500).index.tolist())
top500_in_cols = [c for c in top500_codes if c in close.columns]
print(f"  TOP500(生存偏差): {len(top500_in_cols)} 只")

# 全量股票池
full_universe = exclude_st([c for c in all_cols if c in close.columns])
print(f"  全量(无ST): {len(full_universe)} 只")

# 无偏差替代池: 沪深300 + 中证500 成分股（模拟）
# 用代码段近似 + 活跃度底线
hs300_codes = exclude_st([c for c in all_cols if c.startswith('60') or c.startswith('000') or c.startswith('001')])
# 更合理的替代: 所有信号≥5次的 + 排除ST
min_active = sig_counts_4y[sig_counts_4y >= 5].index.tolist()
active_universe = exclude_st(min_active)
print(f"  活跃≥5次(无ST): {len(active_universe)} 只")

# 测试配置
test_configs = [
    ("Z1:-12/8+5/6:30:15:30", {'cost_stop':{'enabled':True,'threshold':-0.12},
         'trailing_stop':{'enabled':True,'activation':0.08,'drawdown':0.05},
         'ladder_tp':{'enabled':True,'levels':[{'profit':0.06,'sell_ratio':0.3},{'profit':0.15,'sell_ratio':0.3}]},
         'time_stop':{'enabled':True,'max_hold_days':20},
         'cond_time_stop':{'enabled':True,'days':7,'profit':0.02}},
     np.array([0.06,0.15],dtype=np.float64), np.array([0.30,0.30],dtype=np.float64), 2),
    ("Z2:-10/10+6/8:30:20:30", {'cost_stop':{'enabled':True,'threshold':-0.10},
         'trailing_stop':{'enabled':True,'activation':0.10,'drawdown':0.06},
         'ladder_tp':{'enabled':True,'levels':[{'profit':0.08,'sell_ratio':0.3},{'profit':0.20,'sell_ratio':0.3}]},
         'time_stop':{'enabled':True,'max_hold_days':20},
         'cond_time_stop':{'enabled':True,'days':7,'profit':0.02}},
     np.array([0.08,0.20],dtype=np.float64), np.array([0.30,0.30],dtype=np.float64), 2),
]

pools = [
    ("TOP500(生存偏差)", top500_in_cols),
    ("全量(无ST)", full_universe),
    ("活跃≥5次(无ST)", active_universe),
]

print(f"\n  {'信号':<18} {'股票池':<24} {'止损':<30} {'累计%':>8} {'年化%':>8} {'回撤%':>8} {'夏普':>6} {'交易':>7}")
print("  " + "-" * 100)

turtle_results = []
for sname, sig_raw in turtle_signals.items():
    sig = sig_raw.loc[START:END]
    for pname, pool in pools:
        cl = [c for c in pool if c in sig.columns]
        if len(cl) < 30: continue
        for zname, stop_cfg, lp, lr, nl in test_configs:
            r = run_bt(close, high, low, sig, cl, stop_cfg, lp, lr, nl)
            if r is None: continue
            r['sig'] = sname; r['pool'] = pname; r['stop'] = zname
            turtle_results.append(r)
            mark = " [生存偏差]" if "TOP500" in pname else " [真实]"
            print(f"  {sname:<18} {pname:<24} {zname:<30} {r['cumret']*100:>+8.2f}% {r['annret']*100:>+8.2f}% {r['maxdd']*100:>+8.2f}% {r['sharpe']:>6.2f} {r['trades']:>7,}{mark}")

# ═══════════════════════════════════════════════════════════
# PART B: 均线策略 — MA60条件放宽
# ═══════════════════════════════════════════════════════════
print("\n" + "=" * 100)
print("  PART B: 均线多头策略 — MA60条件放宽")
print("=" * 100)

print("  计算均线指标...", flush=True)
ma5 = close.rolling(5).mean()
ma10 = close.rolling(10).mean()
ma20 = close.rolling(20).mean()
ma60_b = close.rolling(60).mean()
ma20a = ma20.rolling(10).mean()

close_s = close.loc[START_SHORT:END_SHORT]
ma5_s = ma5.loc[START_SHORT:END_SHORT]
ma10_s = ma10.loc[START_SHORT:END_SHORT]
ma20_s = ma20.loc[START_SHORT:END_SHORT]
ma60_s = ma60_b.loc[START_SHORT:END_SHORT]
ma20a_s = ma20a.loc[START_SHORT:END_SHORT]
high_s = high.loc[START_SHORT:END_SHORT]
low_s = low.loc[START_SHORT:END_SHORT]

# 共用条件
ad = (ma5_s > ma10_s) & (ma10_s > ma20_s)
ab = (close_s / close_s.shift(1) < 1.08).rolling(10).min() > 0
ac = (low_s <= high_s.shift(1)).rolling(10).min() > 0
cross_up = (ma20_s > ma20a_s) & (ma20_s.shift(1) <= ma20a_s.shift(1))
upnday_ma10 = (ma10_s > ma10_s.shift(1)) & (ma10_s.shift(1) > ma10_s.shift(2))
upnday_ma5 = (ma5_s > ma5_s.shift(1)) & (ma5_s.shift(1) > ma5_s.shift(2))
bullish = ma20_s > ma60_s

# 三组MA60条件
# 原始: UPNDAY(MA60,20) — 极严格
upnday_ma60_20 = pd.DataFrame(True, index=ma60_s.index, columns=ma60_s.columns)
for i in range(1, 21):
    upnday_ma60_20 = upnday_ma60_20 & (ma60_s > ma60_s.shift(i))

# 放宽A: UPNDAY(MA60,5)
upnday_ma60_5 = pd.DataFrame(True, index=ma60_s.index, columns=ma60_s.columns)
for i in range(1, 6):
    upnday_ma60_5 = upnday_ma60_5 & (ma60_s > ma60_s.shift(i))

# 放宽B: MA60 > REF(MA60,10)
ma60_up_10d = ma60_s > ma60_s.shift(10)

base_cond = ad & ab & ac & upnday_ma10 & upnday_ma5 & bullish

ma_variants = [
    ("原始: UPNDAY(MA60,20)", base_cond & upnday_ma60_20),
    ("放宽A: UPNDAY(MA60,5)", base_cond & upnday_ma60_5),
    ("放宽B: MA60>REF(MA60,10)", base_cond & ma60_up_10d),
    ("组合: UPNDAY(MA60,5)+均线多头", base_cond & upnday_ma60_5),
]

full_clean = exclude_st([c for c in all_cols if c in close_s.columns])
print(f"  全量股票: {len(full_clean)} 只")

# 止盈止损: -6%硬止损 / +3%卖50% / +3%激活-3%回撤移动止盈 / 15天
stop_cfg_ma = {
    'cost_stop':      {'enabled': True,  'threshold': -0.06},
    'trailing_stop':  {'enabled': True,  'activation': 0.03, 'drawdown': 0.03},
    'ladder_tp':      {'enabled': True,  'levels': [{'profit': 0.03, 'sell_ratio': 0.5}]},
    'time_stop':      {'enabled': True,  'max_hold_days': 15},
}
lp_ma = np.array([0.03], dtype=np.float64)
lr_ma = np.array([0.50], dtype=np.float64)

print(f"\n  {'MA60条件':<30} {'信号数':>8} {'累计%':>8} {'年化%':>8} {'回撤%':>8} {'夏普':>6} {'交易':>7}")
print("  " + "-" * 80)

ma_results = []
for vname, vsig in ma_variants:
    sig_valid = vsig.astype(bool)
    r = run_bt(close, high, low, sig_valid, full_clean, stop_cfg_ma, lp_ma, lr_ma, 1)
    if r is None: continue
    r['sig'] = vname
    ma_results.append(r)
    print(f"  {vname:<30} {sig_valid.sum().sum():>8,} {r['cumret']*100:>+8.2f}% {r['annret']*100:>+8.2f}% {r['maxdd']*100:>+8.2f}% {r['sharpe']:>6.2f} {r['trades']:>7,}")

# 再用最优MA60条件 + 更好止盈止损试试
print(f"\n  尝试优化止盈止损...")
best_ma_variant = max(ma_results, key=lambda x: x['annret'])
best_ma_sig = None
for vname, vsig in ma_variants:
    if vname == best_ma_variant['sig']:
        best_ma_sig = vsig
        break

alt_stops = [
    ("硬止损-8%/+4%卖50%/+4%激活-4%回撤/20d", -0.08, 0.04, 0.04, [(0.04,0.5)], 20),
    ("硬止损-10%/+5%卖50%/+5%激活-5%回撤/20d", -0.10, 0.05, 0.05, [(0.05,0.5)], 20),
]

for sn, cost_thr, trail_act, trail_dd, ladder_lv, mh in alt_stops:
    stop_cfg = {
        'cost_stop':      {'enabled': True, 'threshold': cost_thr},
        'trailing_stop':  {'enabled': True, 'activation': trail_act, 'drawdown': trail_dd},
        'ladder_tp':      {'enabled': True, 'levels': [{'profit': p, 'sell_ratio': r} for p, r in ladder_lv]},
        'time_stop':      {'enabled': True, 'max_hold_days': mh},
    }
    lp = np.array([lv[0] for lv in ladder_lv], dtype=np.float64)
    lr = np.array([lv[1] for lv in ladder_lv], dtype=np.float64)
    r = run_bt(close, high, low, best_ma_sig, full_clean, stop_cfg, lp, lr, len(ladder_lv))
    if r is None: continue
    r['sig'] = f"{best_ma_variant['sig']}+{sn}"
    ma_results.append(r)
    print(f"  {r['sig']:<50} {r['cumret']*100:>+8.2f}% {r['annret']*100:>+8.2f}% {r['maxdd']*100:>+8.2f}% {r['sharpe']:>6.2f} {r['trades']:>7,}")

# ══════════════════════════════════════
# 总结
# ══════════════════════════════════════
print("\n" + "=" * 100)
print("  总结")
print("=" * 100)

print("\n  【海龟真实水平】")
for r in turtle_results:
    if 'TOP500' not in r['pool']:
        print(f"  [{r['sig']}] [{r['pool']}] [{r['stop']}] → 年化:{r['annret']*100:+.2f}% 累计:{r['cumret']*100:+.2f}% 回撤:{r['maxdd']*100:.2f}%")
    else:
        print(f"  [{r['sig']}] [{r['pool']}] [{r['stop']}] → 年化:{r['annret']*100:+.2f}%  ⚠️生存偏差!")

print("\n  【均线策略 — MA60放宽】")
for r in ma_results:
    print(f"  [{r['sig']}] → 年化:{r['annret']*100:+.2f}% 累计:{r['cumret']*100:+.2f}% 回撤:{r['maxdd']*100:.2f}% 交易:{r['trades']}")

print("=" * 100)
