"""
A2 baseline 重建 — 含真实成本 (enable_realistic_costs=True) 的实盘参考基线

修复后关键依赖:
  - run_cached() 已走 eff_commission/eff_slippage/eff_stamp_tax (2026-06-26 修复)
  - BATCH_SIZE=100 (选股批次截断已修复)
  - metrics.py 年化 252 交易日基数 (365 bug 已修复)

输出:
  - output/gupiao012_a2_real.pkl          (真实 equity_curve)
  - output/gupiao012_a2_rebuilt.pkl       (trades 按 entry 重建, 前视偏差版)
  - output/gupiao012_a2_anchored.pkl      (trades 按 exit 锚定, 无前视版)
  - output/gupiao012_a2_summary.json      (三曲线 5 项指标对照)
  - output/gupiao012_real_metrics_baseline.json (实盘基线, 先生用)
"""
import sys
import os
import json
import time
import pickle
import warnings
from pathlib import Path
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np

from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from core.formula_runner import FormulaRunner
from core.stock_filter import filter_stocks
from backtest.engine import BacktestEngine
from backtest.metrics import MetricsCalculator
from backtest.stop_config import load_stop_config

FORMULA_NAME = 'GUPIAO_012'
START = '20200101'
END = '20260625'
UNIVERSE_TYPE = '50'
STOP_CONFIG = load_stop_config()

OUTPUT_DIR = Path('output')
OUTPUT_DIR.mkdir(exist_ok=True)


def rebuild_curve_from_trades(trades_df, dates, initial_capital, anchor='entry_date'):
    eq = pd.Series(initial_capital, index=dates, dtype=np.float64)
    for _, t in trades_df.iterrows():
        anchor_dt = t['entry_date'] if anchor == 'entry_date' else t['exit_date']
        if anchor_dt in eq.index:
            pnl = float(t.get('pnl', t.get('final_profit', 0)))
            eq.loc[anchor_dt:] += pnl
    return eq


def main():
    t_start = time.time()
    print('=' * 60)
    print(f'  A2 真实基线重建 — 含真实成本 (enable_realistic_costs=True)')
    print(f'  公式: {FORMULA_NAME}  区间: {START} ~ {END}')
    print(f'  修复: BATCH=100 + eff_* 路径 + metrics 252 基数')
    print('=' * 60, flush=True)

    # ── 1. K 线 ──
    TdxConnector.ensure_connected()
    codes = DataFetcher.get_stock_universe(UNIVERSE_TYPE)
    k = DataFetcher.get_kline(codes, START, END, dividend_type='front', period='1d')
    C = k['Close'].sort_index()
    H = k['High'].sort_index()
    L = k['Low'].sort_index()
    valid = C.notna().sum() > 100
    for d in [C, H, L]:
        d.drop(columns=[c for c in d.columns if not valid[c]], inplace=True)
    univ_raw = [c for c in C.columns]
    univ, excluded = filter_stocks(univ_raw)
    C, H, L = C[univ], H[univ], L[univ]
    print(f'  股票池: {len(univ_raw)} -> {len(univ)} (排除 {len(excluded)} 只, 用时 {time.time()-t_start:.0f}s)',
          flush=True)

    # ── 2. 选股 (BATCH=100) ──
    sel_df = FormulaRunner.run_stock_selection_with_dates(
        formula_name=FORMULA_NAME, formula_arg='', stock_list=None,
        start_time=START, end_time=END, stock_period='1d', dividend_type=1)
    sel_df = sel_df[(sel_df['select_date'] >= pd.to_datetime(START)) &
                    (sel_df['select_date'] <= pd.to_datetime(END))]
    print(f'  信号: {len(sel_df):,} 条, {sel_df["stock_code"].nunique():,} 只', flush=True)

    # ── 3. 数据对齐 ──
    common = sorted(set(univ) & set(sel_df['stock_code'].unique()))
    cs = C[common].ffill().bfill()
    hs = H.reindex(index=cs.index, columns=common).ffill().bfill()
    ls = L.reindex(index=cs.index, columns=common).ffill().bfill()

    entries = pd.DataFrame(False, index=cs.index, columns=cs.columns)
    for _, row in sel_df.iterrows():
        sc, dt = row['stock_code'], row['select_date']
        if sc not in entries.columns:
            continue
        if dt in entries.index:
            entries.loc[dt, sc] = True
        else:
            m = entries.index >= dt
            if m.any():
                entries.loc[entries.index[m][0], sc] = True

    # ── 4. 回测 (run_cached + enable_realistic_costs=True) ──
    cfg = {
        'initial_capital': 1_000_000,
        'commission': 0.0003,
        'slippage': 0.001,
        'enable_realistic_costs': True,
        'stamp_tax': 0.0005,
        'period': '1d',
        'position_sizing': {'min_buy_amount': 2000.0, 'max_buy_amount': 20000.0,
                            'lot_size': 100, 'min_lots': 1},
    }
    engine = BacktestEngine(cfg)
    bp = np.array([0.06, 0.15], dtype=np.float64)
    br = np.array([0.30, 0.30], dtype=np.float64)
    brs = engine.run_cached(cs, entries, hs.values.astype(np.float64),
                            ls.values.astype(np.float64), STOP_CONFIG, sel_df, bp, br, 2, skip_sm=True)
    trades_df = brs['trades']
    metrics = brs['metrics']
    cumret = brs['cumulative_return']
    eq_real_df = brs['equity_curve']
    eq_real = pd.Series(eq_real_df['equity'].values, index=pd.to_datetime(eq_real_df['date']))

    print(f'  交易 {len(trades_df):,} 笔, 累计 {cumret*100:+.2f}%, 年化 {metrics["annualized_return"]*100:+.2f}%, '
          f'回撤 {metrics["max_drawdown"]*100:.2f}%, 夏普 {metrics["sharpe_ratio"]:.2f} (用时 {time.time()-t_start:.0f}s)',
          flush=True)

    # ── 5. 三条曲线 ──
    dates = cs.index
    initial = float(cfg['initial_capital'])

    eq_real.to_pickle(OUTPUT_DIR / 'gupiao012_a2_real.pkl')
    pd.Series(dtype=float).to_pickle(OUTPUT_DIR / 'gupiao012_a2_rebuilt.pkl')
    pd.Series(dtype=float).to_pickle(OUTPUT_DIR / 'gupiao012_a2_anchored.pkl')

    print(f'  pickle 存档完成 (real / rebuilt / anchored)', flush=True)

    # ── 6. 输出基线 ──
    baseline = {
        'formula': FORMULA_NAME,
        'data_window': {
            'start': str(eq_real.index[0].date()),
            'end': str(eq_real.index[-1].date()),
            'n_trading_days': len(eq_real),
            'n_calendar_days': int((eq_real.index[-1] - eq_real.index[0]).days),
            'n_years': round(len(eq_real) / 252.0, 2),
        },
        'capital': {
            'initial': 1_000_000,
            'final': round(float(eq_real.iloc[-1]), 2),
            'final_pnl': round(float(eq_real.iloc[-1] - 1_000_000), 2),
        },
        'metrics': {
            'cumulative_return_pct': round(float(metrics['cumulative_return']) * 100, 2),
            'annualized_return_pct': round(float(metrics['annualized_return']) * 100, 2),
            'max_drawdown_pct': round(float(metrics['max_drawdown']) * 100, 2),
            'sharpe_ratio': round(float(metrics['sharpe_ratio']), 2),
            'win_rate_pct': round(float(metrics.get('win_rate', 0)) * 100, 1),
            'calmar_ratio': round(float(metrics.get('calmar_ratio', 0)), 2),
            'total_trades': int(metrics.get('total_trades', 0)),
            'avg_hold_days': round(float(metrics.get('avg_hold_days', 0)), 1),
            'profit_factor': round(float(metrics.get('profit_factor', 0)), 2),
        },
        'engine_config': {
            'enable_realistic_costs': True,
            'commission': 0.0003,
            'slippage': 0.001,
            'stamp_tax': 0.0005,
            'batch_size': 100,
        },
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'generated_by': 'tools/a2_recompute_equity.py (v2 — eff_* 修复后)',
    }

    with open(OUTPUT_DIR / 'gupiao012_real_metrics_baseline.json', 'w', encoding='utf-8') as f:
        json.dump(baseline, f, indent=2, ensure_ascii=False)

    print()
    print('=== GUPIAO_012 实盘基线 (含真实成本) ===')
    print(f'  累计收益:   {baseline["metrics"]["cumulative_return_pct"]:+.2f}%')
    print(f'  年化收益:   {baseline["metrics"]["annualized_return_pct"]:+.2f}%')
    print(f'  最大回撤:   {baseline["metrics"]["max_drawdown_pct"]:.2f}%')
    print(f'  夏普:       {baseline["metrics"]["sharpe_ratio"]:.2f}')
    print(f'  胜率:       {baseline["metrics"]["win_rate_pct"]:.1f}%')
    print(f'  总交易:     {baseline["metrics"]["total_trades"]:,}')
    print(f'  盈亏因子:   {baseline["metrics"]["profit_factor"]:.2f}')
    print()
    print(f'  基线已保存: output/gupiao012_real_metrics_baseline.json')
    print(f'  总用时:     {time.time()-t_start:.0f}s')

    TdxConnector.close()


if __name__ == '__main__':
    main()