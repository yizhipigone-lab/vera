"""黑马起步 全A: 按流通市值分桶验证小盘股效应。
带缓存: 首次全跑(全A回测+取股本)后存 trades_with_mcap.csv, 之后改图/改桶读缓存秒出。
分桶: <50 / 50-100 / 100-200 / 200-300 / >300亿 (流通市值, 互斥)。
市值=最新股本(ActiveCapital流通/J_zgb总,万股)×入场价, TDX取不到历史股本, 有股本变动误差。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei']
plt.rcParams['axes.unicode_minus'] = False

BINS = [0, 50, 100, 200, 300, float('inf')]
LABELS = ['<50亿', '50-100亿', '100-200亿', '200-300亿', '>300亿']
OUT = Path('output/mcap_analysis')
CACHE = OUT / 'trades_with_mcap.csv'

def add_bucket(df):
    df['cap_bucket'] = pd.cut(df['circ_mcap'], bins=BINS, labels=LABELS, include_lowest=True)
    return df

print("=" * 60, flush=True)
print("黑马起步 全A 市值分桶分析  2019-2026", flush=True)
print("=" * 60, flush=True)

if CACHE.exists():
    print(f"\n命中缓存 {CACHE.name}, 跳过全A重跑+取股本\n", flush=True)
    trades_v = pd.read_csv(CACHE, encoding='utf-8-sig')
    pnl_col = 'profit_pct'
    trades_v = add_bucket(trades_v)
    trades_v['pnl_pct'] = trades_v[pnl_col] * 100
else:
    from core.connector import TdxConnector
    from pipeline.pipeline import Pipeline
    TdxConnector.initialize()
    from tqcenter import tq

    print("\n[1/5] 重跑全A回测拿交易明细...", flush=True)
    pipe = Pipeline('config/strategy_heima_chuangye.yaml', 'config/default.yaml')
    pipe.config['selection']['universe']['type'] = '5'
    pipe.config['strategy']['name'] = '黑马起步_全A'
    selections = pipe.step1_select()
    bt = pipe.step2_backtest(selections)
    trades = bt['trades'].copy()
    print(f"  交易笔数: {len(trades)}, 涉及 {trades['stock_code'].nunique()} 只", flush=True)
    pnl_col = 'profit_pct' if 'profit_pct' in trades.columns else 'return'

    print(f"\n[2/5] 取 {trades['stock_code'].nunique()} 只股票流通/总股本...", flush=True)
    stocks = list(trades['stock_code'].unique())
    cap_cache = {}
    for i, code in enumerate(stocks):
        if i % 500 == 0:
            print(f"  进度 {i}/{len(stocks)}, 已取 {len(cap_cache)}", flush=True)
        try:
            info = tq.get_stock_info(code)
            ac = info.get('ActiveCapital'); zg = info.get('J_zgb')
            if ac is not None:
                cap_cache[code] = (float(ac), float(zg) if zg is not None else None)
        except Exception:
            pass
    print(f"  取到 {len(cap_cache)} 只", flush=True)

    trades['active_cap'] = trades['stock_code'].map(lambda c: cap_cache.get(c, (None, None))[0])
    trades['total_cap'] = trades['stock_code'].map(lambda c: cap_cache.get(c, (None, None))[1])
    trades['circ_mcap'] = trades['active_cap'] * trades['entry_price'] / 10000  # 亿
    trades['total_mcap'] = trades['total_cap'] * trades['entry_price'] / 10000
    valid = trades['circ_mcap'].notna()
    print(f"  有市值数据: {valid.sum()} / {len(trades)} ({valid.mean()*100:.1f}%)", flush=True)
    trades_v = trades[valid].copy()
    trades_v = add_bucket(trades_v)
    trades_v['pnl_pct'] = trades_v[pnl_col] * 100

    OUT.mkdir(parents=True, exist_ok=True)
    trades_v.to_csv(CACHE, index=False, encoding='utf-8-sig')  # 全列(含hold_days/exit_reason), 供画像用
    print(f"  已存缓存 {CACHE} (全列 {len(trades_v.columns)}列)", flush=True)
    TdxConnector.close()

# [3/5] 分桶统计
print(f"\n[3/5] 分桶统计 (流通市值):", flush=True)
g = trades_v.groupby('cap_bucket', observed=True)['pnl_pct']
summary = pd.DataFrame({
    '笔数': g.count(),
    '平均收益%': g.mean(),
    '中位数%': g.median(),
    '胜率%': g.apply(lambda s: (s > 0).mean() * 100),
    '盈亏比': g.apply(lambda s: (s[s > 0].mean() / abs(s[s < 0].mean())) if (s < 0).any() and (s > 0).any() else np.nan),
    '平均流通市值亿': trades_v.groupby('cap_bucket', observed=True)['circ_mcap'].mean(),
})
print(summary.round(2).to_string(), flush=True)

# [4/5] 相关性
print(f"\n[4/5] 相关性:", flush=True)
corr_trade = trades_v[['circ_mcap', 'pnl_pct']].corr().iloc[0, 1]
print(f"  逐笔: 流通市值 vs 单笔收益率  r = {corr_trade:.4f}", flush=True)
by_stock = trades_v.groupby('stock_code').agg(avg_pnl=('pnl_pct', 'mean'), circ=('circ_mcap', 'first'))
corr_stock = by_stock[['circ', 'avg_pnl']].corr().iloc[0, 1]
print(f"  按股聚合: 流通市值 vs 平均收益  r = {corr_stock:.4f}", flush=True)

# [5/5] 出图
print(f"\n[5/5] 出图...", flush=True)
OUT.mkdir(parents=True, exist_ok=True)
ACCENT, WARN = '#5b5bd6', '#f5a623'
x = list(range(len(LABELS)))

fig, ax = plt.subplots(figsize=(10, 5))
ax.bar([i - 0.2 for i in x], summary['平均收益%'], width=0.4, label='平均收益', color=ACCENT)
ax.bar([i + 0.2 for i in x], summary['中位数%'], width=0.4, label='中位数', color=WARN)
ax.set_xticks(x); ax.set_xticklabels(LABELS)
ax.set_ylabel('收益率 %'); ax.set_title('黑马起步 全A: 各流通市值桶的平均/中位数收益 (2019-2026)')
ax.legend(); ax.grid(axis='y', alpha=0.3)
for i in x:
    ax.text(i - 0.2, summary['平均收益%'].iloc[i] + 0.15, f"{summary['平均收益%'].iloc[i]:.1f}", ha='center', fontsize=8)
    ax.text(i + 0.2, summary['中位数%'].iloc[i] + 0.15, f"{summary['中位数%'].iloc[i]:.1f}", ha='center', fontsize=8)
fig.tight_layout(); fig.savefig(OUT / 'bucket_bar.png', dpi=150); plt.close(fig)

fig, ax = plt.subplots(figsize=(10, 5))
sample = trades_v.sample(min(5000, len(trades_v)), random_state=1)
ax.scatter(sample['circ_mcap'], sample['pnl_pct'], s=4, alpha=0.2, color=ACCENT)
ax.set_xscale('log'); ax.axhline(0, color='red', linewidth=0.8, alpha=0.5)
ax.set_xlabel('流通市值 (亿元, 对数轴)'); ax.set_ylabel('单笔收益率 %')
ax.set_title(f'市值 vs 收益散点 (n={len(sample)}, 逐笔相关 r={corr_trade:.3f})')
ax.grid(alpha=0.3)
fig.tight_layout(); fig.savefig(OUT / 'scatter.png', dpi=150); plt.close(fig)

fig, ax = plt.subplots(figsize=(10, 5))
data_box = [trades_v[trades_v['cap_bucket'] == b]['pnl_pct'].values for b in LABELS]
try:  # matplotlib 3.9+ 用 tick_labels 替代 labels
    bp = ax.boxplot(data_box, tick_labels=LABELS, showfliers=False, whis=[5, 95], patch_artist=True)
except TypeError:
    bp = ax.boxplot(data_box, labels=LABELS, showfliers=False, whis=[5, 95], patch_artist=True)
for patch in bp['boxes']:
    patch.set_facecolor(ACCENT); patch.set_alpha(0.4)
ax.axhline(0, color='red', linewidth=0.8, alpha=0.5)
ax.set_ylabel('单笔收益率 %'); ax.set_xlabel('流通市值桶')
ax.set_title('各市值桶收益分布 (箱体5-95%, 中位线, 去异常值)')
ax.grid(axis='y', alpha=0.3)
fig.tight_layout(); fig.savefig(OUT / 'bucket_box.png', dpi=150); plt.close(fig)

summary.round(4).to_csv(OUT / 'bucket_summary.csv', encoding='utf-8-sig')
print(f"\n输出: {OUT}", flush=True)
print("  bucket_bar.png / scatter.png / bucket_box.png / bucket_summary.csv / trades_with_mcap.csv", flush=True)
print("=== 完成 ===", flush=True)
