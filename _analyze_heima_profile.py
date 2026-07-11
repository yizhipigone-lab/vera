"""画像分析 (单一数据源 trades_with_mcap.csv, 22329笔, 避免跨次合并放大):
细分桶 + 暴涨股 + avg>10%股。维度: 市值/价格/收益/交易次数/胜率。
(持有期/退出原因/行业 待全列缓存重跑后补)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd
import numpy as np
pd.set_option('display.width', 200)

OUT = Path('output/mcap_analysis')
df = pd.read_csv(OUT / 'trades_with_mcap.csv', encoding='utf-8-sig')
df['pnl_pct'] = df['profit_pct'] * 100
print(f"数据: {len(df)} 笔, {df['stock_code'].nunique()} 只股 (单一源, 无合并放大)\n", flush=True)

BINS = [0, 30, 50, 100, 200, 300, float('inf')]
LABELS = ['<30亿', '30-50亿', '50-100亿', '100-200亿', '200-300亿', '>300亿']
df['bucket'] = pd.cut(df['circ_mcap'], bins=BINS, labels=LABELS, include_lowest=True)

print("=" * 60, flush=True)
print("细分市值桶 (微盘单独看)", flush=True)
print("=" * 60, flush=True)
g = df.groupby('bucket', observed=True)
tbl = pd.DataFrame({
    '笔数': g['pnl_pct'].count(),
    '平均%': g['pnl_pct'].mean(),
    '中位%': g['pnl_pct'].median(),
    '胜率%': g['pnl_pct'].apply(lambda s: (s > 0).mean() * 100),
    '盈亏比': g['pnl_pct'].apply(lambda s: s[s > 0].mean() / abs(s[s < 0].mean()) if (s < 0).any() and (s > 0).any() else np.nan),
    '暴涨>50%占比': g['pnl_pct'].apply(lambda s: (s > 50).mean() * 100),
})
print(tbl.round(2).to_string(), flush=True)
tbl.to_csv(OUT / 'bucket_fine.csv', encoding='utf-8-sig')

def profile(stocks_df, label, outname):
    by = stocks_df.groupby('stock_code').agg(
        n=('pnl_pct', 'count'), avg=('pnl_pct', 'mean'), med=('pnl_pct', 'median'),
        win=('pnl_pct', lambda s: (s > 0).mean()),
        mcap=('circ_mcap', 'mean'), price=('entry_price', 'mean'))
    by = by.sort_values('avg', ascending=False)
    by.to_csv(OUT / outname, encoding='utf-8-sig')
    print(f"\n{'=' * 60}", flush=True)
    print(f"{label}", flush=True)
    print(f"  股票数={len(by)}  笔数={len(stocks_df)}", flush=True)
    print(f"  流通市值(亿): 中位={by['mcap'].median():.1f} 均={by['mcap'].mean():.1f} 25%={by['mcap'].quantile(.25):.1f} 75%={by['mcap'].quantile(.75):.1f}", flush=True)
    print(f"  入场价(元):   中位={by['price'].median():.2f} 均={by['price'].mean():.2f} 75%={by['price'].quantile(.75):.2f}", flush=True)
    print(f"  每股交易次数: 中位={by['n'].median():.0f} 均={by['n'].mean():.1f} max={by['n'].max()}", flush=True)
    print(f"  平均单笔收益%: 中位={by['avg'].median():.1f} 均={by['avg'].mean():.1f}", flush=True)
    print(f"  胜率: 中位={by['win'].median()*100:.0f}% 均={by['win'].mean()*100:.0f}%", flush=True)
    mb = by['mcap'].apply(lambda x: '<30' if x < 30 else '30-50' if x < 50 else '50-100' if x < 100 else '100-200' if x < 200 else '200-300' if x < 300 else '>300').value_counts()
    mb = mb.reindex(['<30', '30-50', '50-100', '100-200', '200-300', '>300']).fillna(0).astype(int)
    print(f"  市值段分布: " + " | ".join(f"{k}:{v}" for k, v in mb.items() if v > 0), flush=True)
    print(f"  Top10 代表: " + ", ".join(f"{c}({a:.0f}%)" for c, a in list(by['avg'].items())[:10]), flush=True)
    return by

surge = df[df['pnl_pct'] > 50]
sc = surge['stock_code'].unique()
print(f"\n单笔>50%暴涨: {len(surge)} 笔 / {len(sc)} 只股", flush=True)
profile(df[df['stock_code'].isin(sc)], "暴涨股画像 (出现过单笔>50%的股)", 'surge_stocks.csv')

byall = df.groupby('stock_code')['pnl_pct'].mean()
hc = byall[byall > 10].index
print(f"\n平均收益>10%的股: {len(hc)} 只 ({len(hc) / df['stock_code'].nunique() * 100:.1f}%)", flush=True)
profile(df[df['stock_code'].isin(hc)], "平均收益>10% 股画像 (策略对它们特别有效)", 'high_avg_stocks.csv')

print("\n=== 基础画像完成 (持有期/退出原因/行业 待全列缓存重跑补) ===", flush=True)
