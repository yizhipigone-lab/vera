"""公式优化 v4 — 1d频率全量搜索 + 前5用5m验证"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine

TdxConnector.ensure_connected()
START, END = '20260101', '20260528'
PERIOD = '1d'  # 先用1d快速扫描

ENGINE_1D = {
    'initial_capital': 200000.0, 'commission': 0.0003, 'slippage': 0.001, 'period': '1d',
    'position_sizing': {'min_buy_amount': 2000.0, 'max_buy_amount': 10000.0, 'lot_size': 100, 'min_lots': 1},
}

def gen_signals(close_daily, open_d=None, vol_d=None,
                ma_fast=20, ma_smooth=10, trend=False, vol_f=False, yang=False):
    ma = close_daily.rolling(ma_fast, min_periods=ma_fast).mean()
    sm = ma.rolling(ma_smooth, min_periods=ma_smooth).mean()
    cross = (ma > sm) & (ma.shift(1) <= sm.shift(1))
    s = cross.shift(1) & (sm > sm.shift(1))
    if trend:
        m60 = close_daily.rolling(60,min_periods=60).mean()
        s = s & (close_daily > m60)
    if vol_f and vol_d is not None:
        v20 = vol_d.rolling(20,min_periods=20).mean()
        s = s & (vol_d > v20 * 1.5)
    if yang and open_d is not None:
        s = s & (close_daily > open_d)
    return s.fillna(False)

def to_selections(sig_df):
    recs = []
    for c in sig_df.columns:
        for d in sig_df.index[sig_df[c]]:
            recs.append({'stock_code':c,'select_date':d.strftime('%Y-%m-%d')})
    return pd.DataFrame(recs)

# === Step 1: 获取1d数据 ===
print("Step 1: 获取1d OHLC...", flush=True)
t0 = time.time()
codes = DataFetcher.get_stock_universe('50')
k1d = DataFetcher.get_kline(codes, START, END, dividend_type="front", period="1d")
close = k1d["Close"].sort_index()
high = k1d["High"].sort_index()
low = k1d["Low"].sort_index()
open_p = k1d["Open"].sort_index()
vol = k1d.get("Volume", pd.DataFrame(index=close.index)).sort_index()
valid = close.notna().sum() > 60
close = close.loc[:, valid]
high = high.reindex(columns=close.columns)
low = low.reindex(columns=close.columns)
open_p = open_p.reindex(columns=close.columns)
if not vol.empty: vol = vol.reindex(columns=close.columns)
print(f"1d: {close.shape} stocks | {time.time()-t0:.0f}s", flush=True)

# === Step 2: 公式网格 ===
FORMULA_GRID = []
for mf, ms in [(20,10),(30,10),(30,15),(40,10),(60,20),(50,20),(15,5),(10,5),(20,5),(25,10)]:
    for trend in [False, True]:
        for vf in [False, True]:
            for yang in [False, True]:
                if not trend and not vf and not yang:
                    continue  # 跳过无过滤（跟第一组重复）
                if vf and vol.empty: continue
                FORMULA_GRID.append((mf, ms, trend, vf, yang,
                    f"M{mf}x{ms}" + ("+T60" if trend else "") + ("+V" if vf else "") + ("+Y" if yang else "")))
# 加入无过滤的基线
for mf, ms in [(20,10),(30,10),(30,15),(40,10),(60,20),(50,20),(15,5),(10,5)]:
    FORMULA_GRID.insert(0, (mf, ms, False, False, False, f"M{mf}x{ms}"))

# 去重
seen = set(); unique = []
for x in FORMULA_GRID:
    key = x[5]
    if key not in seen: seen.add(key); unique.append(x)
FORMULA_GRID = unique
print(f"公式: {len(FORMULA_GRID)}", flush=True)

# === Step 3: 生成所有公式的信号 ===
print("Step 2: 生成信号...", flush=True)
all_signals = {}
for mf, ms, trend, vf, yang, label in FORMULA_GRID:
    sig = gen_signals(close, open_p, vol if vf else None, mf, ms, trend, vf, yang)
    n = sig.sum().sum()
    nc = (sig.sum() > 0).sum()
    all_signals[label] = {'sig': sig, 'n': n, 'nc': nc, 'mf': mf, 'ms': ms, 'trend': trend, 'vf': vf, 'yang': yang}
    if nc <= 1000:
        print(f"  {label}: {int(n)}信号/{int(nc)}只", flush=True)

# === Step 4: 1d回测全量搜索（仅选股<1500只的公式）===
STOP_GRID = [
    ("6%→100/C-12/T20", [{'p':0.06,'r':1.0}], -0.12, 0.05,0.03, 20),
    ("3%→30,6%→100/C-12", [{'p':0.03,'r':0.30},{'p':0.06,'r':1.0}], -0.12, 0.08,0.04, 20),
    ("4%→100/C-12/T20", [{'p':0.04,'r':1.0}], -0.12, 0.05,0.03, 20),
    ("2%→30,5%→100/C-10", [{'p':0.02,'r':0.30},{'p':0.05,'r':1.0}], -0.10, 0.08,0.04, 20),
    ("3%→33,6%→50,10%→100/C-12", [{'p':0.03,'r':0.33},{'p':0.06,'r':0.50},{'p':0.10,'r':1.0}], -0.12, 0.08,0.04, 20),
    ("3%→100/C-8/T15", [{'p':0.03,'r':1.0}], -0.08, 0.05,0.03, 15),
    ("5%→100/C-12/T20", [{'p':0.05,'r':1.0}], -0.12, 0.05,0.03, 20),
]

engine_1d = BacktestEngine(ENGINE_1D)
results_1d = []
best = -999
count = 0
total = sum(1 for v in all_signals.values() if v['nc'] <= 1500) * len(STOP_GRID)
print(f"\nStep 3: 1d回测 {total}组合 (仅选<1500只公式)...", flush=True)

for f_label, info in all_signals.items():
    if info['nc'] > 1500 or info['n'] == 0: continue
    sig = info['sig']
    sel = to_selections(sig)
    codes_f = sorted(sel['stock_code'].unique())

    # 用相同列对齐
    common = sorted(set(close.columns) & set(codes_f))
    c = close[common].ffill().bfill()
    h = high.reindex(index=c.index, columns=common).ffill().bfill()
    l = low.reindex(index=c.index, columns=common).ffill().bfill()
    hn = h.values.astype(np.float64)
    ln = l.values.astype(np.float64)

    # entries
    e = pd.DataFrame(False, index=c.index, columns=c.columns)
    for _, row in sel.iterrows():
        code = row['stock_code']; dt = pd.to_datetime(row['select_date'])
        if code not in e.columns: continue
        if dt in e.index: e.loc[dt, code] = True
        else:
            m = e.index >= dt
            if m.any(): e.loc[e.index[m][0], code] = True

    for s_label, lv_raw, cost, ta, td, tdays in STOP_GRID:
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

        result = engine_1d.run_cached(c, e, hn, ln, sc, sel, lp, lr, len(lv), skip_sm=True)
        ret = result['cumulative_return']
        n_tr = len(result['trades'])
        m = result['metrics']

        results_1d.append({'cum_ret': ret, 'annual': m['annualized_return'], 'max_dd': m['max_drawdown'],
                           'win_rate': m['win_rate'], 'trades': n_tr,'signals': info['n'],
                           'formula': f_label, 'stop': s_label, 'n_stocks': info['nc']})

        if ret > best:
            best = ret
            print(f"  *** BEST [{count}/{total}]: {ret*100:+.2f}% 年{m['annualized_return']*100:+.1f}% | {f_label} {s_label}", flush=True)
        if count % 50 == 0:
            print(f"  [{count}/{total}] best={best*100:+.2f}%", flush=True)

results_1d.sort(key=lambda x: x['cum_ret'], reverse=True)

print(f"\n{'='*100}")
print(f"1d TOP 20 (共{len(results_1d)}组合, 耗时{time.time()-t0:.0f}s)")
print(f"{'='*100}")
for i, r in enumerate(results_1d[:20]):
    print(f"{i+1:2d}. {r['cum_ret']*100:+.2f}% 年{r['annual']*100:+.1f}% 回{r['max_dd']*100:+.1f}% 胜{r['win_rate']*100:.0f}% {r['trades']}笔 | {r['formula']:25s} | {r['stop']}")

# === Step 5: 前5公式用5m验证 ===
print(f"\n{'='*100}")
print("Step 4: 前5公式用5m验证...")
print(f"{'='*100}")

ENGINE_5M = {**ENGINE_1D, 'period': '5m'}
engine_5m = BacktestEngine(ENGINE_5M)

top5_formulas = list(dict.fromkeys(r['formula'] for r in results_1d[:5]))  # 去重取前5

for f_label in top5_formulas[:5]:
    info = all_signals[f_label]
    sig = info['sig']
    sel = to_selections(sig)
    codes_f = sorted(sel['stock_code'].unique())
    if len(codes_f) > 500:
        # 取信号最多的前500只
        cnt = sel['stock_code'].value_counts()
        codes_f = sorted(cnt.head(500).index)
        sel = sel[sel['stock_code'].isin(codes_f)]

    print(f"\n{f_label}: {len(sel)}信号/{len(codes_f)}只, 取5m数据...", flush=True)
    try:
        k5 = DataFetcher.get_kline(codes_f, START, END, dividend_type="front", period="5m")
    except Exception as e:
        print(f"  5m失败: {e}", flush=True)
        continue

    c5 = k5["Close"].sort_index()
    h5 = k5["High"].sort_index(); l5 = k5["Low"].sort_index()

    # entries
    e5 = pd.DataFrame(False, index=c5.index, columns=c5.columns)
    for _, row in sel.iterrows():
        code = row['stock_code']; dt = pd.to_datetime(row['select_date'])
        if code not in e5.columns: continue
        m = e5.index >= dt
        if m.any():
            fb = e5.index[m][0]
            dm = e5.index.normalize() == fb.normalize()
            e5.loc[e5.index[dm][-1] if dm.any() else fb, code] = True

    cols = sorted(c5.columns.intersection(e5.columns))
    c = c5[cols].ffill().bfill(); e = e5.reindex(index=c.index, columns=cols, fill_value=False)
    fcols = cols
    hn = ln = None
    if h5 is not None:
        hc = sorted(set(fcols) & set(h5.columns))
        ht = h5.reindex(index=c.index, columns=hc).ffill().bfill()
        fcols = sorted(set(fcols) & set(ht.columns))
        c, e = c[fcols], e[fcols]; hn = ht[fcols].values.astype(np.float64)
    if l5 is not None:
        lc = sorted(set(fcols) & set(l5.columns))
        lt = l5.reindex(index=c.index, columns=lc).ffill().bfill()
        fcols2 = sorted(set(fcols) & set(lt.columns))
        c, e = c[fcols2], e[fcols2]
        if hn is not None: hn = ht[fcols2].values.astype(np.float64)
        ln = lt[fcols2].values.astype(np.float64)

    # 测试该公式最优的2个止损配置
    best_stops = [r['stop'] for r in results_1d if r['formula'] == f_label][:2]
    for si, (s_label, lv_raw, cost, ta, td, tdays) in enumerate(STOP_GRID):
        if s_label not in best_stops: continue
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
        result = engine_5m.run_cached(c, e, hn, ln, sc, sel, lp, lr, len(lv), skip_sm=True)
        ret = result['cumulative_return']
        m = result['metrics']
        print(f"  5m: {ret*100:+.2f}% 年{m['annualized_return']*100:+.1f}% 回{m['max_drawdown']*100:+.1f}% 胜{m['win_rate']*100:.0f}% {len(result['trades'])}笔 | {s_label}", flush=True)

# 保存
with open('output/optimize_formula.json', 'w', encoding='utf-8') as f:
    json.dump(results_1d, f, ensure_ascii=False, indent=2)
print(f"\n总耗时: {time.time()-t0:.0f}s")
