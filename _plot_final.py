"""生成反映最终结论的图 (读全列缓存, 含 entry_date 可分时段):
1. final_bucket.png — 细分6桶(<30/.../>300) 平均收益(柱)+暴涨占比(线)
2. final_decay.png — 分时段小盘(<50)vs大盘(>300) 溢价衰减 (核心: 近3年消失)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei']
plt.rcParams['axes.unicode_minus'] = False

OUT = Path('output/mcap_analysis')
df = pd.read_csv(OUT / 'trades_with_mcap.csv', encoding='utf-8-sig')
df['entry_date'] = pd.to_datetime(df['entry_date'])
df['pnl_pct'] = df['profit_pct'] * 100
df['year'] = df['entry_date'].dt.year

BINS = [0, 30, 50, 100, 200, 300, float('inf')]
LABELS = ['<30', '30-50', '50-100', '100-200', '200-300', '>300']
df['bucket'] = pd.cut(df['circ_mcap'], bins=BINS, labels=LABELS, include_lowest=True)

# 图1: 细分桶 平均收益(柱)+暴涨占比(线)
g = df.groupby('bucket', observed=True)
avg = g['pnl_pct'].mean()
surge = g['pnl_pct'].apply(lambda s: (s > 50).mean() * 100)
fig, ax1 = plt.subplots(figsize=(10, 5.5))
x = list(range(len(LABELS)))
ax1.bar(x, avg, color='#5b5bd6', alpha=0.85, label='平均收益%')
ax1.set_ylabel('平均收益率 %', color='#5b5bd6')
ax1.set_xticks(x); ax1.set_xticklabels([f'{l}亿' for l in LABELS])
ax1.set_xlabel('流通市值')
for i, v in enumerate(avg):
    ax1.text(i, v + 0.12, f'{v:.1f}', ha='center', fontsize=9, color='#3a3aa6')
ax2 = ax1.twinx()
ax2.plot(x, surge, color='#f5a623', marker='o', linewidth=2.2, label='暴涨>50%占比%')
ax2.set_ylabel('暴涨(>50%)占比 %', color='#f5a623')
for i, v in enumerate(surge):
    ax2.text(i, v + 0.04, f'{v:.2f}', ha='center', color='#b87400', fontsize=8)
ax1.set_title('黑马起步 全A: 流通市值分桶 — 平均收益(紫柱)与暴涨占比(橙线)\n微盘<30亿平均最高7.4%、暴涨概率2.09%是大盘0.27%的7.7倍')
ax1.grid(axis='y', alpha=0.25)
fig.tight_layout(); fig.savefig(OUT / 'final_bucket.png', dpi=150); plt.close(fig)

# 图2: 分时段小盘溢价衰减
df['period'] = pd.cut(df['year'], bins=[2018, 2020, 2022, 2024, 2026],
                      labels=['2019-2020', '2021-2022', '2023-2024', '2025-2026'])
small = df[df['circ_mcap'] < 50].groupby('period', observed=True)['pnl_pct'].mean()
big = df[df['circ_mcap'] > 300].groupby('period', observed=True)['pnl_pct'].mean()
premium = (small - big)
fig, ax = plt.subplots(figsize=(10, 5.5))
w = 0.35; xp = list(range(len(premium)))
ax.bar([i - w / 2 for i in xp], small.values, width=w, label='小盘<50亿', color='#5b5bd6')
ax.bar([i + w / 2 for i in xp], big.values, width=w, label='大盘>300亿', color='#8e4ec6')
for i, v in enumerate(small.values):
    ax.text(i - w / 2, v + 0.3, f'{v:.1f}', ha='center', fontsize=9)
for i, v in enumerate(big.values):
    ax.text(i + w / 2, v + 0.3, f'{v:.1f}', ha='center', fontsize=9)
# 溢价标注
for i, p in enumerate(premium.values):
    ax.annotate(f'溢价{p:+.1f}pp', (i, max(small.values[i], big.values[i]) + 2.5),
                ha='center', fontsize=9, color='#d1242f', fontweight='bold')
ax.set_xticks(xp); ax.set_xticklabels(premium.index)
ax.set_ylabel('平均收益率 %'); ax.set_xlabel('时段')
ax.set_title('小盘溢价随时间衰减 (小盘-大盘 收益差)\n核心发现: 2019-2020溢价+10.4pp → 2023-2024仅+0.5pp, 衰减95%, 近3年几乎消失')
ax.legend(loc='upper right'); ax.grid(axis='y', alpha=0.3)
fig.tight_layout(); fig.savefig(OUT / 'final_decay.png', dpi=150); plt.close(fig)

print('生成: final_bucket.png, final_decay.png', flush=True)
print(f'分时段小盘溢价(pp): ' + ' | '.join(f'{p}={v:+.2f}' for p, v in premium.items()), flush=True)
