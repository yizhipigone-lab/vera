"""
GUPIAO_012 实盘参考基线 — A2 + 选1 闭环产出

数据来源:
  - 实盘参考数字全部来自 output/gupiao012_a2_real.pkl (C2 修复后真实 equity_curve)
  - 年化已用 252 交易日基数 (修复后)
  - 夏普已扣 risk_free=1.5% (与 metrics.py 口径一致)

先生用法:
  - 实盘月度/季度结算, 拿来对照
  - 任何时段回测, 拿来对照基线
  - 公式调整/参数优化前, 记下基线作为"原配方"参考
"""
import sys
import os
import json
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from backtest.metrics import MetricsCalculator


def build_baseline():
    pkl_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'output', 'gupiao012_a2_real.pkl')
    if not os.path.exists(pkl_path):
        raise FileNotFoundError(f"缺少 {pkl_path}, 请先跑 tools/a2_recompute_equity.py")

    eq = pd.read_pickle(pkl_path)
    df = pd.DataFrame({'date': pd.DatetimeIndex(eq.index), 'equity': eq.values})

    m = MetricsCalculator.compute_all(df, pd.DataFrame(), initial_capital=1_000_000,
                                      risk_free=0.015, periods_per_year=252)

    n_days = len(df)
    cumret = m['cumulative_return']
    annret = m['annualized_return']
    maxdd = m['max_drawdown']
    sharpe = m['sharpe_ratio']

    # 计算日胜率 (交易胜率 metrics.py 不算, 单独算)
    daily_ret = df['equity'].pct_change().dropna()
    daily_winrate = float((daily_ret > 0).mean())

    # 最大回撤发生日期
    peak = df['equity'].expanding().max()
    dd_series = (df['equity'] - peak) / peak
    maxdd_date = df.loc[dd_series.idxmin(), 'date']

    # 峰值日期
    peak_date = df.loc[df['equity'].idxmax(), 'date']

    baseline = {
        'formula': 'GUPIAO_012',
        'data_window': {
            'start': str(df['date'].iloc[0].date()),
            'end': str(df['date'].iloc[-1].date()),
            'n_trading_days': int(n_days),
            'n_calendar_days': int((df['date'].iloc[-1] - df['date'].iloc[0]).days),
            'n_years': round(n_days / 252.0, 2),
        },
        'capital': {
            'initial': 1_000_000,
            'final': float(df['equity'].iloc[-1]),
            'final_pnl': float(df['equity'].iloc[-1] - 1_000_000),
        },
        'metrics': {
            'cumulative_return': round(cumret, 4),
            'cumulative_return_pct': round(cumret * 100, 2),
            'annualized_return': round(annret, 4),
            'annualized_return_pct': round(annret * 100, 2),
            'max_drawdown': round(maxdd, 4),
            'max_drawdown_pct': round(maxdd * 100, 2),
            'max_drawdown_date': str(maxdd_date.date()),
            'sharpe_ratio': round(sharpe, 2),
            'daily_winrate': round(daily_winrate, 4),
            'daily_winrate_pct': round(daily_winrate * 100, 2),
            'calmar_ratio': round(annret / abs(maxdd), 2),
        },
        'real_pickle_path': 'output/gupiao012_a2_real.pkl',
        'baseline_purpose': (
            'GUPIAO_012 实盘运行参考基线. 实盘月度/季度结算拿来对照. '
            '任何公式调整/参数优化前, 记下当前 baseline, 调整后跑同区间对照, '
            '若新策略 baseline 全部指标不低于本基线, 才算"真改进".'
        ),
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'generated_by': 'tools/a2_build_baseline.py',
        'metrics_module': 'backtest.metrics.MetricsCalculator.compute_all (252 交易日基数, 修复后)',
    }

    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'output', 'gupiao012_real_metrics_baseline.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(baseline, f, indent=2, ensure_ascii=False)

    print('=== GUPIAO_012 实盘参考基线 ===')
    print()
    for k, v in baseline['data_window'].items():
        print(f'  {k:<20}: {v}')
    print()
    print('  资金:')
    for k, v in baseline['capital'].items():
        print(f'    {k:<15}: {v:,.0f}' if isinstance(v, (int, float)) else f'    {k:<15}: {v}')
    print()
    print('  指标 (先生实盘对照用):')
    print(f'    累计收益           : {baseline["metrics"]["cumulative_return_pct"]:+.2f}%')
    print(f'    年化收益           : {baseline["metrics"]["annualized_return_pct"]:+.2f}%')
    print(f'    最大回撤           : {baseline["metrics"]["max_drawdown_pct"]:.2f}%  (发生于 {baseline["metrics"]["max_drawdown_date"]})')
    print(f'    夏普比率           : {baseline["metrics"]["sharpe_ratio"]:.2f}')
    print(f'    日胜率             : {baseline["metrics"]["daily_winrate_pct"]:.2f}%')
    print(f'    卡玛比率           : {baseline["metrics"]["calmar_ratio"]:.2f}')
    print()
    print(f'  沉淀到: {out_path}')


if __name__ == '__main__':
    build_baseline()