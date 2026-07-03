"""BARSLAST版 ATAN金叉策略 — 多维度优化搜索

优化维度：
  1. 股票池：全量 / HS300 / ZZ500 / ZZ1000 / ZZ A500 / 创业板 / 各类组合
  2. 公式过滤：均线趋势 / 成交量放大 / 排除ST
  3. 止盈止损：成本止损阈值 / 移动止盈参数 / 阶梯止盈档位

目标：2022-2025 全量回测，年化收益 ≥ 20%
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import numpy as np
import itertools
import json
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine
from backtest.stop_config import load_stop_config

# ═══════════════════════════════════════════════════
# 0. 配置
# ═══════════════════════════════════════════════════
START = '20220101'
END   = '20251231'

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

# Stop config candidates for grid search
STOP_GRID = [
    # (name, cost_stop_threshold, trailing_act, trailing_dd, ladder_levels)
    # ladder_levels: list of (profit, sell_ratio)
    ("默认保守 -12%/8+5/6:30:15:30", -0.12, 0.08, 0.05, [(0.06, 0.3), (0.15, 0.3)]),
    ("激进止损 -10%/10+6", -0.10, 0.10, 0.06, [(0.08, 0.3), (0.20, 0.3)]),
    ("宽止损 -15%/8+5", -0.15, 0.08, 0.05, [(0.06, 0.3), (0.15, 0.3)]),
    ("紧移动 -12%/5+3", -0.12, 0.05, 0.03, [(0.04, 0.3), (0.10, 0.3)]),
    ("多档阶梯 -12%/8+5", -0.12, 0.08, 0.05, [(0.04, 0.2), (0.08, 0.2), (0.15, 0.2), (0.25, 0.2)]),
    ("大波段 -12%/12+8", -0.12, 0.12, 0.08, [(0.10, 0.3), (0.25, 0.3)]),
    ("单档快进快出 -8%/5+3", -0.08, 0.05, 0.03, [(0.05, 0.5), (0.12, 0.5)]),
    ("超宽止损 -18%/8+5", -0.18, 0.08, 0.05, [(0.06, 0.3), (0.15, 0.3)]),
    ("中位平衡 -10%/8+4", -0.10, 0.08, 0.04, [(0.05, 0.25), (0.12, 0.25), (0.20, 0.25)]),
    ("高激活 -12%/15+8", -0.12, 0.15, 0.08, [(0.08, 0.3), (0.20, 0.3)]),
]

TdxConnector.ensure_connected()

# ═══════════════════════════════════════════════════
# 1. 获取全量数据
# ═══════════════════════════════════════════════════
print("=" * 100)
print("  BARSLAST版 ATAN金叉策略 — 多维度优化")
print("=" * 100)
print(f"  回测区间: {START} ~ {END}")

print("\n[1/5] 加载数据...", flush=True)
all_codes = DataFetcher.get_stock_universe('50')
k = DataFetcher.get_kline(all_codes, '20200101', END, dividend_type="front", period="1d")
close = k["Close"].sort_index()
high = k["High"].sort_index()
low = k["Low"].sort_index()
volume = k.get("Volume", pd.DataFrame()).sort_index()

valid = close.notna().sum() > 200
close = close.loc[:, valid]
high = high.reindex(columns=close.columns)
low = low.reindex(columns=close.columns)
if not volume.empty:
    volume = volume.reindex(columns=close.columns)

all_cols = close.columns.tolist()
print(f"  全量: {close.shape[0]} 日 × {len(all_cols)} 只股票")

# ═══════════════════════════════════════════════════
# 2. 计算 BARSLAST 版 XG 信号
# ═══════════════════════════════════════════════════
print("\n[2/5] 计算 BARSLAST 版 XG 信号...", flush=True)
ma5 = close.rolling(5).mean()
ma20 = close.rolling(20).mean()
x1 = np.arctan((ma5 / ma5.shift(1) - 1) * 100) * 180 / np.pi
x2 = x1.rolling(5).mean()

jc = (x1 > x2) & (x1.shift(1) <= x2.shift(1))
sc = (x1 < x2) & (x1.shift(1) >= x2.shift(1))

# Pre-compute: for each stock, for each bar, find last_sc position
print("  预计算 BARSLAST 信号...", flush=True)
# Efficient vectorized approach
sig_bs = pd.DataFrame(False, index=close.index, columns=close.columns)

for ci, col in enumerate(close.columns):
    if ci % 500 == 0:
        print(f"    {ci}/{len(close.columns)}...", flush=True)
    xi1 = x1[col].values
    xi2 = x2[col].values
    jc_arr = jc[col].values
    sc_arr = sc[col].values
    n = len(xi1)

    last_sc = -1
    for i in range(n):
        if sc_arr[i]:
            last_sc = i
        if jc_arr[i] and last_sc >= 0:
            if xi2[i] < xi2[last_sc] and xi1[i] > xi1[last_sc]:
                sig_bs.iloc[i, sig_bs.columns.get_loc(col)] = True

# REF(XG,1)=1 → ZP signal
sig = sig_bs.shift(1).fillna(False).astype(bool)
sig = sig.loc[START:END]
print(f"  BARSLAST 信号总数: {sig.sum().sum():,}")

# ═══════════════════════════════════════════════════
# 3. 定义股票池子集
# ═══════════════════════════════════════════════════
print("\n[3/5] 定义股票池子集...", flush=True)

# 按代码前缀分类
def classify(code):
    if code.startswith('688'): return '科创板'
    if code.startswith('300') or code.startswith('301'): return '创业板'
    if code.startswith('60'): return '上证主板'
    if code.startswith('00') or code.startswith('001'): return '深证主板'
    if code.startswith('002') or code.startswith('003'): return '中小板'
    return '其他'

def exclude_st(codes_list):
    return [c for c in codes_list if 'ST' not in c and '*ST' not in c and 'st' not in c]

def exclude_kechuang(codes_list):
    return [c for c in codes_list if not c.startswith('688')]

def exclude_chuangyeban(codes_list):
    return [c for c in codes_list if not c.startswith('300') and not c.startswith('301')]

# 不同股票池定义
universes = {}

# 按板块
universes['上证主板'] = exclude_st([c for c in all_cols if classify(c) == '上证主板'])
universes['深证主板'] = exclude_st([c for c in all_cols if classify(c) == '深证主板'])
universes['中小板'] = exclude_st([c for c in all_cols if classify(c) == '中小板'])
universes['创业板'] = exclude_st([c for c in all_cols if classify(c) == '创业板'])
universes['科创板'] = exclude_st([c for c in all_cols if classify(c) == '科创板'])

# 主板组合（10%涨跌停）
universes['主板(沪+深)'] = exclude_st(universes['上证主板'] + universes['深证主板'])
universes['主板+中小板'] = exclude_st(universes['上证主板'] + universes['深证主板'] + universes['中小板'])
universes['全量(无ST)'] = exclude_st(all_cols)
universes['全量(无ST无科创)'] = exclude_kechuang(exclude_st(all_cols))
universes['全量(无ST无科创无创业)'] = exclude_chuangyeban(exclude_kechuang(exclude_st(all_cols)))

# 按信号活跃度精选（TOP信号数）
signal_counts = sig.sum()
for top_n in [100, 200, 300, 500]:
    top_codes = signal_counts.nlargest(top_n).index.tolist()
    top_clean = exclude_st(top_codes)
    universes[f'信号活跃TOP{top_n}(无ST)'] = top_clean

for name, codes in universes.items():
    print(f"  {name}: {len(codes)} 只")

# ═══════════════════════════════════════════════════
# 4. 公式过滤变体
# ═══════════════════════════════════════════════════
print("\n[4/5] 添加公式过滤变体...", flush=True)

# 计算均线趋势过滤器（预先计算）
ma20_slope = ma20.diff(5)  # 20日均线5日变化方向

# 成交量放大检测
if not volume.empty:
    vol_ma20 = volume.rolling(20).mean()
    vol_ratio = volume / vol_ma20

# 定义公式过滤条件（在信号基础上叠加）
formula_filters = {
    '无额外过滤': None,
    'MA20向上(5日)': lambda sig: sig & (ma20_slope > 0),
    '放量1.2x': lambda sig: sig & (vol_ratio > 1.2) if not volume.empty else sig,
    '放量1.5x': lambda sig: sig & (vol_ratio > 1.5) if not volume.empty else sig,
    'MA20向上+放量1.2x': lambda sig: sig & (ma20_slope > 0) & (vol_ratio > 1.2) if not volume.empty else sig,
    'MA20向上+放量1.5x': lambda sig: sig & (ma20_slope > 0) & (vol_ratio > 1.5) if not volume.empty else sig,
}

# ═══════════════════════════════════════════════════
# 5. 网格搜索
# ═══════════════════════════════════════════════════
print("\n[5/5] 网格搜索最优配置...\n", flush=True)

def build_selections(sig_df, code_list):
    """构建selections DataFrame"""
    records = []
    for col in code_list:
        if col not in sig_df.columns:
            continue
        for idx in sig_df.index[sig_df[col]]:
            records.append({'stock_code': col, 'select_date': idx.strftime('%Y-%m-%d')})
    return pd.DataFrame(records)

def run_single(close_df, high_df, low_df, sig_df, codes, stop_cfg_name, stop_params):
    """单次回测"""
    sel = build_selections(sig_df, codes)
    if sel.empty or len(sel) < 100:
        return None

    common = sorted(set(close_df.columns) & set(sel['stock_code'].unique()))
    if len(common) < 10:
        return None

    cs = close_df[common].ffill().bfill()
    hs = high_df.reindex(index=cs.index, columns=common).ffill().bfill()
    ls = low_df.reindex(index=cs.index, columns=common).ffill().bfill()

    entries = pd.DataFrame(False, index=cs.index, columns=cs.columns)
    for _, row in sel.iterrows():
        code = row['stock_code']; dt = pd.to_datetime(row['select_date'])
        if code not in entries.columns: continue
        if dt in entries.index:
            entries.loc[dt, code] = True
        else:
            m = entries.index >= dt
            if m.any(): entries.loc[entries.index[m][0], code] = True

    cost_thr, trail_act, trail_dd, ladder_levels = stop_params

    STOP_CONFIG = {
        'cost_stop':      {'enabled': True,  'threshold': cost_thr},
        'trailing_stop':  {'enabled': True,  'activation': trail_act, 'drawdown': trail_dd},
        'ladder_tp':      {'enabled': True,  'levels': [{'profit': p, 'sell_ratio': r} for p, r in ladder_levels]},
        'time_stop':      {'enabled': True,  'max_hold_days': 20},
        'cond_time_stop': {'enabled': True,  'days': 7, 'profit': 0.02},
    }

    ladder_profits = np.array([lv[0] for lv in ladder_levels], dtype=np.float64)
    ladder_ratios = np.array([lv[1] for lv in ladder_levels], dtype=np.float64)
    n_ladder = len(ladder_levels)

    engine = BacktestEngine(ENGINE_CFG)
    try:
        result = engine.run_cached(cs, entries, hs.values.astype(np.float64),
                                   ls.values.astype(np.float64), STOP_CONFIG, sel,
                                   ladder_profits, ladder_ratios, n_ladder, skip_sm=True)
    except Exception as e:
        return None

    return {
        'stop_name': stop_cfg_name,
        'cumret': result['cumulative_return'],
        'annret': result['metrics'].get('annualized_return', 0),
        'maxdd': result['metrics'].get('max_drawdown', 0),
        'sharpe': result['metrics'].get('sharpe_ratio', 0),
        'winrate': result['metrics'].get('win_rate', 0),
        'trades': len(result['trades']),
        'signals': len(sel),
    }

# 收集所有候选组合
results = []

# 分层搜索策略：
# 第一层：固定stop配置(默认保守)，测试所有 股票池×公式过滤 组合
# 第二层：取最优股票池，测试所有stop配置
# 第三层：取最优stock×filter×stop，微调

print("─" * 100)
print("  阶段A: 股票池 × 公式过滤 (固定止损: 默认保守)")
print("─" * 100)

default_stop = STOP_GRID[0]  # 默认保守

# 选几个最有希望的池子先测
candidate_pools = [
    '全量(无ST)',
    '全量(无ST无科创)',
    '全量(无ST无科创无创业)',
    '主板(沪+深)',
    '主板+中小板',
    '信号活跃TOP200(无ST)',
    '信号活跃TOP300(无ST)',
    '信号活跃TOP500(无ST)',
]

candidate_filters = ['无额外过滤', 'MA20向上(5日)', '放量1.2x', 'MA20向上+放量1.2x']

for pool_name in candidate_pools:
    if pool_name not in universes:
        continue
    pool_codes = [c for c in universes[pool_name] if c in sig.columns]
    if len(pool_codes) < 50:
        continue

    for filt_name in candidate_filters:
        filt_fn = formula_filters.get(filt_name)
        if filt_fn:
            filtered_sig = filt_fn(sig)
        else:
            filtered_sig = sig

        res = run_single(close, high, low, filtered_sig, pool_codes,
                        default_stop[0], default_stop[1:])
        if res is None:
            continue
        res['pool'] = pool_name
        res['filter'] = filt_name
        results.append(res)
        print(f"  [{res['pool']:<22s}] [{res['filter']:<16s}] "
              f"收益:{res['cumret']*100:+.2f}% 年化:{res['annret']*100:+.2f}% "
              f"回撤:{res['maxdd']*100:.2f}% 夏普:{res['sharpe']:.2f} "
              f"交易:{res['trades']} 信号:{res['signals']}")

# 找最优股票池×过滤组合
best_combo = max(results, key=lambda r: r['annret'])
print(f"\n  → 最优组合: [{best_combo['pool']}] × [{best_combo['filter']}] "
      f"年化:{best_combo['annret']*100:+.2f}%")

# 阶段B：在最优池上扫stop配置
print("\n" + "─" * 100)
print(f"  阶段B: 在 [{best_combo['pool']}] × [{best_combo['filter']}] 上搜索止盈止损")
print("─" * 100)

best_pool_codes = [c for c in universes[best_combo['pool']] if c in sig.columns]
best_filt_fn = formula_filters.get(best_combo['filter'])
if best_filt_fn:
    best_sig = best_filt_fn(sig)
else:
    best_sig = sig

for stop_cfg in STOP_GRID:
    res = run_single(close, high, low, best_sig, best_pool_codes,
                    stop_cfg[0], stop_cfg[1:])
    if res is None:
        continue
    res['pool'] = best_combo['pool']
    res['filter'] = best_combo['filter']
    results.append(res)
    print(f"  [{stop_cfg[0]:<30s}] "
          f"收益:{res['cumret']*100:+.2f}% 年化:{res['annret']*100:+.2f}% "
          f"回撤:{res['maxdd']*100:.2f}% 夏普:{res['sharpe']:.2f} "
          f"交易:{res['trades']}")

# 找全局最优
global_best = max(results, key=lambda r: r['annret'])
print(f"\n  → 全局最优: [{global_best['pool']}] × [{global_best['filter']}] × [{global_best['stop_name']}] "
      f"年化:{global_best['annret']*100:+.2f}%")

# ═══════════════════════════════════════════════════
# 6. 输出完整排名
# ═══════════════════════════════════════════════════
results.sort(key=lambda r: r['annret'], reverse=True)

print("\n" + "=" * 100)
print("  优化排名 TOP20（按年化收益）")
print("=" * 100)
print(f"  {'排名':<5} {'股票池':<24} {'过滤':<18} {'止损配置':<30} {'收益%':>8} {'年化%':>8} {'回撤%':>8} {'夏普':>6} {'交易':>6}")
print("  " + "─" * 100)

for i, r in enumerate(results[:20], 1):
    print(f"  {i:<5} {r['pool']:<24} {r['filter']:<18} {r['stop_name']:<30} "
          f"{r['cumret']*100:>+8.2f} {r['annret']*100:>+8.2f} {r['maxdd']*100:>+8.2f} "
          f"{r['sharpe']:>6.2f} {r['trades']:>6}")

# 保存完整结果
output = {
    'best': {
        'pool': global_best['pool'],
        'filter': global_best['filter'],
        'stop': global_best['stop_name'],
        'cumret': global_best['cumret'],
        'annret': global_best['annret'],
        'maxdd': global_best['maxdd'],
        'sharpe': global_best['sharpe'],
        'winrate': global_best['winrate'],
        'trades': global_best['trades'],
    },
    'top20': [{
        'rank': i, 'pool': r['pool'], 'filter': r['filter'], 'stop': r['stop_name'],
        'cumret': r['cumret'], 'annret': r['annret'], 'maxdd': r['maxdd'],
        'sharpe': r['sharpe'], 'trades': r['trades'],
    } for i, r in enumerate(results[:20], 1)],
    'all_results': results,
}

with open('output/optimize_atan_bs.json', 'w', encoding='utf-8') as f:
    json.dump(output, f, ensure_ascii=False, indent=2, default=str)

print(f"\n  结果已保存: output/optimize_atan_bs.json")
print("=" * 100)
