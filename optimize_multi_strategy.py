"""多策略组合优化 — 技术指标+量价+动量+基本面因子"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine

TdxConnector.ensure_connected()
START, END = '20260101', '20260528'

ENGINE_1D = {
    'initial_capital': 200000.0, 'commission': 0.0003, 'slippage': 0.001, 'period': '1d',
    'position_sizing': {'min_buy_amount': 2000.0, 'max_buy_amount': 10000.0, 'lot_size': 100, 'min_lots': 1},
}

def to_selections(sig_df):
    recs = []
    for c in sig_df.columns:
        for d in sig_df.index[sig_df[c]]:
            recs.append({'stock_code':c,'select_date':d.strftime('%Y-%m-%d')})
    return pd.DataFrame(recs)

# ============================================================
# 策略1: MACD金叉 + KDJ金叉 + RSI超卖反转
# ============================================================
def strategy_macd_kdj_rsi(close, high, low, vol):
    """MACD金叉 + KDJ金叉 + RSI超卖反转"""
    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    macd_cross = (dif > dea) & (dif.shift(1) <= dea.shift(1))  # 金叉

    # KDJ
    low_9 = low.rolling(9, min_periods=9).min()
    high_9 = high.rolling(9, min_periods=9).max()
    rsv = (close - low_9) / (high_9 - low_9 + 1e-10) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    kdj_cross = (k > d) & (k.shift(1) <= d.shift(1)) & (k < 50)  # 低位金叉

    # RSI
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(14, min_periods=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14, min_periods=14).mean()
    rs = gain / (loss + 1e-10)
    rsi = 100 - (100 / (1 + rs))
    rsi_signal = (rsi > 30) & (rsi.shift(1) <= 30)  # 从超卖区突破

    # 组合：MACD金叉 AND (KDJ金叉 OR RSI突破)
    signal = macd_cross & (kdj_cross | rsi_signal)
    return signal.fillna(False)

# ============================================================
# 策略2: 突破策略（海龟交易法则简化版）
# ============================================================
def strategy_breakout(close, high, low, vol):
    """20日突破 + 10日突破确认"""
    high_20 = high.rolling(20, min_periods=20).max()
    high_10 = high.rolling(10, min_periods=10).max()
    # 今日突破20日新高 AND 前一日突破10日新高（双重确认）
    signal = (close > high_20.shift(1)) & (close.shift(1) > high_10.shift(2))
    return signal.fillna(False)

# ============================================================
# 策略3: 动量策略（多周期动量）
# ============================================================
def strategy_momentum(close, high, low, vol):
    """5日、10日、20日动量全部向上 + 成交量放大"""
    mom5 = close / close.shift(5) - 1
    mom10 = close / close.shift(10) - 1
    mom20 = close / close.shift(20) - 1

    # 三个动量全部 > 0
    mom_signal = (mom5 > 0) & (mom10 > 0) & (mom20 > 0)

    # 成交量 > 20日均量 * 1.2
    vol_ma20 = vol.rolling(20, min_periods=20).mean()
    vol_signal = vol > vol_ma20 * 1.2

    signal = mom_signal & vol_signal
    return signal.fillna(False)

# ============================================================
# 策略4: 趋势+波动率突破（布林带下轨反弹）
# ============================================================
def strategy_bollinger(close, high, low, vol):
    """价格触及布林带下轨后反弹 + MA60上升趋势"""
    ma20 = close.rolling(20, min_periods=20).mean()
    std20 = close.rolling(20, min_periods=20).std()
    lower = ma20 - 2 * std20

    # 昨日触及下轨，今日反弹
    touch_lower = close.shift(1) <= lower.shift(1)
    bounce = close > close.shift(1)

    # MA60趋势向上
    ma60 = close.rolling(60, min_periods=60).mean()
    trend_up = ma60 > ma60.shift(5)

    signal = touch_lower & bounce & trend_up
    return signal.fillna(False)

# ============================================================
# 策略5: 均线多头排列 + 放量突破
# ============================================================
def strategy_ma_alignment(close, high, low, vol):
    """MA5 > MA10 > MA20 > MA60 多头排列 + 放量突破MA20"""
    ma5 = close.rolling(5, min_periods=5).mean()
    ma10 = close.rolling(10, min_periods=10).mean()
    ma20 = close.rolling(20, min_periods=20).mean()
    ma60 = close.rolling(60, min_periods=60).mean()

    # 多头排列
    alignment = (ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60)

    # 突破MA20
    breakout = (close > ma20) & (close.shift(1) <= ma20.shift(1))

    # 放量
    vol_ma20 = vol.rolling(20, min_periods=20).mean()
    vol_surge = vol > vol_ma20 * 1.5

    signal = alignment & breakout & vol_surge
    return signal.fillna(False)

# ============================================================
# 策略6: 小市值+动量组合
# ============================================================
def strategy_small_cap_momentum(close, high, low, vol, market_cap=None):
    """小市值 + 近期强势动量"""
    # 20日涨幅 > 10%
    ret20 = close / close.shift(20) - 1
    momentum = ret20 > 0.10

    # 成交量持续活跃（10日均量 > 20日均量）
    vol_ma10 = vol.rolling(10, min_periods=10).mean()
    vol_ma20 = vol.rolling(20, min_periods=20).mean()
    vol_active = vol_ma10 > vol_ma20

    # 如果有市值数据，筛选小市值（这里简化为价格<30作为代理）
    if market_cap is not None:
        small_cap = market_cap < market_cap.quantile(0.3)
    else:
        small_cap = close < 30  # 简化：低价股作为小市值代理

    signal = momentum & vol_active & small_cap
    return signal.fillna(False)

# ============================================================
# 数据准备
# ============================================================
print("Step 1: 获取1d OHLCV...", flush=True)
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
vol = vol.reindex(columns=close.columns) if not vol.empty else close * 0

print(f"1d: {close.shape} | {time.time()-t0:.0f}s", flush=True)

# ============================================================
# 生成策略信号
# ============================================================
STRATEGIES = [
    ("MACD+KDJ+RSI", strategy_macd_kdj_rsi),
    ("Breakout20+10", strategy_breakout),
    ("Momentum5-10-20", strategy_momentum),
    ("BollingerBounce", strategy_bollinger),
    ("MA-Alignment", strategy_ma_alignment),
    ("SmallCap+Mom", strategy_small_cap_momentum),
]

print(f"\nStep 2: 生成{len(STRATEGIES)}个策略信号...", flush=True)
all_signals = {}
for label, func in STRATEGIES:
    sig = func(close, high, low, vol)
    sel = to_selections(sig)
    n = len(sel)
    nc = len(sel['stock_code'].unique()) if n > 0 else 0
    all_signals[label] = {'sig': sig, 'sel': sel, 'n': n, 'nc': nc}
    print(f"  {label}: {n}信号/{nc}只", flush=True)

# ============================================================
# 策略组合（信号取并集）
# ============================================================
print("\nStep 3: 测试策略组合...", flush=True)
from itertools import combinations

COMBOS = []
# 单策略
for label in all_signals.keys():
    COMBOS.append(([label], f"Single:{label}"))

# 双策略组合
for c in combinations(all_signals.keys(), 2):
    COMBOS.append((list(c), f"Combo2:{'+'.join([s[:5] for s in c])}"))

# 三策略组合（选几个有代表性的）
top3_combos = [
    (["MACD+KDJ+RSI", "Momentum5-10-20", "MA-Alignment"], "Tech3"),
    (["Breakout20+10", "BollingerBounce", "SmallCap+Mom"], "Mixed3"),
    (["MACD+KDJ+RSI", "MA-Alignment", "SmallCap+Mom"], "Best3"),
]
for lst, label in top3_combos:
    COMBOS.append((lst, f"Combo3:{label}"))

# ============================================================
# 止损网格
# ============================================================
STOP_GRID = [
    ("6%→100/C-12/T20", [{'p':0.06,'r':1.0}], -0.12, 0.05,0.03, 20),
    ("4%→100/C-12/T20", [{'p':0.04,'r':1.0}], -0.12, 0.05,0.03, 20),
    ("3%→30,6%→100/C-12", [{'p':0.03,'r':0.30},{'p':0.06,'r':1.0}], -0.12, 0.08,0.04, 20),
    ("5%→100/C-12/T20", [{'p':0.05,'r':1.0}], -0.12, 0.05,0.03, 20),
]

# ============================================================
# 回测
# ============================================================
engine = BacktestEngine(ENGINE_1D)
results = []
count = 0
best = -999
total = len(COMBOS) * len(STOP_GRID)

print(f"回测 {total} 组合...\n", flush=True)

for strategy_list, combo_label in COMBOS:
    # 合并信号（取并集）
    combined_sig = pd.DataFrame(False, index=close.index, columns=close.columns)
    for s_name in strategy_list:
        combined_sig = combined_sig | all_signals[s_name]['sig']

    sel = to_selections(combined_sig)
    if len(sel) == 0: continue

    codes_f = sorted(sel['stock_code'].unique())
    n_sig = len(sel)

    # 限制股票数量
    if len(codes_f) > 800:
        cnt = sel['stock_code'].value_counts()
        codes_f = sorted(cnt.head(800).index)
        sel = sel[sel['stock_code'].isin(codes_f)]

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

        result = engine.run_cached(c, e, hn, ln, sc, sel, lp, lr, len(lv), skip_sm=True)
        ret = result['cumulative_return']
        m = result['metrics']

        results.append({'cum_ret': ret, 'annual': m['annualized_return'], 'max_dd': m['max_drawdown'],
                       'win_rate': m['win_rate'], 'trades': len(result['trades']), 'signals': n_sig,
                       'strategy': combo_label, 'stop': s_label, 'n_stocks': len(codes_f)})

        if ret > best:
            best = ret
            print(f"*** BEST [{count}/{total}]: {ret*100:+.2f}% 年{m['annualized_return']*100:+.1f}% | {combo_label} | {s_label}", flush=True)

        if count % 20 == 0:
            print(f"[{count}/{total}] best={best*100:+.2f}%", flush=True)

results.sort(key=lambda x: x['cum_ret'], reverse=True)

print(f"\n{'='*100}")
print(f"TOP 20 (共{len(results)}组合, 总耗时{time.time()-t0:.0f}s)")
print(f"{'='*100}")
for i, r in enumerate(results[:20]):
    print(f"{i+1:2d}. {r['cum_ret']*100:+.2f}% 年{r['annual']*100:+.1f}% 回{r['max_dd']*100:+.1f}% 胜{r['win_rate']*100:.0f}% {r['trades']}笔 | {r['strategy']:30s} | {r['stop']}")

with open('output/optimize_multi_strategy.json', 'w', encoding='utf-8') as f:
    # 转换 numpy 类型
    for r in results:
        for k in ['cum_ret', 'annual', 'max_dd', 'win_rate']:
            if isinstance(r[k], (np.integer, np.floating)):
                r[k] = float(r[k])
        r['trades'] = int(r['trades'])
        r['signals'] = int(r['signals'])
        r['n_stocks'] = int(r['n_stocks'])
    json.dump(results, f, ensure_ascii=False, indent=2)

print(f"\n结果: output/optimize_multi_strategy.json")
print(f"总耗时: {time.time()-t0:.0f}s")
