"""ATAN金叉策略优化 V2 — 强化公式过滤 + 深度搜索

改进方向：
1. 公式层面：X_1低位金叉、金叉强度、中长期趋势确认
2. 股票池：增加板块/市值角度
3. 止损参数微调 + 时间止损可变
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import numpy as np
import json
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine
from utils.logger import get_logger
logger = get_logger(__name__)

START, END = '20220101', '20251231'
TdxConnector.ensure_connected()

ENGINE_CFG = {
    'initial_capital': 1000000.0, 'commission': 0.0003, 'slippage': 0.001,
    'period': '1d',
    'position_sizing': {'min_buy_amount': 2000.0, 'max_buy_amount': 20000.0, 'lot_size': 100, 'min_lots': 1},
}

# ═══════════════════════════════════
# 1. 加载数据
# ═══════════════════════════════════
print("=" * 100)
print("  ATAN金叉优化 V2 — 强化过滤 + 深度搜索")
print("=" * 100)

codes = DataFetcher.get_stock_universe('50')
k = DataFetcher.get_kline(codes, '20200101', END, dividend_type="front", period="1d")
close = k["Close"].sort_index()
high = k["High"].sort_index()
low = k["Low"].sort_index()
volume = k.get("Volume", pd.DataFrame()).sort_index()
valid = close.notna().sum() > 200
for d in [close, high, low]:
    d = d.loc[:, valid] if d is not None else None

close = close.loc[:, valid]
high = high.reindex(columns=close.columns)
low = low.reindex(columns=close.columns)
volume = volume.reindex(columns=close.columns)

print(f"  数据: {close.shape}")
all_cols = close.columns.tolist()

# ═══════════════════════════════════
# 2. 计算基础指标
# ═══════════════════════════════════
print("计算指标...", flush=True)
ma5 = close.rolling(5).mean()
ma10 = close.rolling(10).mean()
ma20 = close.rolling(20).mean()
ma60 = close.rolling(60).mean()
ma120 = close.rolling(120).mean()

x1 = np.arctan((ma5 / ma5.shift(1) - 1) * 100) * 180 / np.pi
x2 = x1.rolling(5).mean()
x1_ma20 = x1.rolling(20).mean()

jc = (x1 > x2) & (x1.shift(1) <= x2.shift(1))
sc = (x1 < x2) & (x1.shift(1) >= x2.shift(1))

if not volume.empty:
    vol_ma5 = volume.rolling(5).mean()
    vol_ma20 = volume.rolling(20).mean()
    vol_ratio = volume / vol_ma20

# 计算趋势指标
adx_long = (close > ma60) & (ma60 > ma60.shift(20))
adx_strong = (close > ma120) & (ma120 > ma120.shift(60))

# ═══════════════════════════════════
# 3. 生成多个版本的XG信号（加不同过滤）
# ═══════════════════════════════════
print("生成多版本XG信号...", flush=True)

def gen_barslast_sig(jc, sc, x1, x2, extra_cond=None):
    """BARSLAST版 XG，可选叠加额外条件"""
    sig = pd.DataFrame(False, index=x1.index, columns=x1.columns)
    for ci, col in enumerate(x1.columns):
        xi1 = x1[col].values; xi2 = x2[col].values
        jc_arr = jc[col].values; sc_arr = sc[col].values
        n = len(xi1)

        extra = np.ones(n, dtype=bool)
        if extra_cond is not None:
            ec = extra_cond[col].values
            extra = ec if isinstance(ec, np.ndarray) else np.ones(n, dtype=bool)

        last_sc = -1
        for i in range(n):
            if sc_arr[i]: last_sc = i
            if jc_arr[i] and last_sc >= 0 and extra[i]:
                if xi2[i] < xi2[last_sc] and xi1[i] > xi1[last_sc]:
                    sig.iloc[i, sig.columns.get_loc(col)] = True
    return sig

# 多个信号版本（预计算，节约时间）
print("  v0: BARSLAST基础..."); s0 = gen_barslast_sig(jc, sc, x1, x2)
print("  v1: +低位金叉(X_1<10)..."); lo_filter = x1 < 10; s1 = gen_barslast_sig(jc, sc, x1, x2, lo_filter)
print("  v2: +低位金叉(X_1<5)..."); s2 = gen_barslast_sig(jc, sc, x1, x2, x1 < 5)
print("  v3: +中长期趋势(>MA60)..."); s3 = gen_barslast_sig(jc, sc, x1, x2, close > ma60)
print("  v4: +强趋势(>MA120)..."); s4 = gen_barslast_sig(jc, sc, x1, x2, close > ma120)
print("  v5: +趋势(>MA60)+低位..."); s5 = gen_barslast_sig(jc, sc, x1, x2, (close > ma60) & (x1 < 10))
print("  v6: +放量1.3x..."); s6 = gen_barslast_sig(jc, sc, x1, x2, vol_ratio > 1.3 if not volume.empty else None)
print("  v7: +趋势+放量+低位..."); s7 = gen_barslast_sig(jc, sc, x1, x2, (close > ma60) & (vol_ratio > 1.2 if not volume.empty else True) & (x1 < 10))

sig_versions = [
    ("BARSLAST基础", s0),
    ("+低位金叉X1<10", s1),
    ("+低位金叉X1<5", s2),
    ("+MA60趋势", s3),
    ("+MA120强趋势", s4),
    ("+MA60趋势+低位", s5),
    ("+放量1.3x", s6),
    ("+趋势+放量+低位", s7),
]

for name, sv in sig_versions:
    shifted = sv.shift(1).fillna(False).astype(bool).loc[START:END]
    print(f"    [{name}] 信号: {shifted.sum().sum():,}")

# ══════════════════════════════════════
# 4. 股票池定义
# ══════════════════════════════════════
print("\n定义股票池...")
def exclude_st(cl): return [c for c in cl if 'ST' not in c and '*ST' not in c]

universes = {
    '全量(无ST)': exclude_st(all_cols),
    '主板+中小板(无ST)': exclude_st([c for c in all_cols if c.startswith(('60','000','001','002','003'))]),
    '上证主板(无ST)': exclude_st([c for c in all_cols if c.startswith(('60'))]),
    '深市(无ST)': exclude_st([c for c in all_cols if c.startswith(('00','002','003'))]),
    '中小板(无ST)': exclude_st([c for c in all_cols if c.startswith(('002','003'))]),
    '创业板(无ST)': exclude_st([c for c in all_cols if c.startswith(('300','301'))]),
}

# 信号活跃度TOP（用基础BARSLAST版本统计）
sig_counts = s0.shift(1).fillna(False).astype(bool).loc[START:END].sum()
for n in [100, 200, 500, 1000]:
    top = sig_counts.nlargest(n).index.tolist()
    universes[f'信号TOP{n}'] = exclude_st(top)

for n, c in universes.items():
    print(f"  {n}: {len(c)}")

# ═══════════════════════════════════
# 5. 止盈止损网格（扩展版）
# ═══════════════════════════════════
STOP_GRID_V2 = [
    ("S1:-12/8+5/6:30:15:30/20d", -0.12, 0.08, 0.05, [(0.06,0.3),(0.15,0.3)], 20),
    ("S2:-10/10+6/8:30:20:30", -0.10, 0.10, 0.06, [(0.08,0.3),(0.20,0.3)], 20),
    ("S3:-8/8+5/5:30:12:30", -0.08, 0.08, 0.05, [(0.05,0.3),(0.12,0.3)], 15),
    ("S4:-15/10+6/8:30:20:30", -0.15, 0.10, 0.06, [(0.08,0.3),(0.20,0.3)], 25),
    ("S5:-12/5+3/4:30:10:30", -0.12, 0.05, 0.03, [(0.04,0.3),(0.10,0.3)], 15),
    ("S6:-12/12+8/10:30:25:30", -0.12, 0.12, 0.08, [(0.10,0.3),(0.25,0.3)], 30),
    ("S7:-10/8+4/5:25:12:25:20:25", -0.10, 0.08, 0.04, [(0.05,0.25),(0.12,0.25),(0.20,0.25)], 20),
    ("S8:-12/10+5/5:30:12:30/15d", -0.12, 0.10, 0.05, [(0.05,0.3),(0.12,0.3)], 15),
    ("S9:-18/10+6/10:30:25:30", -0.18, 0.10, 0.06, [(0.10,0.3),(0.25,0.3)], 25),
    ("S10:-7/6+4/3:25:8:25", -0.07, 0.06, 0.04, [(0.03,0.25),(0.08,0.25)], 15),
]

# ═══════════════════════════════════
# 6. 批量回测
# ═══════════════════════════════════
print("\n开始批量回测...\n", flush=True)

def build_sel(sig_df, code_list):
    records = []
    for col in code_list:
        if col not in sig_df.columns: continue
        for idx in sig_df.index[sig_df[col]]:
            records.append({'stock_code': col, 'select_date': idx.strftime('%Y-%m-%d')})
    return pd.DataFrame(records)

def run_single(close_df, high_df, low_df, sig_df, codes, stop_params):
    sel = build_sel(sig_df, codes)
    if sel.empty or len(sel) < 100: return None
    common = sorted(set(close_df.columns) & set(sel['stock_code'].unique()))
    if len(common) < 10: return None
    cs = close_df[common].ffill().bfill()
    hs = high_df.reindex(index=cs.index, columns=common).ffill().bfill()
    ls = low_df.reindex(index=cs.index, columns=common).ffill().bfill()

    entries = pd.DataFrame(False, index=cs.index, columns=cs.columns)
    for _, row in sel.iterrows():
        code = row['stock_code']; dt = pd.to_datetime(row['select_date'])
        if code not in entries.columns: continue
        if dt in entries.index: entries.loc[dt, code] = True
        else:
            m = entries.index >= dt
            if m.any(): entries.loc[entries.index[m][0], code] = True

    cost_thr, trail_act, trail_dd, ladder_lv, max_hold = stop_params
    STOP = {
        'cost_stop':      {'enabled': True, 'threshold': cost_thr},
        'trailing_stop':  {'enabled': True, 'activation': trail_act, 'drawdown': trail_dd},
        'ladder_tp':      {'enabled': True, 'levels': [{'profit': p, 'sell_ratio': r} for p, r in ladder_lv]},
        'time_stop':      {'enabled': True, 'max_hold_days': max_hold},
        'cond_time_stop': {'enabled': True, 'days': min(7, max_hold-1), 'profit': 0.02},
    }
    lp = np.array([lv[0] for lv in ladder_lv], dtype=np.float64)
    lr = np.array([lv[1] for lv in ladder_lv], dtype=np.float64)

    engine = BacktestEngine(ENGINE_CFG)
    try:
        result = engine.run_cached(cs, entries, hs.values.astype(np.float64),
                                   ls.values.astype(np.float64), STOP, sel, lp, lr, len(ladder_lv), skip_sm=True)
    except Exception as e: logger.warning("run_cached failed: %s", e); return None
    return {
        'cumret': result['cumulative_return'], 'annret': result['metrics'].get('annualized_return',0),
        'maxdd': result['metrics'].get('max_drawdown',0), 'sharpe': result['metrics'].get('sharpe_ratio',0),
        'winrate': result['metrics'].get('win_rate',0), 'trades': len(result['trades']),
    }

results = []
# 重点搜索：只搜最有希望的
target_sigs = ['BARSLAST基础', '+MA60趋势', '+MA60趋势+低位', '+趋势+放量+低位']
target_pools = ['全量(无ST)', '主板+中小板(无ST)', '上证主板(无ST)', '深市(无ST)', '信号TOP200', '信号TOP500']

# 各组合试一次默认stop，找最好的6个组合
print("─" * 90)
print("  阶段A: 筛选最优 sig×pool 组合 (固定S1)")
print("─" * 90)
combo_results = []
for sname, sig_df in sig_versions:
    if sname not in target_sigs: continue
    sig_shifted = sig_df.shift(1).fillna(False).astype(bool).loc[START:END]
    for pname in target_pools:
        codes_list = [c for c in universes[pname] if c in sig_shifted.columns]
        if len(codes_list) < 30: continue
        r = run_single(close, high, low, sig_shifted, codes_list,
                      (-0.12, 0.08, 0.05, [(0.06,0.3),(0.15,0.3)], 20))
        if r is None: continue
        r['sig'] = sname; r['pool'] = pname; r['stop'] = 'S1'
        combo_results.append(r)
        combo_results.sort(key=lambda x: x['annret'], reverse=True)
        print(f"  [{sname:<16s}] [{pname:<22s}] 收益:{r['cumret']*100:+6.2f}% 年化:{r['annret']*100:+5.2f}% 回撤:{r['maxdd']*100:.2f}% 夏普:{r['sharpe']:.2f} 交易:{r['trades']}")

# 取前4个组合 + 全部stop配置
best_combos = combo_results[:4]
print(f"\n  → 前4组合:")
for bc in best_combos:
    print(f"    [{bc['sig']}] × [{bc['pool']}] 年化:{bc['annret']*100:+.2f}%")

# 阶段B: 对前4组合搜所有stop
print("\n" + "─" * 90)
print(f"  阶段B: 对前4组合搜索 {len(STOP_GRID_V2)} 组止盈止损")
print("─" * 90)

for combo in best_combos:
    sname, pname = combo['sig'], combo['pool']
    # 找到对应的sig
    sv = None
    for sn, sd in sig_versions:
        if sn == sname: sv = sd; break
    if sv is None: continue
    sig_shifted = sv.shift(1).fillna(False).astype(bool).loc[START:END]
    codes_list = [c for c in universes[pname] if c in sig_shifted.columns]

    for sn, *sp in STOP_GRID_V2:
        r = run_single(close, high, low, sig_shifted, codes_list, tuple(sp))
        if r is None: continue
        r['sig'] = sname; r['pool'] = pname; r['stop'] = sn
        results.append(r)
        combo_results.append(r)
        if r['annret'] > 0.04:  # 只打印好的
            print(f"  [{sname:<16s}] [{pname:<22s}] [{sn:<30s}] 收益:{r['cumret']*100:+6.2f}% 年化:{r['annret']*100:+5.2f}% 回撤:{r['maxdd']*100:.2f}% 夏普:{r['sharpe']:.2f}")

# 排合并输出
all_results = combo_results + results
all_results.sort(key=lambda x: x['annret'], reverse=True)
# 去重
seen = set()
unique_results = []
for r in all_results:
    k = (r['sig'], r['pool'], r['stop'])
    if k not in seen:
        seen.add(k)
        unique_results.append(r)

print("\n" + "=" * 100)
print("  TOP 30 综合排名")
print("=" * 100)
print(f"  {'#':<4} {'公式':<18} {'股票池':<24} {'止损配置':<32} {'累计%':>7} {'年化%':>7} {'回撤%':>7} {'夏普':>5} {'交易':>6}")
print("  " + "-" * 100)
for i, r in enumerate(unique_results[:30], 1):
    print(f"  {i:<4} {r['sig']:<18} {r['pool']:<24} {r['stop']:<32} "
          f"{r['cumret']*100:>+7.2f} {r['annret']*100:>+7.2f} {r['maxdd']*100:>+7.2f} "
          f"{r['sharpe']:>5.2f} {r['trades']:>6}")

# 保存
if unique_results:
    best = unique_results[0]
    print(f"\n  全局最优: [{best['sig']}] × [{best['pool']}] × [{best['stop']}] 年化:{best['annret']*100:+.2f}%")

output = {
    'best': {'sig': best['sig'], 'pool': best['pool'], 'stop': best['stop'],
             'cumret': best['cumret'], 'annret': best['annret'], 'maxdd': best['maxdd'],
             'sharpe': best['sharpe'], 'winrate': best['winrate'], 'trades': best['trades']},
    'top30': [dict(r) for r in unique_results[:30]],
    'all': [dict(r) for r in unique_results],
}
with open('output/optimize_atan_v2.json', 'w', encoding='utf-8') as f:
    json.dump(output, f, ensure_ascii=False, indent=2, default=str)
print(f"  已保存: output/optimize_atan_v2.json")
