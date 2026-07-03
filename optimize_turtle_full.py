"""海龟突破策略全维度优化 — 力争年化20%+

优化维度:
  1. 信号强化: 放量确认、趋势对齐、突破强度
  2. 股票池: 排除20%涨跌停、ST、微盘
  3. 止盈止损: 加宽移动止盈吃大波段
  4. 仓位: 固定 vs 信号质量加权

Engine v3.2 | 2022-01 ~ 2025-12 | 全量回测
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

# ══════════════════════════════════════
# 1. 加载数据
# ══════════════════════════════════════
print("=" * 110)
print("  海龟突破策略 — 全维度优化搜索")
print(f"  区间: {START} ~ {END}  |  Engine: v3.2-exec-price")
print("=" * 110)

codes = DataFetcher.get_stock_universe('50')
k = DataFetcher.get_kline(codes, '20200101', END, dividend_type="front", period="1d")
close = k["Close"].sort_index()
high = k["High"].sort_index()
low = k["Low"].sort_index()
volume = k.get("Volume", pd.DataFrame()).sort_index()

valid = close.notna().sum() > 200
close = close.loc[:, valid]
high = high.reindex(columns=close.columns)
low = low.reindex(columns=close.columns)
volume = volume.reindex(columns=close.columns)

print(f"  全量: {close.shape[0]}日 × {close.shape[1]}只")
all_codes = close.columns.tolist()

# ══════════════════════════════════════
# 2. 预计算所有指标 & 信号变体
# ══════════════════════════════════════
print("\n[2] 预计算指标 & 多版本信号...", flush=True)

# 基础海龟信号
hi20 = high.rolling(20, min_periods=20).max()
hi10 = high.rolling(10, min_periods=10).max()
base_signal = (close > hi20.shift(1)) & (close.shift(1) > hi10.shift(2))

# 趋势指标
ma20 = close.rolling(20).mean()
ma60 = close.rolling(60).mean()
ma120 = close.rolling(120).mean()
ma20_slope = ma20.diff(10)  # MA20 10日变化方向

# 成交量指标
vol_ma20 = volume.rolling(20).mean()
vol_ratio = volume / vol_ma20.shift(1)  # 相对20日均量

# 突破强度: 突破日收盘超出20日高点的幅度
breakout_strength = (close - hi20.shift(1)) / hi20.shift(1)

# 构建多种信号变体
print("  构建信号变体...", flush=True)

signal_variants = {}

# V0: 基础海龟
signal_variants['S0:基础'] = base_signal

# V1-V4: 放量确认
for th in [1.2, 1.5, 2.0]:
    label = f'S1:放量{th}x'
    signal_variants[label] = base_signal & (vol_ratio > th)

# V5-V8: 趋势对齐
for ma, name in [(ma20, 'MA20'), (ma60, 'MA60'), (ma120, 'MA120')]:
    # 突破日价格在MA上方
    signal_variants[f'S5:>{name}'] = base_signal & (close > ma)
    # 突破日价格在MA上方 + MA向上
    signal_variants[f'S6:>{name}+斜'] = base_signal & (close > ma) & (ma.diff(20) > 0)

# V9-V12: 放量 + 趋势组合
signal_variants['S9:放量1.5x+>MA60'] = base_signal & (vol_ratio > 1.5) & (close > ma60)
signal_variants['S10:放量1.5x+>MA120'] = base_signal & (vol_ratio > 1.5) & (close > ma120)
signal_variants['S11:放量2x+>MA60+斜'] = base_signal & (vol_ratio > 2.0) & (close > ma60) & (ma60.diff(20) > 0)
signal_variants['S12:放量1.2x+>MA20'] = base_signal & (vol_ratio > 1.2) & (close > ma20)

# V13: 突破强度过滤（避免追高）
signal_variants['S13:突破强度<3%'] = base_signal & (breakout_strength < 0.03)
signal_variants['S14:突破强度<5%'] = base_signal & (breakout_strength < 0.05)

# V15-V16: 多重确认
signal_variants['S15:放量1.5x+>MA60+强度<5%'] = base_signal & (vol_ratio > 1.5) & (close > ma60) & (breakout_strength < 0.05)
signal_variants['S16:放量2x+>MA120+斜+强度<5%'] = base_signal & (vol_ratio > 2.0) & (close > ma120) & (ma120.diff(20) > 0) & (breakout_strength < 0.05)

# 统计各变体信号量
for name, sv in signal_variants.items():
    total = sv.sum().sum()
    print(f"    [{name}] 信号: {total:>8,}")

# ══════════════════════════════════════
# 3. 定义股票池
# ══════════════════════════════════════
print("\n[3] 定义股票池...", flush=True)

def classify(c):
    if c.startswith('688'): return '科创板'
    if c.startswith('300') or c.startswith('301'): return '创业板'
    if c.startswith('60'): return '沪主板'
    if c.startswith('00') or c.startswith('001'): return '深主板'
    if c.startswith('002') or c.startswith('003'): return '中小板'
    return '其他'

def exclude_st(cl): return [c for c in cl if 'ST' not in c and '*ST' not in c]

universes = {}
universes['全量(无ST)'] = exclude_st(all_codes)

# 只留10%涨跌停制度（排除科创板+创业板）
main_sme = [c for c in all_codes if classify(c) in ('沪主板','深主板','中小板')]
universes['主板+中小板'] = exclude_st(main_sme)
universes['沪主板'] = exclude_st([c for c in all_codes if classify(c) == '沪主板'])
universes['深主板+中小板'] = exclude_st([c for c in all_codes if classify(c) in ('深主板','中小板')])

# 信号活跃度TOP（基于基础信号）
signal_freq = base_signal.sum()
for n in [200, 500, 1000, 2000]:
    top = signal_freq.nlargest(n).index.tolist()
    universes[f'TOP{n}'] = exclude_st(top)

# 排除微盘（用信号次数做最低活跃度代理——至少被选中过10次）
active_codes = signal_freq[signal_freq >= 10].index.tolist()
universes['活跃≥10次(无ST)'] = exclude_st(active_codes)
active_codes_20 = signal_freq[signal_freq >= 20].index.tolist()
universes['活跃≥20次(无ST)'] = exclude_st(active_codes_20)

for n, c in universes.items():
    print(f"  {n}: {len(c):,}")

# ══════════════════════════════════════
# 4. 止盈止损配置（精选）
# ══════════════════════════════════════
print("\n[4] 止盈止损配置...", flush=True)

STOP_GRID = [
    # (name, cost_thr, trail_act, trail_dd, ladder, max_hold)
    # 核心对比
    ("Z1:-12/8+5/6:30:15:30/20d", -0.12, 0.08, 0.05, [(0.06,0.3),(0.15,0.3)], 20),
    ("Z2:-10/10+6/8:30:20:30/20d", -0.10, 0.10, 0.06, [(0.08,0.3),(0.20,0.3)], 20),
    ("Z3:-12/5+3/4:30:10:30/15d", -0.12, 0.05, 0.03, [(0.04,0.3),(0.10,0.3)], 15),
    # 大波段变体
    ("Z4:-15/12+8/10:30:25:30/30d", -0.15, 0.12, 0.08, [(0.10,0.3),(0.25,0.3)], 30),
    ("Z5:-12/15+10/10:30:25:30/30d", -0.12, 0.15, 0.10, [(0.10,0.3),(0.25,0.3)], 30),
    ("Z6:-8/10+6/6:25:15:25/25d", -0.08, 0.10, 0.06, [(0.06,0.25),(0.15,0.25)], 25),
    # 三档阶梯
    ("Z7:-10/8+4/5:25:12:25:20:25/20d", -0.10, 0.08, 0.04, [(0.05,0.25),(0.12,0.25),(0.20,0.25)], 20),
    ("Z8:-12/10+5/5:25:15:25:25:25/25d", -0.12, 0.10, 0.05, [(0.05,0.25),(0.15,0.25),(0.25,0.25)], 25),
    # 紧止损
    ("Z9:-5/8+5/6:30:15:30/15d", -0.05, 0.08, 0.05, [(0.06,0.3),(0.15,0.3)], 15),
]

# ══════════════════════════════════════
# 5. 批量回测
# ══════════════════════════════════════
print("\n[5] 开始批量回测...\n", flush=True)

def build_sel(sig_df, code_list):
    recs = []
    for col in code_list:
        if col not in sig_df.columns: continue
        for idx in sig_df.index[sig_df[col]]:
            recs.append({'stock_code': col, 'select_date': idx.strftime('%Y-%m-%d')})
    return pd.DataFrame(recs)

def run_one(close_df, high_df, low_df, sig_df, codes_list, sp):
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

    cost_thr, trail_act, trail_dd, ladder_lv, max_hold = sp
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

    m = result['metrics']
    return {
        'cumret': result['cumulative_return'], 'annret': m['annualized_return'],
        'maxdd': m['max_drawdown'], 'sharpe': m['sharpe_ratio'],
        'winrate': m['win_rate'], 'trades': len(result['trades']),
        'signals': len(sel),
    }

results = []

# 分层搜索策略
# 层1: 所有 signal×pool 用 Z1 快速筛选 → 找TOP信号和TOP池
# 层2: TOP组合 × 所有STOP → 深度优化

print("─" * 100)
print("  层1: 信号×股票池 快速筛选 (固定Z1)")
print("─" * 100)

# 选核心信号+核心池
core_signals = ['S0:基础', 'S1:放量1.2x', 'S1:放量1.5x', 'S9:放量1.5x+>MA60',
                'S10:放量1.5x+>MA120', 'S6:>MA60+斜', 'S6:>MA120+斜',
                'S15:放量1.5x+>MA60+强度<5%', 'S5:>MA60', 'S5:>MA120']
core_pools = ['全量(无ST)', '主板+中小板', '沪主板', '深主板+中小板',
              '活跃≥10次(无ST)', '活跃≥20次(无ST)', 'TOP200', 'TOP500']

layer1 = []
for sname, sig_raw in signal_variants.items():
    if sname not in core_signals: continue
    sig = sig_raw.loc[START:END]
    for pname in core_pools:
        cl = [c for c in universes[pname] if c in sig.columns]
        if len(cl) < 30: continue
        r = run_one(close, high, low, sig, cl, (-0.12, 0.08, 0.05, [(0.06,0.3),(0.15,0.3)], 20))
        if r is None: continue
        r['sig'] = sname; r['pool'] = pname; r['stop'] = 'Z1'
        layer1.append(r)
        results.append(r)
        if r['annret'] > 0.04:
            print(f"  [{sname:<28s}] [{pname:<22s}] "
                  f"累计:{r['cumret']*100:+7.2f}% 年化:{r['annret']*100:+6.2f}% "
                  f"回撤:{r['maxdd']*100:.2f}% 夏普:{r['sharpe']:.2f} 交易:{r['trades']:,}")

layer1.sort(key=lambda x: x['annret'], reverse=True)
top_combos = layer1[:6]
print(f"\n  → 层1 TOP6组合:")
for c in top_combos:
    print(f"    [{c['sig']}] × [{c['pool']}] 年化:{c['annret']*100:+.2f}% 累计:{c['cumret']*100:+.2f}% 回撤:{c['maxdd']*100:.2f}%")

# 层2: TOP组合 × 全部STOP
print("\n" + "─" * 100)
print(f"  层2: TOP6组合 × {len(STOP_GRID)}组止盈止损")
print("─" * 100)

for combo in top_combos:
    sname, pname = combo['sig'], combo['pool']
    sv = signal_variants[sname]
    sig = sv.loc[START:END]
    cl = [c for c in universes[pname] if c in sig.columns]

    for sn, *sp in STOP_GRID:
        r = run_one(close, high, low, sig, cl, tuple(sp))
        if r is None: continue
        r['sig'] = sname; r['pool'] = pname; r['stop'] = sn
        results.append(r)
        if r['annret'] > 0.06:
            print(f"  [{sname:<28s}] [{pname:<22s}] [{sn:<32s}] "
                  f"累计:{r['cumret']*100:+7.2f}% 年化:{r['annret']*100:+6.2f}% "
                  f"回撤:{r['maxdd']*100:.2f}% 夏普:{r['sharpe']:.2f} 交易:{r['trades']:,}")

# 层3: 最佳组合 + 仓位加倍 (max_buy 20k→40k)
print("\n" + "─" * 100)
print("  层3: 仓位加倍测试 (max_buy=40000)")
print("─" * 100)

ENGINE_CFG_2X = {**ENGINE_CFG, 'position_sizing': {'min_buy_amount': 2000.0, 'max_buy_amount': 40000.0, 'lot_size': 100, 'min_lots': 1}}

top3 = layer1[:3]
for combo in top3:
    sname, pname = combo['sig'], combo['pool']
    sv = signal_variants[sname]
    sig = sv.loc[START:END]
    cl = [c for c in universes[pname] if c in sig.columns]

    for sn, *sp in STOP_GRID[:4]:  # 只测前4个stop
        if not (sname and pname and sn): continue
        sel = build_sel(sig, cl)
        if sel.empty or len(sel) < 50: continue
        common = sorted(set(close.columns) & set(sel['stock_code'].unique()))
        if len(common) < 10: continue
        cs = close[common].ffill().bfill()
        hs = high.reindex(index=cs.index, columns=common).ffill().bfill()
        ls = low.reindex(index=cs.index, columns=common).ffill().bfill()

        entries = pd.DataFrame(False, index=cs.index, columns=cs.columns)
        for _, row in sel.iterrows():
            code, dt = row['stock_code'], pd.to_datetime(row['select_date'])
            if code not in entries.columns: continue
            if dt in entries.index: entries.loc[dt, code] = True
            else:
                m = entries.index >= dt
                if m.any(): entries.loc[entries.index[m][0], code] = True

        cost_thr, trail_act, trail_dd, ladder_lv, max_hold = tuple(sp)
        STOP = {
            'cost_stop':      {'enabled': True, 'threshold': cost_thr},
            'trailing_stop':  {'enabled': True, 'activation': trail_act, 'drawdown': trail_dd},
            'ladder_tp':      {'enabled': True, 'levels': [{'profit': p, 'sell_ratio': r} for p, r in ladder_lv]},
            'time_stop':      {'enabled': True, 'max_hold_days': max_hold},
            'cond_time_stop': {'enabled': True, 'days': min(7, max_hold-1), 'profit': 0.02},
        }
        lp = np.array([lv[0] for lv in ladder_lv], dtype=np.float64)
        lr = np.array([lv[1] for lv in ladder_lv], dtype=np.float64)

        engine = BacktestEngine(ENGINE_CFG_2X)
        try:
            result = engine.run_cached(cs, entries, hs.values.astype(np.float64),
                                       ls.values.astype(np.float64), STOP, sel, lp, lr, len(ladder_lv), skip_sm=True)
        except Exception as e: logger.warning("run_cached failed: %s", e); continue
        m = result['metrics']
        r = {
            'sig': sname, 'pool': pname, 'stop': sn, 'size': '2x',
            'cumret': result['cumulative_return'], 'annret': m['annualized_return'],
            'maxdd': m['max_drawdown'], 'sharpe': m['sharpe_ratio'],
            'winrate': m['win_rate'], 'trades': len(result['trades']), 'signals': len(sel),
        }
        results.append(r)
        print(f"  [2x仓位] [{sname:<28s}] [{pname:<22s}] [{sn:<32s}] "
              f"累计:{r['cumret']*100:+7.2f}% 年化:{r['annret']*100:+6.2f}% "
              f"回撤:{r['maxdd']*100:.2f}% 夏普:{r['sharpe']:.2f} 交易:{r['trades']:,}")

# ══════════════════════════════════════
# 6. 输出排名
# ══════════════════════════════════════
# 去重
seen = set()
unique = []
for r in sorted(results, key=lambda x: x['annret'], reverse=True):
    k = (r['sig'], r['pool'], r['stop'], r.get('size', '1x'))
    if k not in seen:
        seen.add(k)
        unique.append(r)

print("\n" + "=" * 110)
print("  TOP 25 综合排名")
print("=" * 110)
print(f"  {'#':<4} {'信号公式':<32} {'股票池':<24} {'止损':<34} {'仓位':>4} {'累计%':>8} {'年化%':>8} {'回撤%':>8} {'夏普':>6} {'交易':>7}")
print("  " + "-" * 110)
for i, r in enumerate(unique[:25], 1):
    sz = r.get('size', '1x')
    print(f"  {i:<4} {r['sig']:<32} {r['pool']:<24} {r['stop']:<34} {sz:>4} "
          f"{r['cumret']*100:>+8.2f} {r['annret']*100:>+8.2f} {r['maxdd']*100:>+8.2f} "
          f"{r['sharpe']:>6.2f} {r['trades']:>7,}")

# 年化20%达标检查
above_20 = [r for r in unique if r['annret'] >= 0.20]
print(f"\n  {'─'*60}")
if above_20:
    print(f"  年化≥20%的组合: {len(above_20)} 个")
    for r in above_20:
        print(f"    [{r['sig']}] × [{r['pool']}] × [{r['stop']}] × [{r.get('size','1x')}] → {r['annret']*100:.2f}%")
else:
    best = unique[0] if unique else None
    print(f"  无一达到20%年化。最优: {best['annret']*100:.2f}%")
    print(f"  距离目标差: {(0.20-best['annret'])*100:.1f} 个百分点")

# 保存
if unique:
    output = {
        'best': {'sig': unique[0]['sig'], 'pool': unique[0]['pool'], 'stop': unique[0]['stop'],
                 'cumret': unique[0]['cumret'], 'annret': unique[0]['annret'],
                 'maxdd': unique[0]['maxdd'], 'sharpe': unique[0]['sharpe'],
                 'trades': unique[0]['trades']},
        'above_20pct': [dict(r) for r in above_20],
        'top30': [dict(r) for r in unique[:30]],
        'all': [dict(r) for r in unique],
    }
    with open('output/optimize_turtle_final.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  结果已保存: output/optimize_turtle_final.json")

print("=" * 110)
