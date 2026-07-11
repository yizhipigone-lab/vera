"""全维度画像 (读全列缓存 + 连TDX取行业):
暴涨股 + avg>10%股 的 市值/价格/持有期/退出原因/行业 画像。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd
import numpy as np
pd.set_option('display.width', 200)

OUT = Path('output/mcap_analysis')
df = pd.read_csv(OUT / 'trades_with_mcap.csv', encoding='utf-8-sig')
BINS = [0, 30, 50, 100, 200, 300, float('inf')]
LABELS = ['<30亿', '30-50亿', '50-100亿', '100-200亿', '200-300亿', '>300亿']
df['bucket'] = pd.cut(df['circ_mcap'], bins=BINS, labels=LABELS, include_lowest=True)
df['pnl_pct'] = df['profit_pct'] * 100
print(f"全列数据: {len(df)} 笔, {df['stock_code'].nunique()} 只股\n", flush=True)

surge_codes = df[df['pnl_pct'] > 50]['stock_code'].unique()
byall_mean = df.groupby('stock_code')['pnl_pct'].mean()
high_codes = byall_mean[byall_mean > 10].index
need = list(set(list(surge_codes) + list(high_codes)))
print(f"取行业: {len(need)} 只股 (暴涨股+avg10股)", flush=True)

from core.connector import TdxConnector
TdxConnector.initialize()
from tqcenter import tq
industry = {}
for i, code in enumerate(need):
    if i % 200 == 0:
        print(f"  行业进度 {i}/{len(need)}", flush=True)
    try:
        info = tq.get_stock_info(code)
        hy = info.get('rs_hyname')
        if hy:
            industry[code] = hy
    except Exception:
        pass
TdxConnector.close()
print(f"取到行业 {len(industry)} 只\n", flush=True)
df['industry'] = df['stock_code'].map(industry)

def profile(stocks_df, label, outname):
    by = stocks_df.groupby('stock_code').agg(
        n=('pnl_pct', 'count'), avg=('pnl_pct', 'mean'), med=('pnl_pct', 'median'),
        win=('pnl_pct', lambda s: (s > 0).mean()),
        mcap=('circ_mcap', 'mean'), price=('entry_price', 'mean'), hold=('hold_days', 'mean'),
        industry=('industry', 'first'))
    by = by.sort_values('avg', ascending=False)
    by.to_csv(OUT / outname, encoding='utf-8-sig')
    print(f"{'=' * 60}", flush=True)
    print(f"{label}", flush=True)
    print(f"  股票数={len(by)}  笔数={len(stocks_df)}", flush=True)
    print(f"  流通市值(亿): 中位={by['mcap'].median():.1f} 均={by['mcap'].mean():.1f} 25%={by['mcap'].quantile(.25):.1f} 75%={by['mcap'].quantile(.75):.1f}", flush=True)
    print(f"  入场价(元):   中位={by['price'].median():.2f} 均={by['price'].mean():.2f} 75%={by['price'].quantile(.75):.2f}", flush=True)
    print(f"  平均持有期(天): 中位={by['hold'].median():.1f} 均={by['hold'].mean():.1f}", flush=True)
    print(f"  每股交易次数: 中位={by['n'].median():.0f} 均={by['n'].mean():.1f} max={by['n'].max()}", flush=True)
    print(f"  平均单笔收益%: 中位={by['avg'].median():.1f} 均={by['avg'].mean():.1f}", flush=True)
    print(f"  胜率: 中位={by['win'].median()*100:.0f}% 均={by['win'].mean()*100:.0f}%", flush=True)
    reason = stocks_df['exit_reason'].value_counts()
    top = reason.head(4)
    print(f"  退出原因: " + " | ".join(f"{k}({v},{v/len(stocks_df)*100:.0f}%)" for k, v in top.items()), flush=True)
    ind = by['industry'].dropna().value_counts()
    topi = ind.head(8)
    print(f"  行业Top8: " + " | ".join(f"{k}:{v}" for k, v in topi.items()), flush=True)
    return by

profile(df[df['stock_code'].isin(surge_codes)], "暴涨股全维度画像 (单笔>50%的股)", 'surge_stocks_full.csv')
profile(df[df['stock_code'].isin(high_codes)], "平均收益>10%股全维度画像 (策略王牌股)", 'high_avg_stocks_full.csv')
print("\n=== 全维度画像完成 ===", flush=True)
