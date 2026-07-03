"""方案A+B综合优化 — 扩展区间+延长持仓+激进止盈+多策略组合"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine

TdxConnector.ensure_connected()

# 方案A：扩展回测区间到4年
START, END = '20220101', '20260528'  # 4年+

ENGINE_CFG = {
    'initial_capital': 200000.0, 'commission': 0.0003, 'slippage': 0.001, 'period': '1d',
    'position_sizing': {'min_buy_amount': 2000.0, 'max_buy_amount': 10000.0, 'lot_size': 100, 'min_lots': 1},
}

def to_sel(sig_df):
    recs = []
    for c in sig_df.columns:
        for d in sig_df.index[sig_df[c]]:
            recs.append({'stock_code':c,'select_date':d.strftime('%Y-%m-%d')})
    return pd.DataFrame(recs)

# ============ 策略库 ============
def strat_m40_v(close, high, low, vol):
    """最优基线：M40x10+T60+V"""
    ma40 = close.rolling(40, min_periods=40).mean()
    sm = ma40.rolling(10, min_periods=10).mean()
    cross = (ma40 > sm) & (ma40.shift(1) <= sm.shift(1))
    ma60 = close.rolling(60, min_periods=60).mean()
    vol_ma = vol.rolling(20, min_periods=20).mean()
    return cross.shift(1) & (sm > sm.shift(1)) & (close > ma60) & (vol > vol_ma * 1.5)

def strat_macd_kdj_rsi(close, high, low, vol):
    """MACD+KDJ+RSI"""
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26; dea = dif.ewm(span=9, adjust=False).mean()
    macd_cross = (dif > dea) & (dif.shift(1) <= dea.shift(1))

    low9 = low.rolling(9, min_periods=9).min(); high9 = high.rolling(9, min_periods=9).max()
    rsv = (close - low9) / (high9 - low9 + 1e-10) * 100
    k = rsv.ewm(com=2, adjust=False).mean(); d = k.ewm(com=2, adjust=False).mean()
    kdj_cross = (k > d) & (k.shift(1) <= d.shift(1)) & (k < 50)

    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(14, min_periods=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14, min_periods=14).mean()
    rsi = 100 - (100 / (1 + gain / (loss + 1e-10)))
    rsi_signal = (rsi > 30) & (rsi.shift(1) <= 30)
    return macd_cross & (kdj_cross | rsi_signal)

def strat_breakout(close, high, low, vol):
    """海龟突破"""
    high20 = high.rolling(20, min_periods=20).max()
    high10 = high.rolling(10, min_periods=10).max()
    return (close > high20.shift(1)) & (close.shift(1) > high10.shift(2))

def strat_momentum(close, high, low, vol):
    """多周期动量"""
    mom5 = close / close.shift(5) - 1; mom10 = close / close.shift(10) - 1; mom20 = close / close.shift(20) - 1
    vol_ma = vol.rolling(20, min_periods=20).mean()
    return (mom5 > 0) & (mom10 > 0) & (mom20 > 0) & (vol > vol_ma * 1.2)

def strat_ma_align(close, high, low, vol):
    """均线多头排列"""
    ma5 = close.rolling(5, min_periods=5).mean(); ma10 = close.rolling(10, min_periods=10).mean()
    ma20 = close.rolling(20, min_periods=20).mean(); ma60 = close.rolling(60, min_periods=60).mean()
    alignment = (ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60)
    breakout = (close > ma20) & (close.shift(1) <= ma20.shift(1))
    vol_ma = vol.rolling(20, min_periods=20).mean()
    return alignment & breakout & (vol > vol_ma * 1.5)

STRATEGIES = [
    ("M40+V[Best]", strat_m40_v),
    ("MACD+KDJ+RSI", strat_macd_kdj_rsi),
    ("Breakout", strat_breakout),
    ("Momentum", strat_momentum),
    ("MA-Align", strat_ma_align),
]

# ============ 数据获取 ============
print(f"获取数据 {START}~{END}...", flush=True)
t0 = time.time()
codes = DataFetcher.get_stock_universe('50')
k = DataFetcher.get_kline(codes, START, END, dividend_type="front", period="1d")
close = k["Close"].sort_index(); high = k["High"].sort_index()
low = k["Low"].sort_index(); vol = k.get("Volume", pd.DataFrame()).sort_index()
valid = close.notna().sum() > 200  # 4年至少200天
close = close.loc[:, valid]; high = high.reindex(columns=close.columns)
low = low.reindex(columns=close.columns); vol = vol.reindex(columns=close.columns) if not vol.empty else close * 0
print(f"数据: {close.shape} | {time.time()-t0:.0f}s", flush=True)

# ============ 生成信号 ============
print("生成策略信号...", flush=True)
all_sigs = {}
for label, func in STRATEGIES:
    sig = func(close, high, low, vol)
    sel = to_sel(sig)
    all_sigs[label] = {'sig': sig, 'sel': sel, 'n': len(sel), 'nc': len(sel['stock_code'].unique()) if len(sel) > 0 else 0}
    print(f"  {label}: {len(sel)}信号/{all_sigs[label]['nc']}只", flush=True)

# 组合信号（取并集）
combo_sig = pd.DataFrame(False, index=close.index, columns=close.columns)
for label in all_sigs.keys():
    combo_sig = combo_sig | all_sigs[label]['sig']
combo_sel = to_sel(combo_sig)
print(f"  [组合ALL]: {len(combo_sel)}信号/{len(combo_sel['stock_code'].unique())}只", flush=True)
all_sigs['Combo-ALL'] = {'sig': combo_sig, 'sel': combo_sel, 'n': len(combo_sel), 'nc': len(combo_sel['stock_code'].unique())}

# ============ 方案A：扩展持仓周期 + 激进止盈 ============
STOP_CONFIGS = [
    # 原最优（基线）
    ("Baseline:6%→100/T20", [{'p':0.06,'r':1.0}], -0.12, 0.05,0.03, 20),

    # 方案A1：延长持仓周期
    ("A1:6%→100/T30", [{'p':0.06,'r':1.0}], -0.12, 0.05,0.03, 30),
    ("A2:6%→100/T40", [{'p':0.06,'r':1.0}], -0.12, 0.05,0.03, 40),
    ("A3:6%→100/T60", [{'p':0.06,'r':1.0}], -0.12, 0.05,0.03, 60),

    # 方案A2：激进止盈梯度
    ("A4:3%→30,8%→50,15%→100/T30", [{'p':0.03,'r':0.30},{'p':0.08,'r':0.50},{'p':0.15,'r':1.0}], -0.12, 0.08,0.04, 30),
    ("A5:5%→30,10%→50,18%→100/T40", [{'p':0.05,'r':0.30},{'p':0.10,'r':0.50},{'p':0.18,'r':1.0}], -0.15, 0.08,0.04, 40),
    ("A6:4%→25,10%→50,20%→100/T60", [{'p':0.04,'r':0.25},{'p':0.10,'r':0.50},{'p':0.20,'r':1.0}], -0.15, 0.10,0.05, 60),

    # 方案A3：组合（长周期+激进止盈）
    ("A7:8%→100/T40", [{'p':0.08,'r':1.0}], -0.15, 0.08,0.04, 40),
    ("A8:10%→100/T60", [{'p':0.10,'r':1.0}], -0.15, 0.10,0.05, 60),
]

# ============ 回测 ============
engine = BacktestEngine(ENGINE_CFG)
results = []
count = 0; best = -999
total = len(all_sigs) * len(STOP_CONFIGS)
print(f"\n回测 {total} 组合...\n", flush=True)

for s_label, info in all_sigs.items():
    sel = info['sel']
    if len(sel) == 0: continue

    codes_f = sorted(sel['stock_code'].unique())
    if len(codes_f) > 500:  # 限制
        cnt = sel['stock_code'].value_counts()
        codes_f = sorted(cnt.head(500).index)
        sel = sel[sel['stock_code'].isin(codes_f)]

    common = sorted(set(close.columns) & set(codes_f))
    c = close[common].ffill().bfill()
    h = high.reindex(index=c.index, columns=common).ffill().bfill()
    l = low.reindex(index=c.index, columns=common).ffill().bfill()
    hn = h.values.astype(np.float64); ln = l.values.astype(np.float64)

    e = pd.DataFrame(False, index=c.index, columns=c.columns)
    for _, row in sel.iterrows():
        code = row['stock_code']; dt = pd.to_datetime(row['select_date'])
        if code not in e.columns: continue
        if dt in e.index: e.loc[dt, code] = True
        else:
            m = e.index >= dt
            if m.any(): e.loc[e.index[m][0], code] = True

    for stop_label, lv_raw, cost, ta, td, tdays in STOP_CONFIGS:
        count += 1
        lv = sorted(lv_raw, key=lambda x: x['p'])
        lp = np.array([x['p'] for x in lv], dtype=np.float64)
        lr = np.array([x['r'] for x in lv], dtype=np.float64)

        sc = {
            'cost_stop': {'enabled': True, 'threshold': cost},
            'trailing_stop': {'enabled': True, 'activation': ta, 'drawdown': td},
            'ladder_tp': {'enabled': True, 'levels': [{'profit':x['p'],'sell_ratio':x['r']} for x in lv]},
            'time_stop': {'enabled': True, 'max_hold_days': tdays},
            'cond_time_stop': {'enabled': True, 'days': 7, 'profit': 0.02},
        }

        result = engine.run_cached(c, e, hn, ln, sc, sel, lp, lr, len(lv), skip_sm=True)
        ret = result['cumulative_return']
        m = result['metrics']

        results.append({
            'cum_ret': float(ret), 'annual': float(m['annualized_return']),
            'max_dd': float(m['max_drawdown']), 'win_rate': float(m['win_rate']),
            'trades': int(len(result['trades'])), 'signals': int(info['n']),
            'strategy': s_label, 'stop': stop_label, 'n_stocks': len(codes_f)
        })

        if ret > best:
            best = ret
            print(f"*** [{count}/{total}] NEW BEST: {ret*100:+.2f}% 年{m['annualized_return']*100:+.1f}% 回{m['max_drawdown']*100:+.1f}% 胜{m['win_rate']*100:.0f}% {len(result['trades'])}笔", flush=True)
            print(f"    {s_label} | {stop_label}", flush=True)

        if count % 10 == 0:
            print(f"[{count}/{total}] best={best*100:+.2f}%", flush=True)

results.sort(key=lambda x: x['cum_ret'], reverse=True)

print(f"\n{'='*110}")
print(f"TOP 20 (共{len(results)}组合, 总耗时{time.time()-t0:.0f}s)")
print(f"{'='*110}")
for i, r in enumerate(results[:20]):
    print(f"{i+1:2d}. {r['cum_ret']*100:+6.2f}% 年{r['annual']*100:+6.1f}% 回{r['max_dd']*100:+5.1f}% 胜{r['win_rate']*100:3.0f}% {r['trades']:4d}笔 | {r['strategy']:15s} | {r['stop']}")

with open('output/optimize_final.json', 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

print(f"\n结果: output/optimize_final.json | 总耗时: {time.time()-t0:.0f}s")
